//! Client-scenario proxy process (`--tun-proxy`): a TUN device terminated by
//! zfstack, every accepted connection relayed to a fixed upstream address with
//! the kernel's TCP (like a proxy client's TUN inbound + direct outbound).
//!
//! ```text
//! app (kernel TCP) ──TUN zfcT──▶ reader thread ──▶ zfstack driver task ──▶ relay (task or splice) ──▶ kernel TCP upstream
//!                  ◀── writev ──────────────────── egress callback ◀──────
//! ```
//!
//! `--vnet-hdr` opens the TUN with `IFF_VNET_HDR` and `TUN_F_CSUM`, so neither
//! the kernel nor zfstack sums payload bytes on this hop (zfstack::offload).

use std::io;
use std::net::SocketAddr;
use std::os::fd::RawFd;

use tokio::io::AsyncWriteExt;
use zfstack::offload::{VirtioNetHdr, VIRTIO_NET_HDR_LEN};
use zfstack::pktpool::{PacketBuf, PacketPool};
use zfstack::tokio_adapter::{self, ResourceLimits, StreamConfig};
use zfstack::{IfaceConfig, IfaceId, OutPacket, PeerId, RxChecksum, SendResult, StackConfig};

use crate::tun;

pub const PROXY_TUN: &str = "zfcT";
pub const PROXY_TUN_ADDR: &str = "172.19.0.1/30";
/// Destinations routed into the TUN (outside the device subnet: sing-box answers
/// DNS on the device's next address).
pub const PROXY_ROUTE: &str = "198.18.0.0/15";

const IFF_VNET_HDR: libc::c_short = 0x4000;
const TUNSETOFFLOAD: libc::c_ulong = 0x4004_54d0;
const TUN_F_CSUM: libc::c_uint = 0x01;
const TUN_F_TSO4: libc::c_uint = 0x02;
const TUN_F_TSO6: libc::c_uint = 0x04;

/// Ingress batches handed to the driver are bounded in bytes, so a slow driver
/// pushes back on the reader (and the TUN queue) instead of buffering packets.
const BATCH_BYTES: usize = 512 * 1024;
const BATCH_QUEUE: usize = 4;

pub struct ProxyOpts {
    pub mtu: u16,
    pub upstream: SocketAddr,
    pub workers: usize,
    pub relay_buf: usize,
    pub client_profile: bool,
    /// Relay in the stack driver (`TcpStream::splice`) instead of a copy task.
    pub splice: bool,
    /// virtio-net header + checksum offload (+ TSO receive when `tso`).
    pub vnet_hdr: bool,
    pub tso: bool,
    /// Run the stack driver on its own current-thread runtime.
    pub driver_thread: bool,
    pub egress_rate: u64,
    pub adaptive_egress: bool,
}

fn write_packet(fd: RawFd, p: &OutPacket<'_>, vnet: bool) -> SendResult {
    let vh = VirtioNetHdr::for_packet(p).encode();
    let mut iov = [libc::iovec { iov_base: std::ptr::null_mut(), iov_len: 0 }; 4];
    let mut n = 0;
    let parts: [&[u8]; 4] = [if vnet { &vh[..] } else { &[] }, p.header, p.payload[0], p.payload[1]];
    for part in parts {
        if !part.is_empty() {
            iov[n] = libc::iovec { iov_base: part.as_ptr() as *mut _, iov_len: part.len() };
            n += 1;
        }
    }
    loop {
        let rc = unsafe { libc::writev(fd, iov.as_ptr(), n as i32) };
        if rc >= 0 {
            return SendResult::Accepted;
        }
        match io::Error::last_os_error().raw_os_error() {
            Some(libc::EINTR) => continue,
            // A TUN write only fails like this when the device is going away
            // or the kernel backlog overflows; drop like a lossy link.
            _ => return SendResult::Accepted,
        }
    }
}

type Batch = Vec<(PacketBuf, usize, RxChecksum)>;

/// Idle packet buffers kept (and charged) by the reader's pool.
const POOL_CACHED: usize = 128;

/// Diagnostic-only source identities, configured by the owned TUN case runner.
/// These are not authenticated peers and must not be used as a production ACL.
#[derive(Clone, Copy, Debug)]
struct PeerMode(bool);

impl PeerMode {
    fn from_value(value: Option<&str>) -> io::Result<Self> {
        match value {
            None => Ok(Self(false)),
            Some("4") => Ok(Self(true)),
            _ => Err(io::Error::new(io::ErrorKind::InvalidInput, "ZFBENCH_TUN_PEERS must be unset or 4")),
        }
    }

    fn classify(self, packet: &[u8]) -> Option<PeerId> {
        if !self.0 {
            return Some(PeerId(0));
        }
        // The stack still validates the complete IP/TCP packet and checksum.
        // Unknown addresses never fall back to the reader's accounting peer.
        if packet.len() >= 20 && packet[0] >> 4 == 4 && packet[12..15] == [172, 19, 1] && (1..=4).contains(&packet[15]) {
            Some(PeerId(packet[15] as u64))
        } else {
            None
        }
    }
}

struct PeerSamples {
    memory: Vec<zfstack::budget::MemoryHandle>,
    egress: std::sync::Arc<[std::sync::atomic::AtomicU64; 5]>,
}

