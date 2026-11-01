use std::collections::HashSet;
use std::io;
use std::net::SocketAddr;
use std::os::fd::{AsRawFd, FromRawFd, OwnedFd};
use std::path::{Path, PathBuf};
use std::time::{Duration, Instant};

use clap::Parser;
use serde_json::{json, Value};
use zfstack::budget::{AllocationKind, GlobalBudget, Tier};
use zfstack::{wire, ConnId, Event, IfaceConfig, IfaceId, OutPacket, PeerId, ReadResult, SendResult, Shard, StackConfig, State, WriteResult};

const MTU: u16 = 1420;
const LIMIT: u64 = 16 << 20;
const PEER: PeerId = PeerId(1);

#[derive(Parser)]
struct Opts {
    #[arg(long)]
    side: String,
    #[arg(long)]
    control: PathBuf,
    #[arg(long)]
    pressure: bool,
    #[arg(long, default_value_t = true, action = clap::ArgAction::Set)]
    timestamps: bool,
}

fn atomic_json(path: &Path, value: &Value) {
    let tmp = path.with_extension("tmp");
    std::fs::write(&tmp, format!("{value}\n")).unwrap();
    std::fs::rename(tmp, path).unwrap();
}

fn open_tun() -> io::Result<OwnedFd> {
    #[repr(C)]
    struct IfReq {
        name: [u8; libc::IFNAMSIZ],
        flags: libc::c_short,
        pad: [u8; 22],
    }
    let raw = unsafe { libc::open(c"/dev/net/tun".as_ptr(), libc::O_RDWR | libc::O_NONBLOCK | libc::O_CLOEXEC) };
    if raw < 0 {
        return Err(io::Error::last_os_error());
    }
    let fd = unsafe { OwnedFd::from_raw_fd(raw) };
    let mut req = IfReq { name: [0; libc::IFNAMSIZ], flags: 0x0001 | 0x1000, pad: [0; 22] };
    req.name[..6].copy_from_slice(b"i100t0");
    if unsafe { libc::ioctl(raw, 0x4004_54ca as libc::c_ulong, &mut req) } < 0 {
        return Err(io::Error::last_os_error());
    }
    Ok(fd)
}

#[derive(Default)]
struct SinkStats {
    packets: u64,
    max_bytes: usize,
    full: u64,
    errors: Vec<i32>,
    events: Vec<Value>,
    seen: HashSet<u32>,
}

impl SinkStats {
    fn send(&mut self, fd: &OwnedFd, pkt: &OutPacket<'_>, elapsed_ms: u128) -> SendResult {
        let bytes = pkt.to_vec();
        self.max_bytes = self.max_bytes.max(bytes.len());
        assert!(bytes.len() <= MTU as usize, "packet exceeds interface MTU");
        assert_eq!(pkt.gso_size, 0, "this diagnostic uses ordinary TUN packets");
        let ip = wire::parse_ip(&bytes).unwrap();
        let tcp = wire::parse_tcp(&bytes, &ip).unwrap();
        let payload = ip.l4_len - tcp.data_off;
        let n = unsafe { libc::write(fd.as_raw_fd(), bytes.as_ptr().cast(), bytes.len()) };
        if n != bytes.len() as isize {
            let error = io::Error::last_os_error();
            if error.kind() == io::ErrorKind::WouldBlock || error.kind() == io::ErrorKind::Interrupted {
                self.full += 1;
            } else {
                self.errors.push(error.raw_os_error().unwrap_or(-1));
            }
            return SendResult::Full;
        }
        self.packets += 1;
        let retransmit = payload > 0 && !self.seen.insert(tcp.seq.0);
        assert!(self.events.len() < 4096, "unexpected packet storm");
        self.events.push(json!({"ms":elapsed_ms,"bytes":bytes.len(),"payload":payload,
            "seq":tcp.seq.0,"ack":tcp.ack.0,"sack_blocks":tcp.opts.sack_blocks().len(),"retransmit":retransmit}));
        SendResult::Accepted
    }
}

