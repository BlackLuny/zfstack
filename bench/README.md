# zfbench: S0 benchmark harness

This is a real-machine harness for the S0 experiments in
[`docs/design/0002-s0-benchmark-and-falsification.md`](../docs/design/0002-s0-benchmark-and-falsification.md).
It compares server-side TCP stacks (the kernel, the BlackLuny smoltcp fork and
zfstack) behind one emulated bottleneck on a single Linux machine. The kernel
has no netem on this VM, so a userspace link emulator stands in for it.

```
 root netns                                         zfbench process
┌───────────────────────┐   TUN zfbA     ┌─────────────────────────────┐
│ kernel TCP client     │  10.201.0.1/24 │ link emulator               │
│ (std sockets, N flows)│ ◀────────────▶ │  up thread   (client→server)│
└───────────────────────┘   MTU 1420     │  down thread (server→client)│
                                         └──────────┬──────────────────┘
                   --stack smoltcp-* / zfstack       │ crossbeam channels (batches of IP packets)
                   ┌─────────────────────────────────▼──┐
                   │ stack thread: UserStack adapter +   │  10.201.0.2:5201
                   │ embedded app server (same thread)   │
                   └─────────────────────────────────────┘
                   --stack kernel
                   ┌─────────────────────────────────────┐
                   │ netns zfbns: TUN zfbB 10.201.0.2/24 │
                   │ kernel TcpListener :5201 (threads)  │
                   └─────────────────────────────────────┘
```

## Build

```sh
cargo build --release -p zfbench   # kernel, smoltcp and zfstack modes (feature `zfstack` is on by default)
```

smoltcp comes from the git dependency
`https://github.com/BlackLuny/smoltcp` rev `8014f8b21e12faf89b3b453ceea32027344721af`
(0.13.1, features `socket-tcp-{cubic,bbr,reno}`, `medium-ip`, `proto-ipv4`).

## Running a single case (as root)

```sh
B=target/release/zfbench
$B --stack kernel --kernel-cc cubic --test down --rtt-ms 12 --secs 10
$B --stack smoltcp-bbr  --test down --rtt-ms 80 --queue-bdp 0.25 --loss 0.01 --flows 8
$B --stack smoltcp-cubic --test up --client-cc cubic
$B --stack smoltcp-cubic --test mixed --rtt-ms 80          # E8 latency baseline
$B --stack smoltcp-cubic --test connect --conns 2000 --concurrency 64   # E7
$B --cleanup                                                # remove leftover zfbA / zfbns
```

The run prints one JSON object to stdout, and to `--json FILE` if given. The
exit code is 1 on a correctness failure (a pattern mismatch, a wrong upload
byte count or a flow error) and 3 on a setup error. **Run one instance at a
time**, because every instance uses the same TUN and netns names.

Main flags (see `--help`):

| flag | default | meaning |
|---|---|---|
| `--stack` | smoltcp-cubic | `kernel`, `smoltcp-cubic`, `smoltcp-bbr`, `smoltcp-reno`, `zfstack-cubic`, `zfstack-bbr`, `zfstack-nopace` |
| `--test` | down | `down`, `up`, `mixed`, `connect` |
| `--rate-mbps` / `--rate-up-mbps` | 200 / same | bottleneck rate, counted in IP bytes; 0 = unlimited |
| `--rtt-ms` | 12 | base RTT; each direction gets RTT/2 of propagation delay |
| `--loss` / `--loss-up` | 0 / 0 | random loss probability on the down or up direction |
| `--queue-bdp` k | 2 | bottleneck queue = k × R × RTT bytes per direction (at least 1 MTU); `--queue-bytes` overrides it |
| `--flows` | 1 | parallel flows for down or up |
| `--secs` / `--warmup` | 10 / 2 | test length and the seconds discarded at the start |
| `--conns` / `--concurrency` / `--connect-timeout-ms` | 2000 / 64 / 3000 | connect test |
| `--sock-buf-kb` | 4096 | userspace stack socket rx and tx buffer size |
| `--listen-pool` | 8 | smoltcp: sockets kept in Listen |
| `--smol-pacing-backlog-us` | fork default (2000) | smoltcp `set_pacing_max_backlog_us` |
| `--smol-timestamps` | off | smoltcp TCP timestamps |
| `--kernel-cc` / `--client-cc` | system default | `TCP_CONGESTION` for the kernel server or kernel client sockets |
| `--seed` | 1 | seed for the loss RNG |

