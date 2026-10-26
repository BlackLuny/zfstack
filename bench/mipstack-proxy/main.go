// Command mipstack-proxy is the mihomo IP stack (MIPS) side of zfbench's
// client-mode TUN comparison.
//
//	kernel TCP client ──▶ 198.18.0.0/15 ──TUN──▶ mipstack ──▶ fixed upstream
//
// It terminates TCP in mipstack and relays each accepted connection to one
// kernel TCP upstream, the same shape as zfbench --tun-proxy with a direct
// outbound. Checksums stay on unless -rx-offload is set: MIPS has no TX
// checksum or TSO offload, so -rx-offload only skips input verification.
package main

import (
	"context"
	"errors"
	"flag"
	"fmt"
	"io"
	"net"
	"net/netip"
	"os"
	"os/exec"
	"syscall"
	"unsafe"

	"github.com/metacubex/mipstack"
)

func main() {
	mtu := flag.Uint("mtu", 65535, "TUN and stack MTU")
	upstream := flag.String("upstream", "127.0.0.1:5201", "kernel TCP upstream")
	tunName := flag.String("tun", "zfcT", "TUN device name")
	addr := flag.String("addr", "172.19.0.1/30", "TUN interface address")
	rxOffload := flag.Bool("rx-offload", false, "trust input IPv4 header and TCP checksums")
	flag.Parse()

	// An addressless promiscuous stack installs an IPv6 default route, so the
	// stack rejects MTUs below the IPv6 minimum even for IPv4-only traffic.
	if *mtu < 1280 || *mtu > 65535 {
		fatalf("mtu %d is outside 1280..65535", *mtu)
	}
	prefix, err := netip.ParsePrefix(*addr)
	if err != nil {
		fatalf("addr: %v", err)
	}
	// The kernel address stays on the TUN so the host can select a source.
	// It must not be a stack-local address: client packets use that source,
	// and MIPS loopback-delivers replies to its own addresses instead of
	// writing them back to the TUN.

	_ = exec.Command("ip", "link", "del", *tunName).Run()
	fd, err := openTUN(*tunName)
	if err != nil {
		fatalf("tun: %v", err)
	}
	defer syscall.Close(fd)
	if err := run("ip", "addr", "add", prefix.String(), "dev", *tunName); err != nil {
		fatalf("addr: %v", err)
	}
	if err := run("ip", "link", "set", *tunName, "mtu", fmt.Sprint(*mtu), "txqueuelen", "500", "up"); err != nil {
		fatalf("link: %v", err)
	}

	stack, err := mipstack.New(mipstack.Config{
		Promiscuous: true,
		MTU:         uint32(*mtu),
	})
	if err != nil {
		fatalf("stack: %v", err)
	}
	defer stack.Close()
	if *rxOffload {
		var off mipstack.RXChecksumOffload
		off.SetIPv4Header(true)
		off.SetTCP(true)
		stack.SetRXChecksumOffload(off)
	}
	if err := stack.Start(); err != nil {
		fatalf("start: %v", err)
	}
	forwarder, err := mipstack.NewTCPForwarder(stack, mipstack.TCPForwarderOptions{}, func(req *mipstack.TCPForwarderRequest) {
		if os.Getenv("MIPS_DEBUG") != "" {
			fmt.Fprintf(os.Stderr, "mipstack-proxy: syn %s -> %s\n", req.Flow().Source, req.Flow().Destination)
		}
		conn, err := req.Accept(context.Background())
		if err != nil {
			if os.Getenv("MIPS_DEBUG") != "" {
				fmt.Fprintf(os.Stderr, "mipstack-proxy: accept: %v\n", err)
			}
			return
		}
		up, err := net.Dial("tcp", *upstream)
		if err != nil {
			if os.Getenv("MIPS_DEBUG") != "" {
				fmt.Fprintf(os.Stderr, "mipstack-proxy: dial: %v\n", err)
			}
			conn.Close()
			return
		}
		_ = conn.SetNoDelay(true)
		if tcp, ok := up.(*net.TCPConn); ok {
			_ = tcp.SetNoDelay(true)
		}
		relay(conn, up)
	})
	if err != nil {
		fatalf("forwarder: %v", err)
	}
	defer forwarder.Close()

	inWait, err := newWaiter(fd, syscall.EPOLLIN)
	if err != nil {
		fatalf("epoll: %v", err)
	}
	outWait, err := newWaiter(fd, syscall.EPOLLOUT)
	if err != nil {
		fatalf("epoll: %v", err)
	}
	errc := make(chan error, 2)
	go func() { errc <- ingress(fd, stack, inWait) }()
	go func() { errc <- egress(fd, stack, outWait) }()
	fmt.Fprintf(os.Stderr, "mipstack-proxy: ready on %s mtu %d rx-offload %v -> %s\n", *tunName, *mtu, *rxOffload, *upstream)
	if err := <-errc; err != nil && !errors.Is(err, os.ErrClosed) && !errors.Is(err, io.EOF) && !errors.Is(err, syscall.EBADF) {
		fatalf("dataplane: %v", err)
	}
}

func relay(down, up net.Conn) {
	defer down.Close()
	defer up.Close()
	done := make(chan struct{}, 2)
	go func() {
		_, _ = io.Copy(down, up)
		closeWrite(down)
		done <- struct{}{}
	}()
	_, _ = io.Copy(up, down)
	closeWrite(up)
	<-done
}