/// Experimental settings. Both variables are required to enable trimming:
/// ZFBENCH_PACKET_POOL_TRIM_IDLE_MS and ZFBENCH_PACKET_POOL_TRIM_TARGET.
/// There are deliberately no experimental defaults.
#[cfg(feature = "packet-pool-idle-trim")]
#[derive(Clone, Copy, Debug)]
struct PacketPoolTrimConfig {
    idle: std::time::Duration,
    target: usize,
}

#[cfg(feature = "packet-pool-idle-trim")]
impl PacketPoolTrimConfig {
    fn from_env() -> io::Result<Option<Self>> {
        fn read(name: &str) -> io::Result<Option<String>> {
            match std::env::var(name) {
                Ok(value) => Ok(Some(value)),
                Err(std::env::VarError::NotPresent) => Ok(None),
                Err(std::env::VarError::NotUnicode(_)) => Err(io::Error::new(io::ErrorKind::InvalidInput, format!("{name} must be UTF-8"))),
            }
        }
        let idle = read("ZFBENCH_PACKET_POOL_TRIM_IDLE_MS")?;
        let target = read("ZFBENCH_PACKET_POOL_TRIM_TARGET")?;
        Self::from_values(idle.as_deref(), target.as_deref())
    }

    fn from_values(idle: Option<&str>, target: Option<&str>) -> io::Result<Option<Self>> {
        let invalid = |message| io::Error::new(io::ErrorKind::InvalidInput, message);
        match (idle, target) {
            (None, None) => Ok(None),
            (Some(idle), Some(target)) => {
                let idle_ms = idle.parse::<u64>().map_err(|_| invalid("ZFBENCH_PACKET_POOL_TRIM_IDLE_MS must be an unsigned integer"))?;
                let target = target.parse::<usize>().map_err(|_| invalid("ZFBENCH_PACKET_POOL_TRIM_TARGET must be an unsigned integer"))?;
                if target > POOL_CACHED {
                    return Err(invalid("ZFBENCH_PACKET_POOL_TRIM_TARGET exceeds POOL_CACHED"));
                }
                Ok(Some(Self { idle: std::time::Duration::from_millis(idle_ms), target }))
            }
            _ => Err(invalid("set both ZFBENCH_PACKET_POOL_TRIM_IDLE_MS and ZFBENCH_PACKET_POOL_TRIM_TARGET")),
        }
    }
}

/// A coarse quiet-period policy driven by the existing reader poll timeout.
/// The idle clock starts at the first timeout after a successful batch, so
/// detection includes that sampling delay. No per-packet clock is read.
#[cfg(feature = "packet-pool-idle-trim")]
struct PacketPoolIdleTrim {
    config: PacketPoolTrimConfig,
    idle_since: Option<std::time::Instant>,
}

#[cfg(feature = "packet-pool-idle-trim")]
impl PacketPoolIdleTrim {
    fn new(config: PacketPoolTrimConfig) -> Self {
        Self { config, idle_since: None }
    }

    fn note_activity(&mut self) {
        self.idle_since = None;
    }

    fn target_at_idle(&mut self, now: std::time::Instant) -> Option<usize> {
        let since = self.idle_since.get_or_insert(now);
        (now.saturating_duration_since(*since) >= self.config.idle).then_some(self.config.target)
    }
}

/// One coarse host timer per pool, independent of RX readiness and TX
/// diagnostics. A weak sender lets reader/driver shutdown end maintenance
/// without keeping the source channel alive. No catch-up timer bursts.
#[cfg(feature = "packet-pool-idle-trim")]
fn spawn_packet_pool_maintenance(pool: PacketPool, config: PacketPoolTrimConfig, source: tokio::sync::mpsc::WeakSender<Batch>) -> tokio::task::JoinHandle<()> {
    pool.trim_idle(std::time::Instant::now(), config.idle, config.target);
    tokio::spawn(async move {
        loop {
            tokio::time::sleep(std::time::Duration::from_secs(1)).await;
            if source.upgrade().is_none_or(|source| source.is_closed()) {
                break;
            }
            pool.trim_idle(std::time::Instant::now(), config.idle, config.target);
        }
    })
}