## Tests and the application protocol

Every request starts with a 16-byte header: `'Z'`, then a command byte, then
6 zero bytes, then a u64 LE parameter. The data streams use the pattern
`byte i == i % 251`, and the receiver checks it.

- **down** (`D`): the server streams the pattern until the peer closes or
  resets. Each client flow reads for `--secs` seconds and checks the pattern.
- **up** (`U`, param = flow index): each client flow writes the pattern for
  `--secs` seconds, then calls `shutdown(Write)`. The server checks the bytes
  and, on EOF, replies with the u64 byte count. The client compares that
  count with what it sent. Goodput is counted at the **server app**, which is
  the receiver.
- **mixed** (E8): first an RR connection (`E`, a 1 KiB message every 100 ms)
  runs alone for 3 s to give the idle baseline. Then one download flow
  saturates the link while RR continues. The report gives P50, P99 and P99.9
  RR RTT after warmup, and the difference from idle.
- **connect** (E7): K connections at concurrency C. Each one is connect, then
  the header, then waiting for EOF from the server (`C`). The report gives
  successes, failures split into timeout, refused and reset, and latency
  percentiles.

For down and up, the result includes the aggregate goodput for every second,
its mean after warmup, the number of zero-throughput seconds, and per-flow
totals and series. For upload it also includes the client's `tcp_info`
(retransmissions, cwnd and srtt).

## Link emulator semantics (`src/link.rs`)

Each direction has its own thread and its own parameters. They follow 0002
§2.1, with delay and bottleneck kept separate:

1. Random loss `p` is applied when a packet arrives and is counted as
   `random_drops`.
2. The bottleneck is a drop-tail queue. A packet is dropped when the bytes
   that are queued but not yet fully serialized, plus its own length, exceed
   `Lq`. Otherwise `departure = max(now, last_departure) + len·8/R`. These
   drops are counted as `bottleneck_drops` and `max_queue_bytes`.
3. Propagation: `deliver_at = departure + D`. This line has no limit and
   never drops.

The emulator waits with `ppoll` on the TUN fds and `recv_deadline` on the
channels, with timer slack set to 1 ns. It measures how late every packet is
delivered compared with `deliver_at`, and reports the mean, the max and a
histogram (`timing_late_*`). It also reports the drops that happen outside
the emulator: the TUN qdisc (replaced by `pfifo limit 10000`), the TUN
`tx_dropped` counter and the softnet backlog.

## The stack thread and the `UserStack` trait (`src/stack.rs`)

The stack thread blocks on the ingress channel with
`recv_deadline(next_deadline)`. When it wakes, it drains every queued batch,
calls `ingress` for each packet and `poll` once, then sends all the output
packets to the link as one batch. The thread measures its own CPU time with
`CLOCK_THREAD_CPUTIME_ID`, and the orchestrator samples it through
`pthread_getcpuclockid` for the measurement window. It also counts wakeups,
timer wakeups and how late each deadline was served.

The smoltcp adapter (`src/adapters/smoltcp.rs`) is set up the way a proxy
would use it:

- `Medium::Ip` device with MTU 1420 and `any_ip` on.
- The interface address is 10.201.0.2/24, with a default route via 10.201.0.1.
- A pool of `--listen-pool` sockets is kept in Listen, and it is refilled
  after every poll.
- Closed and TimeWait sockets are removed.
- Nagle is off, ack delay is the default, and the CC is chosen per mode. BBR
  keeps the fork's pacing defaults.