func closeWrite(c net.Conn) {
	type writer interface{ CloseWrite() error }
	if w, ok := c.(writer); ok {
		_ = w.CloseWrite()
		return
	}
	_ = c.Close()
}

func ingress(fd int, stack *mipstack.Stack, ready *waiter) error {
	bufs := make([][]byte, stack.BatchSize())
	for i := range bufs {
		bufs[i] = make([]byte, 65536)
	}
	lens := make([]int, len(bufs))
	pkts := make([][]byte, len(bufs))
	for {
		n, err := readBatch(fd, bufs, lens, ready)
		if n > 0 && os.Getenv("MIPS_DEBUG") != "" {
			fmt.Fprintf(os.Stderr, "mipstack-proxy: in %d bytes0=%d %x\n", n, lens[0], bufs[0][:min(24, lens[0])])
		}
		if n > 0 {
			for i := 0; i < n; i++ {
				pkts[i] = bufs[i][:lens[i]]
			}
			wrote, werr := stack.Write(pkts[:n], 0)
			if werr != nil {
				return werr
			}
			if wrote != n {
				return fmt.Errorf("stack accepted %d of %d packets", wrote, n)
			}
		}
		if err != nil {
			return err
		}
	}
}

func egress(fd int, stack *mipstack.Stack, ready *waiter) error {
	bufs := make([][]byte, stack.BatchSize())
	sizes := make([]int, len(bufs))
	for i := range bufs {
		bufs[i] = make([]byte, 65536)
	}
	for {
		n, err := stack.Read(bufs, sizes, 0)
		if n > 0 && os.Getenv("MIPS_DEBUG") != "" {
			fmt.Fprintf(os.Stderr, "mipstack-proxy: out %d bytes0=%d %x\n", n, sizes[0], bufs[0][:min(24, sizes[0])])
		}
		for i := 0; i < n; i++ {
			if werr := writePacket(fd, bufs[i][:sizes[i]], ready); werr != nil {
				return werr
			}
		}
		if err != nil {
			return err
		}
	}
}

func readBatch(fd int, bufs [][]byte, lens []int, ready *waiter) (int, error) {
	for {
		n, err := syscall.Read(fd, bufs[0])
		if err == syscall.EINTR {
			continue
		}
		if err == syscall.EAGAIN || err == syscall.EWOULDBLOCK {
			if err = ready.wait(); err != nil {
				return 0, err
			}
			continue
		}
		if err != nil {
			return 0, err
		}
		lens[0] = n
		count := 1
		for count < len(bufs) {
			n, err = syscall.Read(fd, bufs[count])
			if err == syscall.EINTR {
				continue
			}
			if err == syscall.EAGAIN || err == syscall.EWOULDBLOCK {
				return count, nil
			}
			if err != nil {
				return count, err
			}
			lens[count] = n
			count++
		}
		return count, nil
	}
}

func writePacket(fd int, p []byte, ready *waiter) error {
	for len(p) > 0 {
		n, err := syscall.Write(fd, p)
		if err == syscall.EINTR {
			continue
		}
		if err == syscall.EAGAIN || err == syscall.EWOULDBLOCK {
			if err = ready.wait(); err != nil {
				return err
			}
			continue
		}
		if err != nil {
			return err
		}
		if n == 0 {
			return io.ErrShortWrite
		}
		p = p[n:]
	}
	return nil
}

type waiter struct {
	ep int
}

func newWaiter(fd int, events uint32) (*waiter, error) {
	ep, err := syscall.EpollCreate1(syscall.EPOLL_CLOEXEC)
	if err != nil {
		return nil, err
	}
	ev := syscall.EpollEvent{Events: events, Fd: int32(fd)}
	if err := syscall.EpollCtl(ep, syscall.EPOLL_CTL_ADD, fd, &ev); err != nil {
		syscall.Close(ep)
		return nil, err
	}
	return &waiter{ep: ep}, nil
}

func (w *waiter) wait() error {
	var got [1]syscall.EpollEvent
	for {
		_, err := syscall.EpollWait(w.ep, got[:], -1)
		if err == syscall.EINTR {
			continue
		}
		return err
	}
}

func openTUN(name string) (int, error) {
	fd, err := syscall.Open("/dev/net/tun", syscall.O_RDWR|syscall.O_CLOEXEC, 0)
	if err != nil {
		return -1, err
	}
	var req struct {
		name  [16]byte
		flags uint16
		_pad  [22]byte
	}
	copy(req.name[:], name)
	req.flags = 0x0001 | 0x1000 // IFF_TUN | IFF_NO_PI
	_, _, errno := syscall.Syscall(syscall.SYS_IOCTL, uintptr(fd), uintptr(0x400454ca), uintptr(unsafe.Pointer(&req)))
	if errno != 0 {
		syscall.Close(fd)
		return -1, errno
	}
	if err := syscall.SetNonblock(fd, true); err != nil {
		syscall.Close(fd)
		return -1, err
	}
	return fd, nil
}

func run(name string, args ...string) error {
	out, err := exec.Command(name, args...).CombinedOutput()
	if err != nil {
		return fmt.Errorf("%s %v: %v: %s", name, args, err, out)
	}
	return nil
}

func fatalf(format string, args ...any) {
	fmt.Fprintf(os.Stderr, "mipstack-proxy: "+format+"\n", args...)
	os.Exit(1)
}