/// Blocking TUN reader: reads each packet straight into a pooled, charged
/// buffer (zero-copy ingress) and batches them for the driver.
fn reader(
    fd: RawFd,
    pool: PacketPool,
    vnet: bool,
    trust: bool,
    tx: tokio::sync::mpsc::Sender<Batch>,
    #[cfg(feature = "packet-pool-idle-trim")] mut idle_trim: Option<PacketPoolIdleTrim>,
) {
    let start = if vnet { VIRTIO_NET_HDR_LEN } else { 0 };
    let mut spare: Option<PacketBuf> = None;
    loop {
        let mut pfd = libc::pollfd { fd, events: libc::POLLIN, revents: 0 };
        let rc = unsafe { libc::poll(&mut pfd, 1, 1000) };
        if rc < 0 {
            if io::Error::last_os_error().raw_os_error() == Some(libc::EINTR) {
                continue;
            }
            return;
        }
        #[cfg(feature = "packet-pool-idle-trim")]
        if rc == 0 {
            if let Some(policy) = &mut idle_trim {
                if let Some(target) = policy.target_at_idle(std::time::Instant::now()) {
                    // EAGAIN retains one empty buffer in spare. Return that
                    // host-owned buffer before trimming, without touching any
                    // queued packet or payload owner retained by a slice.
                    drop(spare.take());
                    pool.trim_cached_to(target);
                }
                // In this opt-in mode, do not perform a doomed read after
                // a timeout or recreate spare immediately after cold trim.
                // Keep checking while cold: late slice drops can refill free.
                continue;
            }
        }
        let mut batch = Vec::with_capacity(16);
        let mut bytes = 0;
        while bytes < BATCH_BYTES && batch.len() < 256 {
            let mut b = match spare.take().or_else(|| pool.get()) {
                Some(b) => b,
                None => {
                    // Out of budget: let the driver drain before reading on.
                    std::thread::sleep(std::time::Duration::from_millis(1));
                    break;
                }
            };
            let room = b.spare_capacity_mut();
            let n = unsafe { libc::read(fd, room.as_mut_ptr() as *mut _, room.len()) };
            if n < 0 {
                spare = Some(b);
                match io::Error::last_os_error().raw_os_error() {
                    Some(libc::EAGAIN) => break,
                    Some(libc::EINTR) => continue,
                    _ => return,
                }
            }
            if n == 0 {
                return;
            }
            unsafe { b.set_len(n as usize) };
            let mut csum = if trust { RxChecksum::Trusted } else { RxChecksum::Verify };
            if vnet {
                let Some(h) = VirtioNetHdr::decode(&b) else { continue };
                csum = h.rx_checksum();
            }
            bytes += b.len();
            batch.push((b, start, csum));
        }
        #[cfg(feature = "packet-pool-idle-trim")]
        if !batch.is_empty() {
            if let Some(policy) = &mut idle_trim {
                policy.note_activity();
            }
        }
        if !batch.is_empty() && tx.blocking_send(batch).is_err() {
            return;
        }
    }
}

pub fn client_config(profile: bool) -> StackConfig {
    let mut cfg = if profile {
        StackConfig::client()
    } else {
        StackConfig::default()
    };
    crate::tx_tuning::TxTuning::from_env().expect("validated TX tuning").apply(&mut cfg);
    crate::trial_tuning::TrialTuning::from_env().expect("validated trial tuning").apply(&mut cfg);
    cfg
}

fn open_proxy_tun(vnet: bool, tso: bool) -> io::Result<RawFd> {
    if !vnet {
        return tun::open_tun(PROXY_TUN);
    }
    let fd = tun::open_tun_flags(PROXY_TUN, IFF_VNET_HDR)?;
    let mut flags = TUN_F_CSUM;
    if tso {
        flags |= TUN_F_TSO4 | TUN_F_TSO6;
    }
    if unsafe { libc::ioctl(fd, TUNSETOFFLOAD as _, flags as libc::c_ulong) } < 0 {
        let e = io::Error::last_os_error();
        unsafe { libc::close(fd) };
        return Err(e);
    }
    Ok(fd)
}

struct Stack {
    acceptor: tokio_adapter::Acceptor,
    // Dropping these stops the driver.
    _handle: tokio_adapter::StackHandle,
    _task: tokio_adapter::DriverTask,
}

// Explicit runtime-only experimental settings. No trim or sampler defaults.
// All five TX trim variables are required to opt in; SAMPLE_MS is independent.
const TX_TRIM_ENV: [&str; 5] = [
    "ZFBENCH_TX_CACHE_TRIM_CHECK_MS",
    "ZFBENCH_TX_CACHE_TRIM_WARM_MS",
    "ZFBENCH_TX_CACHE_TRIM_COLD_MS",
    "ZFBENCH_TX_CACHE_TRIM_LARGE",
    "ZFBENCH_TX_CACHE_TRIM_SMALL",
];
const TX_SAMPLE_ENV: &str = "ZFBENCH_TX_CACHE_SAMPLE_MS";

#[derive(Clone, Copy, Debug)]
struct TxCacheExperiment {
    trim: Option<zfstack::TxCacheTrimPolicy>,
    sample: Option<std::time::Duration>,
}

impl TxCacheExperiment {
    fn from_env() -> io::Result<Self> {
        fn read(name: &str) -> io::Result<Option<String>> {
            match std::env::var(name) {
                Ok(value) => Ok(Some(value)),
                Err(std::env::VarError::NotPresent) => Ok(None),
                Err(std::env::VarError::NotUnicode(_)) => Err(io::Error::new(io::ErrorKind::InvalidInput, format!("{name} must be UTF-8"))),
            }
        }
        let values: Vec<_> = TX_TRIM_ENV.iter().map(|name| read(name)).collect::<io::Result<_>>()?;
        let sample = read(TX_SAMPLE_ENV)?;
        Self::from_values([values[0].as_deref(), values[1].as_deref(), values[2].as_deref(), values[3].as_deref(), values[4].as_deref()], sample.as_deref())
    }