The app runs in the same thread through the transport-agnostic
`app::AppConn` state machine.

**zfstack** (`src/adapters/zfstack.rs`): one `Shard` with one iface (MTU 1420);
every packet is attributed to one WG peer. The app is driven from shard events
only (Accepted / Readable / Writable / Closed), so idle connections cost nothing
per poll. `zfstack-cubic` and `zfstack-bbr` pace (EDT, §7 of the design);
`zfstack-nopace` is CUBIC without pacing. `--sock-buf-kb` sets the receive
buffer ceiling and the in-flight cap. Stats include per-phase time
(ingress / run / app), recovery counters and limit timers.

## Output JSON (main fields)

- `config`, `env`: the parameters, kernel version, zfstack commit, smoltcp
  revision, `tcp_rmem`/`tcp_wmem` and the default CC.
- `ok`, `errors`: the correctness verdict.
- `results`: output specific to the test.
- `link.{down,up}`: counters for each direction (see above), plus `cpu_sec`
  for each link thread.
- `cpu`:
  - `stack_thread_window_sec` and `cpu_sec_per_effective_GB`: the stack
    thread's CPU over the measurement window, divided by the application
    bytes received in that window.
  - `link_*_window_sec`, `process_window_sec`.
  - `system_window`: from `/proc/stat`; see the caveats.
  - `kernel_server_threads_total_sec`: kernel mode only.
- `stack_thread`: wakeups (total, per second in the window, timer wakeups),
  deadline lateness, and `stack_stats` from the adapter. For smoltcp these
  are the summed `LossStats` (rto, fast_retransmit, sack_retransmit, ...),
  listener refills and max cwnd.

## Matrix and report (`run_matrix.py`)

```sh
sudo bench/run_matrix.py --name s0-e1                  # default matrix, see below
sudo bench/run_matrix.py --quick --name smoke          # 10 s, 1 rep, RTT 12/80, 1 flow
sudo bench/run_matrix.py --tests down --rtts 80 --queues 0.25 --losses 0,0.01 \
     --stacks kernel-cubic,smoltcp-cubic,smoltcp-bbr --reps 5 --name shallow80
bench/run_matrix.py --report-only bench/results/2026-09-24-s0-e1
```

The default matrix runs the stacks `kernel-cubic`, `smoltcp-cubic` and
`smoltcp-bbr` at 200 Mbps with these cells:

- down and up: RTT 1/12/80 ms, loss 0 and 1 %, queue k = 2 and 0.25, 1 and 8 flows;
- mixed: RTT 12/80, k = 2 and 0.25;
- connect: RTT 12, 2000 connections at concurrency 64.

Each cell runs 3 times at 30 s with 3 s of warmup, and the kernel client uses
`--client-cc cubic`. That is 477 runs, about 4.5 h. The run order goes
repetition, then cell, then stack, so drift is spread evenly across stacks.

- Each run is a fresh process, with `--cleanup` before and after it.
- Ctrl-C kills the child, cleans up and writes a partial report.
- `--resume` skips runs whose JSON already exists.

The raw JSON and a `.log` (stderr) for every run are written to
`bench/results/<date>-<name>/`, along with `meta.json` and `report.md`. The
report has one table per test and metric, with the stacks side by side as
**median [min–max]**, and a ratio column against `--baseline` (default
`smoltcp-cubic`). Crashed runs and correctness failures are marked ⚠. The
report ends with a section on emulator health.

In the matrix, the cell's random loss is applied to the **data** direction:
`--loss` for down and mixed, `--loss-up` for up.

Stack specs: `kernel` uses the system default CC (**this VM defaults to
bbr**). `kernel-<cc>` sets `TCP_CONGESTION` on the kernel server, for example
`kernel-cubic`. The others are `smoltcp-cubic|bbr|reno` and `zfstack-cubic|bbr|nopace`.