fn snapshot(shard: &Shard, id: Option<ConnId>, sink: &SinkStats, phase: &str, received: usize, expected: usize, held: u64) -> Value {
    let info = id.and_then(|id| shard.info(id)).map(|i| {
        json!({
            "state":format!("{:?}",i.state),"mss":i.mss,"tx_queued":i.tx_queued,"tx_unsent":i.tx_unsent,
            "rx_queued":i.rx_queued,"ooo_bytes":i.ooo_bytes,"pipe":i.pipe,"lost":i.lost,"sacked":i.sacked,
            "rto_count":i.stats.rto_count,"bytes_retrans":i.stats.bytes_retrans,"dropped_no_mem":i.stats.dropped_no_mem,
            "rx_progress_packets":i.stats.rx_progress_packets,"bytes_sent":i.stats.bytes_sent,"ooo_segs":i.stats.ooo_segs
        })
    });
    json!({"phase":phase,"info":info,"received":received,"expected":expected,"held_bytes":held,
        "reserved":shard.budget().global().reserved(),"high":LIMIT,"record_waiters":shard.record_waiters(),
        "send_record_failures":shard.budget().stats().failures().send_record,"runs":shard.stats().runs,
        "sink_packets":sink.packets,"max_packet_bytes":sink.max_bytes,"sink_full":sink.full,"sink_errors":sink.errors})
}