    fn from_values(values: [Option<&str>; 5], sample: Option<&str>) -> io::Result<Self> {
        fn milliseconds(value: &str, name: &str) -> io::Result<u64> {
            let ms = value.parse::<u64>().map_err(|_| io::Error::new(io::ErrorKind::InvalidInput, format!("{name} must be an unsigned integer")))?;
            if ms > u64::MAX / 1_000_000 {
                return Err(io::Error::new(io::ErrorKind::InvalidInput, format!("{name} exceeds the core nanosecond clock range")));
            }
            Ok(ms)
        }
        fn slots(value: &str, name: &str) -> io::Result<usize> {
            value.parse::<usize>().map_err(|_| io::Error::new(io::ErrorKind::InvalidInput, format!("{name} must be an unsigned integer")))
        }
        let supplied = values.iter().filter(|value| value.is_some()).count();
        let trim = match supplied {
            0 => None,
            5 => {
                let check = milliseconds(values[0].unwrap(), TX_TRIM_ENV[0])?;
                let warm = milliseconds(values[1].unwrap(), TX_TRIM_ENV[1])?;
                let cold = milliseconds(values[2].unwrap(), TX_TRIM_ENV[2])?;
                if check == 0 || cold == 0 || warm > cold {
                    return Err(io::Error::new(io::ErrorKind::InvalidInput, "TX cache trim needs positive CHECK_MS/COLD_MS and WARM_MS <= COLD_MS"));
                }
                Some(zfstack::TxCacheTrimPolicy {
                    check_interval: std::time::Duration::from_millis(check),
                    warm_after: std::time::Duration::from_millis(warm),
                    cold_after: std::time::Duration::from_millis(cold),
                    warm_large: slots(values[3].unwrap(), TX_TRIM_ENV[3])?,
                    warm_small: slots(values[4].unwrap(), TX_TRIM_ENV[4])?,
                })
            }
            _ => return Err(io::Error::new(io::ErrorKind::InvalidInput, "set all five ZFBENCH_TX_CACHE_TRIM_* variables or none")),
        };
        let sample = match sample {
            None => None,
            Some(value) => {
                let ms = milliseconds(value, TX_SAMPLE_ENV)?;
                if ms < 1000 {
                    return Err(io::Error::new(io::ErrorKind::InvalidInput, "ZFBENCH_TX_CACHE_SAMPLE_MS must be at least 1000 (coarse sampling)"));
                }
                Some(std::time::Duration::from_millis(ms))
            }
        };
        Ok(Self { trim, sample })
    }
}

fn spawn_tx_cache_sampler(
    handle: tokio_adapter::StackHandle,
    interval: std::time::Duration,
    packet_pool: PacketPool,
    peer_memory: zfstack::budget::MemoryHandle,
    limits: ResourceLimits,
    peers: Option<PeerSamples>,
) {
    tokio::spawn(async move {
        let start = std::time::Instant::now();
        loop {
            // One bounded control request per interval; no catch-up bursts.
            tokio::time::sleep(interval).await;
            let Some(snap) = handle.snapshot().await else { break };
            if let Some(peers) = &peers {
                let rows: Vec<_> = peers
                    .memory
                    .iter()
                    .enumerate()
                    .map(|(id, memory)| {
                        serde_json::json!({
                            "peer_id": id,
                            "physical_bytes": memory.peer_physical_used(),
                            "egress_packets": peers.egress[id].load(std::sync::atomic::Ordering::Relaxed),
                        })
                    })
                    .collect();
                eprintln!(
                    "ZFBENCH_PEERS {}",
                    serde_json::json!({
                        "elapsed_ms": start.elapsed().as_millis(),
                        "unix_ms": std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).unwrap().as_millis(),
                        "peers": rows,
                        "peer_limit_bytes": limits.peer_bytes,
                        "scope": "one shard; source 172.19.1.N maps to PeerId(N), N=1..4; peer 0 is the bounded reader pool; separate non-atomic reads",
                    })
                );
            }
            eprintln!(
                "ZFBENCH_CONNECTIONS {}",
                serde_json::json!({
                        "elapsed_ms": start.elapsed().as_millis(),
                        "close_counts": format!("{:?}", snap.closes),
                        "abort_counts": format!("{:?}", snap.aborts),
                        "physical_waiters": format!("{:?}", snap.waiters),
                        "failures_by_kind": format!("{:?}", snap.failures_by_kind),
                        "active_connections": snap.active_connections,
                        "adapter_streams": snap.adapter_streams,
                        "rst_sent": snap.stats.rst_sent,
                        "tail_connections": format!("{:?}", snap.tail_connections),
                })
            );
            // Separate low-frequency reads, not an atomic pool/global snapshot.
            // in_use includes the reader spare, queued packets and retained slices.
            let packet_cached = packet_pool.cached();
            let packet_in_use = packet_pool.in_use();
            let packet_buf_size = packet_pool.buf_size() as u64;
            let peer_physical_bytes = peer_memory.peer_physical_used();
            eprintln!(
                "ZFBENCH_TX_CACHE {}",
                serde_json::json!({
                    "elapsed_ms": start.elapsed().as_millis(),
                    "peer_id": 0,
                    "peer_physical_bytes": peer_physical_bytes,
                    "global_limit_bytes": peer_memory.global().high(),
                    "port_limit_bytes": limits.port_bytes,
                    "peer_limit_bytes": limits.peer_bytes,
                    "packet_cached": packet_cached,
                    "packet_in_use": packet_in_use,
                    "packet_buf_size": packet_buf_size,
                    "packet_free_lease_bytes_estimate": (packet_cached as u64).saturating_mul(packet_buf_size),
                    "packet_in_use_lease_bytes_estimate": (packet_in_use as u64).saturating_mul(packet_buf_size),
                    "reserve_failures_by_level": snap.failures_by_level,
                    "admission_debt_by_level": snap.admission_debt,
                    "record_waiters": snap.record_waiters,
                    "send_share_blocked": snap.share_blocked,
                    "senders": snap.senders,
                    "sample_coherence": "separate reads; validate conservation during settled quiet only",
                    "scope": if peers.is_some() { "one shard; PeerId(0) is reader storage only; see ZFBENCH_PEERS for four traffic peers" } else { "one tunproxy shard and one PeerId(0); not a multi-peer fairness test" },
                    "large_cached": snap.tx_cache.large_cached,
                    "small_cached": snap.tx_cache.small_cached,
                    "large_capacity": snap.tx_cache.large_capacity,
                    "small_capacity": snap.tx_cache.small_capacity,
                    "payload_bytes": snap.tx_cache.payload_bytes,
                    "descriptor_bytes": snap.tx_cache.descriptor_bytes,
                    "port_physical_bytes": snap.port_physical_bytes,
                    "global_reserved_bytes": snap.global_reserved_bytes,
                    "global_cached_bytes": snap.global_cached_bytes,
                    "maintenance_checks": snap.stats.tx_cache_maintenance_checks,
                    "idle_trims": snap.stats.tx_cache_idle_trims,
                    "idle_trimmed_blocks": snap.stats.tx_cache_idle_trimmed_blocks,
                    "idle_trimmed_bytes": snap.stats.tx_cache_idle_trimmed_bytes,
                    "pressure_reclaims": snap.stats.cache_reclaims,
                    "pressure_reclaimed_bytes": snap.stats.cache_reclaimed_bytes,
                    "driver_runs": snap.stats.runs,
                })
            );
        }
    });
}