## WG link mode: two machines, real network (`src/wg.rs`, `src/wgmode.rs`)

The emulator mode above has no WireGuard and no real path. The WG link mode
replaces the emulator with a WireGuard tunnel over whatever network joins two
machines. It is meant for the WAN A/B of 0002 §2.3 and does not need zfc:
the tunnel uses boringtun's `Tunn`, the same type zfc's WG path uses (0001
§7.5), inside zfbench.

```
 client machine                                    server machine: zfbench --serve-wg
┌───────────────────────────────┐   UDP 51820    ┌───────────────────────────────────────────┐
│ kernel TCP client (N flows)   │ ◀────────────▶ │ userspace stacks: one thread does          │
│  └ zfbwg 10.201.0.1/24        │  (real path)   │   recvmmsg → boringtun decrypt → ingress    │
│    kernel WG, or boringtun    │                │   → poll → boringtun encrypt → sendmmsg     │
│    on a TUN (userspace)       │   TCP 5202     │ kernel baseline: zfbwgS 10.201.0.2/24       │
│ orchestrator ─────────────────┼─── control ───▶│   (kernel WG or boringtun bridge) + kernel  │
└───────────────────────────────┘                │   TcpListener                               │
                                                 └───────────────────────────────────────────┘
```

Server (one session at a time, runs until killed; every session gets fresh
WireGuard keys, a fresh stack and a fresh UDP socket):

```sh
export ZFBENCH_TOKEN=$(head -c 16 /dev/urandom | xxd -p)   # the same value on both machines
sudo -E target/release/zfbench --serve-wg --allow <client-ip>
#   --ctl-listen 0.0.0.0:5202 --wg-listen 0.0.0.0:51820 --server-wg auto|kernel|userspace
```

Client, one run or a whole matrix:

```sh
sudo -E target/release/zfbench --wg-server <server-ip> --stack zfstack-bbr --test down --flows 8 --secs 30
sudo -E bench/run_matrix.py --wg-server <server-ip> --name wan-218 \
     --stacks kernel-cubic,smoltcp-cubic,zfstack-cubic,zfstack-bbr --tests down,up,mixed,connect --reps 5
```

- **What travels over the control channel.** It is newline-delimited JSON
  on plain TCP outside the tunnel. `start` carries the stack options (the
  same `--stack`, `--sock-buf-kb`, `--kernel-cc` and smoltcp flags as the
  emulator mode) and the client's public key, and the reply carries the
  server's. `snap` returns the server's thread and machine CPU and the upload
  byte counters. The client calls it at the window marks and once a second in
  `up` tests, where the server is the receiver. `stop` returns the
  server-side result. Every request must carry the token, and `--allow`
  limits the client addresses. The server runs as root and creates
  interfaces, so open 5202 only to the client.
- **The emulator flags do not apply.** Rate, RTT, loss and queue are ignored.
  `config.rtt_ms` is the tunnel RTT, measured by TCP connect probes before
  the test: the minimum of probes 2–6, because the first probe includes the
  WireGuard handshake. `config.rate_mbps`, `loss` and `queue_bdp` are 0. On
  a lab link, shape the path with `tc` on the machines themselves.
- **WireGuard on each side.** `--client-wg` and `--server-wg` take `auto`
  (kernel WG if `ip link add type wireguard` and `wg` work, userspace
  otherwise), `kernel` or `userspace`. `--server-wg` only affects the kernel
  baseline, because the userspace stacks always run WireGuard on their own
  stack thread. The implementation used is recorded in `config.client_wg`
  and `config.server_wg`. For the 0002 setup (client on kernel WG) use a
  client with kernel WireGuard. On the 4-vCPU dev VM a userspace client used
  one full core at about 1.3 Gbps (decrypt and TUN write), so above roughly
  1 Gbps it becomes the bottleneck.