fn main() {
    let opts = Opts::parse();
    assert!(opts.side == "a" || opts.side == "b");
    assert_eq!(std::env::var("QA_NETNS_ISOLATED").as_deref(), Ok("1"));
    let active = opts.side == "a";
    let status = opts.control.join(format!("{}.status.json", opts.side));
    let local: SocketAddr = if active { "10.100.0.1:40000" } else { "10.100.0.2:5201" }.parse().unwrap();
    let remote: SocketAddr = if active { "10.100.0.2:5201" } else { "10.100.0.1:40000" }.parse().unwrap();
    let byte = if active { 0x5a } else { 0xa5 };
    let peer_byte = if active { 0xa5 } else { 0x5a };
    let fd = open_tun().unwrap();
    let global = GlobalBudget::new(LIMIT);
    let cfg = StackConfig { pacing: false, timestamps: opts.timestamps, ..StackConfig::default() };
    let mut shard = Shard::with_budget_seeded(cfg, global.clone(), if active { 10001 } else { 10002 });
    shard.set_budget_limits(LIMIT, LIMIT, 8);
    let iface = shard.add_iface(IfaceConfig { mtu: MTU });
    let origin = Instant::now();
    let mut id = None;
    let mut sink = SinkStats::default();
    let mut phase = "setup";
    let mut blocker = None;
    let mut held = 0;
    let mut received = 0;
    let mut expected = 0;
    let mut tun_received = 0;
    let mut pre_recovery = Value::Null;
    let mut last_status = Instant::now() - Duration::from_secs(1);
    let mut input = [0; 65536];
    while origin.elapsed() < Duration::from_secs(15) && !opts.control.join("finish").exists() {
        let now = zfstack::Instant::from_nanos(origin.elapsed().as_nanos() as u64 + 1_000_000_000);
        if phase == "setup" && opts.control.join("network-ready").exists() {
            if active {
                id = Some(shard.connect(now, iface, PEER, local, remote));
            }
            phase = "handshake";
        }
        loop {
            let n = unsafe { libc::read(fd.as_raw_fd(), input.as_mut_ptr().cast(), input.len()) };
            if n < 0 {
                let error = io::Error::last_os_error();
                assert!(matches!(error.kind(), io::ErrorKind::WouldBlock | io::ErrorKind::Interrupted), "TUN read: {error}");
                break;
            }
            assert!(n > 0);
            tun_received += 1;
            shard.ingress_borrowed(now, iface, PEER, &input[..n as usize]);
        }
        while let Some(event) = shard.poll_event() {
            match event {
                Event::Accepted(conn) | Event::Connected(conn) => {
                    id = Some(conn);
                    assert_eq!(shard.info(conn).unwrap().state, State::Established);
                    phase = "connected";
                }
                Event::Closed(_, reason) => panic!("connection closed unexpectedly: {reason:?}"),
                _ => {}
            }
        }
        if phase == "connected" && opts.control.join("start").exists() {
            let conn = id.unwrap();
            let mss = shard.info(conn).unwrap().mss as usize;
            expected = mss + 8;
            let data = vec![byte; mss];
            assert_eq!(shard.write(conn, &data), WriteResult::Written(data.len()));
            shard.run(now, &mut |_: IfaceId, p: &OutPacket<'_>| sink.send(&fd, p, origin.elapsed().as_millis()));
            phase = "head-sent";
        }
        if phase == "head-sent" && opts.control.join("tails").exists() {
            let conn = id.unwrap();
            // Both full-sized heads must precede either OOO tail; otherwise
            // a faster peer can already have SACK options when sending its head.
            for _ in 0..7 {
                assert_eq!(shard.write(conn, &[byte]), WriteResult::Written(1));
                shard.run(now, &mut |_: IfaceId, p: &OutPacket<'_>| sink.send(&fd, p, origin.elapsed().as_millis()));
            }
            // The ninth write remains unsent until BOTH receive OOO tails exist.
            assert_eq!(shard.write(conn, &[byte]), WriteResult::Written(1));
            phase = "filled";
        }
        if phase == "filled" && opts.control.join("pressure").exists() {
            if opts.pressure {
                let ceiling = LIMIT - global.headroom(LIMIT, Tier::Drain);
                held = ceiling - global.reserved();
                blocker = Some(shard.memory_handle(PeerId(2)).try_allocate_kind(held, AllocationKind::SendRecord).unwrap());
            }
            phase = "blocked";
        }
        // Deliberately hold the scheduler only during the bounded setup barrier;
        // after pressure starts, every timer is driven using wall-clock time.
        if !matches!(phase, "filled" | "head-sent" | "setup") {
            shard.egress_released(iface);
            shard.run(now, &mut |_: IfaceId, p: &OutPacket<'_>| sink.send(&fd, p, origin.elapsed().as_millis()));
        }
        assert!(sink.errors.is_empty(), "TUN sink syscall failed: {:?}", sink.errors);
        if matches!(phase, "blocked" | "post-recovery") && opts.control.join("released").exists() {
            let conn = id.unwrap();
            loop {
                match shard.read(now, conn, &mut input) {
                    ReadResult::Data(n) => {
                        assert!(input[..n].iter().all(|b| *b == peer_byte), "payload corruption");
                        received += n;
                    }
                    ReadResult::WouldBlock => break,
                    other => panic!("unexpected read: {other:?}"),
                }
            }
            assert!(received <= expected);
            let i = shard.info(conn).unwrap();
            if received == expected && i.tx_queued == 0 && i.ooo_bytes == 0 && i.pipe == 0 {
                if phase == "blocked" {
                    pre_recovery = snapshot(&shard, id, &sink, phase, received, expected, held);
                    phase = "recovered";
                } else {
                    phase = "complete";
                }
            }
        }
        if phase == "recovered" && opts.control.join("resume").exists() {
            // The first flight must already be fully ACKed while the budget
            // blocker is held. Only then release it and verify connection reuse.
            drop(blocker.take());
            held = 0;
            assert_eq!(shard.write(id.unwrap(), &[byte; 256]), WriteResult::Written(256));
            expected += 256;
            phase = "post-recovery";
        }
        if last_status.elapsed() >= Duration::from_millis(10) {
            atomic_json(&status, &snapshot(&shard, id, &sink, phase, received, expected, held));
            last_status = Instant::now();
        }
        let wait_ms = shard.next_deadline().map_or(5, |d| ((d.as_nanos().saturating_sub(now.as_nanos()) / 1_000_000).min(5)) as i32);
        let mut poll = libc::pollfd { fd: fd.as_raw_fd(), events: libc::POLLIN, revents: 0 };
        unsafe { libc::poll(&mut poll, 1, wait_ms.max(1)) };
    }
    let mut result = snapshot(&shard, id, &sink, phase, received, expected, held);
    result["tun_received"] = json!(tun_received);
    result["sink_events"] = json!(sink.events);
    result["recovered_while_blocked"] = pre_recovery;
    result["elapsed_ms"] = json!(origin.elapsed().as_millis());
    drop(shard);
    drop(blocker);
    result["reserved_after_drop"] = json!(global.reserved());
    assert_eq!(global.reserved(), 0);
    atomic_json(&opts.control.join(format!("{}.result.json", opts.side)), &result);
    println!("{result}");
    if phase != "complete" {
        std::process::exit(2);
    }
}