// Benchmark-host configuration only. These values are ceilings, not usage.
// No overrides preserves the existing from_system() and equal-share behavior.
const BENCH_BUDGET_ENV: [&str; 3] = ["ZFBENCH_GLOBAL_BYTES", "ZFBENCH_PORT_BYTES", "ZFBENCH_PEER_BYTES"];

#[derive(Clone, Copy, Debug)]
struct BudgetDraft {
    bytes: Option<[u64; 3]>,
}

impl BudgetDraft {
    fn from_env() -> io::Result<Self> {
        let mut values: [Option<String>; 3] = Default::default();
        for (index, name) in BENCH_BUDGET_ENV.iter().enumerate() {
            values[index] = match std::env::var(name) {
                Ok(value) => Some(value),
                Err(std::env::VarError::NotPresent) => None,
                Err(_) => return Err(io::Error::new(io::ErrorKind::InvalidInput, format!("{name} must be UTF-8"))),
            };
        }
        Self::from_values([values[0].as_deref(), values[1].as_deref(), values[2].as_deref()])
    }

    fn from_values(values: [Option<&str>; 3]) -> io::Result<Self> {
        let count = values.iter().filter(|value| value.is_some()).count();
        if count == 0 {
            return Ok(Self { bytes: None });
        }
        if count != 3 {
            return Err(io::Error::new(io::ErrorKind::InvalidInput, "set all three ZFBENCH_*_BYTES variables or none"));
        }
        let mut bytes = [0; 3];
        for (index, value) in values.iter().enumerate() {
            bytes[index] = value
                .unwrap()
                .parse::<u64>()
                .map_err(|_| io::Error::new(io::ErrorKind::InvalidInput, format!("{} must be an unsigned integer", BENCH_BUDGET_ENV[index])))?;
        }
        if bytes[2] == 0 || bytes[2] > bytes[1] || bytes[1] > bytes[0] {
            return Err(io::Error::new(io::ErrorKind::InvalidInput, "budgets require 0 < peer <= port <= global"));
        }
        Ok(Self { bytes: Some(bytes) })
    }

    fn create(self, peer_mode: PeerMode) -> (std::sync::Arc<zfstack::budget::GlobalBudget>, ResourceLimits) {
        let global = match self.bytes {
            Some(bytes) => zfstack::budget::GlobalBudget::new(bytes[0]),
            None => zfstack::budget::GlobalBudget::from_system(),
        };
        let high = global.high();
        let bytes = self.bytes.unwrap_or([high; 3]);
        eprintln!(
            "ZFBENCH_BUDGET_CONFIG {}",
            serde_json::json!({
                "global_bytes": high, "port_bytes": bytes[1], "peer_bytes": bytes[2],
                "peer_id": 0, "peer_max_connections": u32::MAX,
                "scope": if peer_mode.0 { "four source-mapped traffic peers plus reader PeerId(0); ceilings are not usage" } else { "one tunproxy shard and one PeerId(0); configured ceilings are not usage" },
            })
        );
        let limits = ResourceLimits { port_bytes: bytes[1], peer_bytes: bytes[2], peer_max_connections: u32::MAX };
        (global, limits)
    }
}