- **CPU accounting.**
  - `cpu.cpu_sec_per_effective_GB` for the userspace stacks is the server
    thread that does UDP, WireGuard, TCP and the app. That is closer to a
    production worker than the emulator mode, where there is no crypto.
    `link.server_wg.peer.{decap,encap}_sec` is the boringtun share.
  - The kernel baseline has no such thread: its TCP runs in softirq, and so
    does its WG when `server_wg` is `kernel`. Compare it through
    `cpu.server_system_busy_sec_per_effective_GB`, which is `/proc/stat`
    busy time on the server machine with the caveats below.
  - The client side is reported separately (`client_*`).
- **The crypto placement differs from zfc today.** Encrypting on the stack
  thread and sending with one `sendmmsg` per poll is the shape 0001 §7.5
  asks of Driver B. zfc currently hands packets back to a Tokio task to
  encrypt and send. That extra hop, and the pacing lateness it causes, is
  not modelled here yet.
- **Leftovers.** The client's `zfbwg`, and the server's `zfbwgS` in the
  kernel baseline, are removed at the end of a run and by `--cleanup`. A
  TUN-backed interface vanishes when its process dies.

## Caveats (read before trusting numbers)

- **Timing precision.** Deliveries are about 15–25 µs late on average under
  load at 200 Mbps; about 97 % arrive within 50 µs and about 99.5 % within
  200 µs. There are rare outliers of 1–6 ms from scheduling hiccups in the
  VM. Every run reports its own `timing_late_*` numbers; check them.
- **Harness RTT floor.** With RTT 0 and no rate limit, an RR exchange takes
  about 0.25–0.3 ms. That is the thread wakeups through TUN, the link threads
  and the stack thread on an idle VM. At the configured RTT, idle RR comes out
  at about RTT + 0.4–0.5 ms, which includes the serialization of 1 KiB at
  200 Mbps. Compare RR **differences**, not absolute values.
- **CPU contention (4 vCPU).** Everything shares 4 vCPUs: the kernel client
  threads, two link threads (roughly 0.3–0.5 core each at 200 Mbps, because
  they wake about once per packet), the stack thread, and softirq for the
  kernel mode. With 8 flows the client adds more threads. 500 Mbps and above
  or many flows will become CPU bound in the harness itself. Watch
  `cpu.process_window_sec` and `system_window.idle_sec`. Nothing is pinned
  to a CPU.
- **CPU accounting.** `cpu_sec_per_effective_GB` counts only the userspace
  stack thread: protocol plus app plus copies, the same thread as in
  production. The kernel mode has no equivalent. Its TCP work runs in
  softirq and syscalls, and `kernel_server_threads_total_sec` covers only the
  syscall side. `system_window` comes from tick-sampled `/proc/stat`, and on
  this VM it **under-reports** CPU for this workload of many short wakeups:
  it showed about 2.3 s busy while the per-thread clocks showed about 3.2 s.
  Do not use it for fine comparisons.
- **The smoltcp listener pool allocates full buffers.** Every listener gets
  4 MiB rx plus 4 MiB tx up front. That costs a lot in the connect test (a
  large share of the stack thread's CPU there), and with 8 listeners the pool
  refuses SYNs during bursts. That refusal is the H6 phenomenon, not a harness
  bug. If production starts with small buffers and grows them
  (`set_recv_capacity_ceiling` / `set_*_capacity`), this adapter does not
  model that yet.
- **Compared with the 0002 testbed (emulator mode).** There is no WireGuard
  layer; see the WG link mode for that. The rate
  counts inner IP bytes: MTU 1420, so the payload ceiling is about 194 Mbps
  without TCP timestamps and about 192.7 Mbps with them (the kernel uses
  them; smoltcp does not by default). There are no veths or qdiscs in the
  path, and TUN without `IFF_VNET_HDR` means there is no GSO/GRO. There is no
  reordering.
- **Kernel client buffers.** Kernel client buffers follow the system sysctls
  (`tcp_rmem` max 32 MiB and `tcp_wmem` max 4 MiB here). They are recorded in
  `env`.