/// Spawn the stack driver on the current runtime and start the TUN reader.
fn start_stack(
    o: &ProxyOpts,
    fd: RawFd,
    tx_experiment: TxCacheExperiment,
    bench_budget: BudgetDraft,
    peer_mode: PeerMode,
    #[cfg(feature = "packet-pool-idle-trim")] idle_trim: Option<PacketPoolTrimConfig>,
) -> Stack {
    let (mtu, vnet, tso) = (o.mtu, o.vnet_hdr, o.tso);
    // TSO super-segments can exceed the MTU (up to 64 KiB of IP packet).
    let max_pkt = if tso { 65535 } else { mtu as usize };
    // Without a vnet header the client profile trusts the local kernel's
    // checksums like sing-box does; with one, the header says per packet.
    let trust = o.client_profile && !vnet;
    let (tx, rx) = tokio::sync::mpsc::channel::<Batch>(BATCH_QUEUE);
    let (global, limits) = bench_budget.create(peer_mode);
    let egress_counts = peer_mode.0.then(|| std::sync::Arc::new(std::array::from_fn::<_, 5, _>(|_| std::sync::atomic::AtomicU64::new(0))));
    let counts = egress_counts.clone();
    let egress = move |_: IfaceId, peer: PeerId, p: &OutPacket<'_>| {
        if let Some(counts) = &counts {
            if let Some(count) = counts.get(peer.0 as usize) {
                count.fetch_add(1, std::sync::atomic::Ordering::Relaxed);
            }
        }
        write_packet(fd, p, vnet)
    };
    let (handle, acceptor, ids, task, (memory, peer_samples)) = tokio_adapter::spawn_with_source_factory(
        client_config(o.client_profile),
        StreamConfig::default(),
        vec![IfaceConfig { mtu }],
        move |shard| {
            if let Some(policy) = tx_experiment.trim {
                shard.set_tx_cache_trim(Some(policy)).expect("validated experimental TX cache trim");
            }
            let samples = egress_counts.map(|egress| PeerSamples { memory: (0..=4).map(|id| shard.memory_handle(PeerId(id))).collect(), egress });
            (egress, (shard.memory_handle(PeerId(0)), samples))
        },
        global,
        limits,
        rx,
        move |shard, now, batch: Batch| {
            for (b, start, csum) in batch {
                if let Some(packet) = b.get(start..) {
                    if let Some(peer) = peer_mode.classify(packet) {
                        if peer_mode.0 {
                            // Reader buffers belong to peer 0. Borrowing copies
                            // retained payload into the actual peer's ledger,
                            // matching zfc's shared external RX-pool contract.
                            shard.ingress_borrowed_with(now, IfaceId(0), peer, packet, csum);
                        } else {
                            shard.ingress_buf(now, IfaceId(0), peer, b, start, csum);
                        }
                    }
                }
            }
        },
    );
    handle.set_iface_egress_rate(ids[0], o.egress_rate);
    if o.adaptive_egress { handle.set_iface_adaptive_egress(ids[0], true); }
    let sample_memory = tx_experiment.sample.map(|_| memory.clone());
    let pool = PacketPool::new(memory, max_pkt + if vnet { VIRTIO_NET_HDR_LEN } else { 0 }, POOL_CACHED);
    #[cfg(feature = "packet-pool-idle-trim")]
    if let Some(config) = idle_trim {
        spawn_packet_pool_maintenance(pool.clone(), config, tx.downgrade());
    }
    let sample_packet_pool = tx_experiment.sample.map(|_| pool.clone());
    assert_eq!(ids[0], IfaceId(0));
    if vnet {
        handle.set_iface_tx_checksum_offload(ids[0], true);
    }
    if tso {
        handle.set_iface_tso(ids[0], 65_000);
    }
    std::thread::Builder::new()
        .name("zfc-tun-rx".into())
        .spawn(move || {
            #[cfg(feature = "packet-pool-idle-trim")]
            reader(fd, pool, vnet, trust, tx, idle_trim.map(PacketPoolIdleTrim::new));
            #[cfg(not(feature = "packet-pool-idle-trim"))]
            reader(fd, pool, vnet, trust, tx);
        })
        .unwrap();
    if let Some(interval) = tx_experiment.sample {
        spawn_tx_cache_sampler(handle.clone(), interval, sample_packet_pool.unwrap(), sample_memory.unwrap(), limits, peer_samples);
    }
    Stack { acceptor, _handle: handle, _task: task }
}

pub fn run(o: ProxyOpts) -> io::Result<()> {
    // Validate budget settings before any TUN deletion, creation or configuration.
    let bench_budget = BudgetDraft::from_env()?;
    let peer_value = std::env::var("ZFBENCH_TUN_PEERS").map(Some).or_else(|error| match error {
        std::env::VarError::NotPresent => Ok(None),
        _ => Err(io::Error::new(io::ErrorKind::InvalidInput, "ZFBENCH_TUN_PEERS must be UTF-8")),
    })?;
    let peer_mode = PeerMode::from_value(peer_value.as_deref())?;
    // Validate all TX settings before deleting, creating or configuring any TUN.
    let tx_experiment = TxCacheExperiment::from_env()?;
    // Reject incomplete experimental settings before changing the TUN device.
    #[cfg(feature = "packet-pool-idle-trim")]
    let idle_trim = PacketPoolTrimConfig::from_env()?;
    tun::sh_quiet(&format!("ip link del {PROXY_TUN}"));
    let fd = open_proxy_tun(o.vnet_hdr, o.tso)?;
    tun::sh(&format!("ip addr add {PROXY_TUN_ADDR} dev {PROXY_TUN}"))?;
    tun::sh(&format!("ip link set {PROXY_TUN} mtu {} txqueuelen 500 up", o.mtu))?;
    let rt = tokio::runtime::Builder::new_multi_thread().worker_threads(o.workers).enable_all().build()?;
    let (upstream, relay_buf, splice) = (o.upstream, o.relay_buf, o.splice);
    let stack = if o.driver_thread {
        // The driver gets a current-thread runtime of its own; spliced
        // sockets move to its reactor, so relay I/O never leaves that thread.
        let (stx, srx) = std::sync::mpsc::channel();
        std::thread::Builder::new().name("zfc-driver".into()).spawn(move || {
            let drt = tokio::runtime::Builder::new_current_thread().enable_all().build().unwrap();
            drt.block_on(async move {
                #[cfg(feature = "packet-pool-idle-trim")]
                let stack = start_stack(&o, fd, tx_experiment, bench_budget, peer_mode, idle_trim);
                #[cfg(not(feature = "packet-pool-idle-trim"))]
                let stack = start_stack(&o, fd, tx_experiment, bench_budget, peer_mode);
                let _ = stx.send(stack);
                std::future::pending::<()>().await
            })
        })?;
        srx.recv().map_err(|_| io::Error::other("driver thread failed"))?
    } else {
        let _g = rt.enter();
        #[cfg(feature = "packet-pool-idle-trim")]
        let stack = start_stack(&o, fd, tx_experiment, bench_budget, peer_mode, idle_trim);
        #[cfg(not(feature = "packet-pool-idle-trim"))]
        let stack = start_stack(&o, fd, tx_experiment, bench_budget, peer_mode);
        stack
    };
    let mut acceptor = stack.acceptor;
    eprintln!("zfbench tun-proxy: ready on {PROXY_TUN} splice {splice} -> {upstream}");
    rt.block_on(async move {
        while let Some(mut down) = acceptor.accept().await {
            tokio::spawn(async move {
                let Ok(mut up) = tokio::net::TcpStream::connect(upstream).await else {
                    let _ = down.shutdown().await;
                    return;
                };
                let _ = up.set_nodelay(true);
                if splice {
                    if let Err(error) = down.splice(up).await {
                        eprintln!("zfbench tun-proxy: splice failed: {error}");
                    }
                } else {
                    let _ = tokio::io::copy_bidirectional_with_sizes(&mut down, &mut up, relay_buf, relay_buf).await;
                }
            });
        }
    });
    Ok(())
}

#[cfg(test)]
mod tx_cache_experiment_tests {
    use super::*;

    #[test]
    fn diagnostic_peers_are_distinct_and_unknown_sources_are_not_admitted() {
        let on = PeerMode::from_value(Some("4")).unwrap();
        let off = PeerMode::from_value(None).unwrap();
        let mut packet = [0u8; 20];
        packet[0] = 0x45;
        packet[12..15].copy_from_slice(&[172, 19, 1]);
        for id in 1..=4 {
            packet[15] = id;
            assert_eq!(on.classify(&packet), Some(PeerId(id as u64)));
            assert_eq!(off.classify(&packet), Some(PeerId(0)));
        }
        for id in [0, 5, 255] {
            packet[15] = id;
            assert_eq!(on.classify(&packet), None);
        }
        packet[15] = 1;
        packet[12] = 127;
        assert_eq!(on.classify(&packet), None);
        packet[12] = 172;
        packet[0] = 0x60;
        assert_eq!(on.classify(&packet), None);
        assert_eq!(on.classify(&packet[..16]), None);
        for value in ["", "0", "1", "5", "04"] {
            assert!(PeerMode::from_value(Some(value)).is_err());
        }
    }

    #[test]
    fn tx_settings_are_explicit_and_sampler_can_observe_disabled_policy() {
        let off = TxCacheExperiment::from_values([None; 5], None).unwrap();
        assert!(off.trim.is_none() && off.sample.is_none());
        let observed_off = TxCacheExperiment::from_values([None; 5], Some("1000")).unwrap();
        assert!(observed_off.trim.is_none());
        assert_eq!(observed_off.sample, Some(std::time::Duration::from_secs(1)));
        let on = TxCacheExperiment::from_values([Some("10000"), Some("20000"), Some("90000"), Some("2"), Some("1")], None).unwrap();
        let trim = on.trim.unwrap();
        assert_eq!(trim.check_interval, std::time::Duration::from_secs(10));
        assert_eq!(trim.warm_after, std::time::Duration::from_secs(20));
        assert_eq!(trim.cold_after, std::time::Duration::from_secs(90));
        assert_eq!((trim.warm_large, trim.warm_small), (2, 1));
    }

    #[test]
    fn malformed_or_partial_tx_settings_are_rejected_without_device_access() {
        let valid = [Some("10000"), Some("20000"), Some("90000"), Some("2"), Some("1")];
        for i in 0..5 {
            let mut partial = valid;
            partial[i] = None;
            assert!(TxCacheExperiment::from_values(partial, None).is_err());
            let mut malformed = valid;
            malformed[i] = Some("bad");
            assert!(TxCacheExperiment::from_values(malformed, None).is_err());
        }
        for values in [
            [Some("0"), Some("20"), Some("90"), Some("2"), Some("1")],
            [Some("10"), Some("91"), Some("90"), Some("2"), Some("1")],
            [Some("10"), Some("0"), Some("0"), Some("2"), Some("1")],
            [Some("18446744073709551615"), Some("20"), Some("90"), Some("2"), Some("1")],
            [Some("10"), Some("20"), Some("90"), Some("-1"), Some("1")],
        ] {
            assert!(TxCacheExperiment::from_values(values, None).is_err());
        }
        for sample in ["0", "999", "bad", "18446744073709551615"] {
            assert!(TxCacheExperiment::from_values([None; 5], Some(sample)).is_err());
        }
    }
}

#[cfg(all(test, feature = "packet-pool-idle-trim"))]
mod idle_trim_tests {
    use super::*;

    #[tokio::test]
    async fn maintenance_runs_during_traffic_and_does_not_keep_reader_alive() {
        use std::time::Duration;
        let global = zfstack::budget::GlobalBudget::new(1 << 20);
        let mut budget = zfstack::budget::Budget::new(global.clone());
        let pool = PacketPool::new(budget.memory_handle(PeerId(0)), 4096, 8);
        drop((0..8).map(|_| pool.get().unwrap()).collect::<Vec<_>>());
        let (tx, mut rx) = tokio::sync::mpsc::channel::<Batch>(1);
        let task = spawn_packet_pool_maintenance(pool.clone(), PacketPoolTrimConfig { idle: Duration::from_millis(500), target: 0 }, tx.downgrade());
        // Never let RX become quiet for the reader's one-second poll timeout.
        tokio::time::timeout(Duration::from_secs(5), async {
            while pool.cached() > 1 {
                drop(pool.get().unwrap());
                tokio::time::sleep(Duration::from_millis(50)).await;
            }
        })
        .await
        .unwrap();
        assert_eq!(global.reserved(), 4096);
        drop(tx);
        assert!(tokio::time::timeout(Duration::from_millis(100), rx.recv()).await.unwrap().is_none());
        tokio::time::timeout(Duration::from_secs(3), task).await.unwrap().unwrap();
        drop(pool);
        assert_eq!(global.reserved(), 0);
    }

    #[test]
    fn experimental_settings_require_explicit_values() {
        assert!(PacketPoolTrimConfig::from_values(None, None).unwrap().is_none());
        assert!(PacketPoolTrimConfig::from_values(Some("20"), None).is_err());
        assert!(PacketPoolTrimConfig::from_values(None, Some("0")).is_err());
        assert!(PacketPoolTrimConfig::from_values(Some("bad"), Some("0")).is_err());
        assert!(PacketPoolTrimConfig::from_values(Some("20"), Some("bad")).is_err());
        assert!(PacketPoolTrimConfig::from_values(Some("20"), Some("129")).is_err());
        let config = PacketPoolTrimConfig::from_values(Some("20"), Some("0")).unwrap().unwrap();
        assert_eq!(config.idle, std::time::Duration::from_millis(20));
        assert_eq!(config.target, 0);
    }

    #[test]
    fn idle_deadline_resets_on_activity_and_remains_due_while_cold() {
        let idle = std::time::Duration::from_millis(20);
        let mut policy = PacketPoolIdleTrim::new(PacketPoolTrimConfig { idle, target: 1 });
        let start = std::time::Instant::now();
        assert_eq!(policy.target_at_idle(start), None);
        assert_eq!(policy.target_at_idle(start + idle - std::time::Duration::from_nanos(1)), None);
        assert_eq!(policy.target_at_idle(start + idle), Some(1));
        assert_eq!(policy.target_at_idle(start + idle + idle), Some(1));
        policy.note_activity();
        let next = start + idle + idle;
        assert_eq!(policy.target_at_idle(next), None);
        assert_eq!(policy.target_at_idle(next + idle), Some(1));
    }

    #[test]
    fn cold_tick_returns_spare_and_reclaims_late_returns() {
        let global = zfstack::budget::GlobalBudget::new(1 << 20);
        let mut budget = zfstack::budget::Budget::new(global.clone());
        let pool = PacketPool::new(budget.memory_handle(PeerId(0)), 4096, 2);
        let late = pool.get().unwrap();
        let mut spare = Some(pool.get().unwrap());
        let idle = std::time::Duration::from_millis(20);
        let mut policy = PacketPoolIdleTrim::new(PacketPoolTrimConfig { idle, target: 0 });
        let start = std::time::Instant::now();
        assert_eq!(policy.target_at_idle(start), None);
        let target = policy.target_at_idle(start + idle).unwrap();
        drop(spare.take());
        assert_eq!(pool.trim_cached_to(target), 1);
        assert_eq!(pool.in_use(), 1);
        assert_eq!(global.reserved(), 4096);
        drop(late);
        assert_eq!(pool.cached(), 1);
        let target = policy.target_at_idle(start + idle + idle).unwrap();
        assert_eq!(pool.trim_cached_to(target), 1);
        assert_eq!(global.reserved(), 0);
        assert_eq!(budget.physical_used(), 0);
    }
}

#[cfg(test)]
mod budget_draft_tests {
    use super::*;

    #[test]
    fn absent_values_preserve_default_and_explicit_values_are_exact() {
        assert!(BudgetDraft::from_values([None; 3]).unwrap().bytes.is_none());
        assert_eq!(BudgetDraft::from_values([Some("67108864"), Some("8388608"), Some("4194304")]).unwrap().bytes, Some([67108864, 8388608, 4194304]));
    }

    #[test]
    fn invalid_values_are_rejected_before_any_tun_operation() {
        let valid = [Some("67108864"), Some("8388608"), Some("4194304")];
        for index in 0..3 {
            let mut partial = valid;
            partial[index] = None;
            assert!(BudgetDraft::from_values(partial).is_err());
            for bad in ["bad", "", "-1", "18446744073709551616"] {
                let mut malformed = valid;
                malformed[index] = Some(bad);
                assert!(BudgetDraft::from_values(malformed).is_err());
            }
        }
        for values in [
            [Some("0"), Some("0"), Some("0")],
            [Some("4194304"), Some("2097152"), Some("0")],
            [Some("1048576"), Some("2097152"), Some("1048576")],
            [Some("4194304"), Some("1048576"), Some("2097152")],
        ] {
            assert!(BudgetDraft::from_values(values).is_err());
        }
    }
}
