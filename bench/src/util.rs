//! Small helpers: CPU clocks, timer slack, RNG, percentiles, /proc readers.

use std::time::Duration;

fn ts_to_secs(ts: &libc::timespec) -> f64 {
    ts.tv_sec as f64 + ts.tv_nsec as f64 * 1e-9
}

/// CPU time (user+sys) consumed by the calling thread, in seconds.
pub fn thread_cpu_now() -> f64 {
    let mut ts: libc::timespec = unsafe { std::mem::zeroed() };
    unsafe { libc::clock_gettime(libc::CLOCK_THREAD_CPUTIME_ID, &mut ts) };
    ts_to_secs(&ts)
}

/// CPU time of another (live) thread of this process.
pub fn pthread_cpu(t: std::os::unix::thread::RawPthread) -> Option<f64> {
    let mut clk: libc::clockid_t = 0;
    let rc = unsafe { libc::pthread_getcpuclockid(t as libc::pthread_t, &mut clk) };
    if rc != 0 {
        return None;
    }
    let mut ts: libc::timespec = unsafe { std::mem::zeroed() };
    if unsafe { libc::clock_gettime(clk, &mut ts) } != 0 {
        return None;
    }
    Some(ts_to_secs(&ts))
}

/// CPU time of the whole process (all threads), seconds.
pub fn process_cpu_now() -> f64 {
    let mut ts: libc::timespec = unsafe { std::mem::zeroed() };
    unsafe { libc::clock_gettime(libc::CLOCK_PROCESS_CPUTIME_ID, &mut ts) };
    ts_to_secs(&ts)
}

/// Make timed sleeps of the calling thread as precise as the kernel allows
/// (default timer slack is 50 µs, which would dominate the emulator error).
pub fn set_timerslack_ns(ns: u64) {
    unsafe {
        libc::prctl(libc::PR_SET_TIMERSLACK, ns as libc::c_ulong, 0, 0, 0);
    }
}

pub fn dur_to_timespec(d: Duration) -> libc::timespec {
    libc::timespec { tv_sec: d.as_secs() as libc::time_t, tv_nsec: d.subsec_nanos() as libc::c_long }
}

/// SplitMix64: tiny, seedable, good enough for Bernoulli loss decisions.
pub struct Rng(u64);

impl Rng {
    pub fn new(seed: u64) -> Self {
        Rng(seed ^ 0x9E37_79B9_7F4A_7C15)
    }
    pub fn next_u64(&mut self) -> u64 {
        self.0 = self.0.wrapping_add(0x9E37_79B9_7F4A_7C15);
        let mut z = self.0;
        z = (z ^ (z >> 30)).wrapping_mul(0xBF58_476D_1CE4_E5B9);
        z = (z ^ (z >> 27)).wrapping_mul(0x94D0_49BB_1331_11EB);
        z ^ (z >> 31)
    }
    /// Uniform in [0, 1).
    pub fn next_f64(&mut self) -> f64 {
        (self.next_u64() >> 11) as f64 * (1.0 / (1u64 << 53) as f64)
    }
}

/// Nearest-rank percentile on an already sorted slice. `p` in [0, 100].
pub fn percentile(sorted: &[f64], p: f64) -> Option<f64> {
    if sorted.is_empty() {
        return None;
    }
    let rank = ((p / 100.0) * sorted.len() as f64).ceil() as usize;
    let idx = rank.clamp(1, sorted.len()) - 1;
    Some(sorted[idx])
}

pub fn mean(v: &[f64]) -> Option<f64> {
    if v.is_empty() {
        None
    } else {
        Some(v.iter().sum::<f64>() / v.len() as f64)
    }
}

/// Summary statistics of a latency sample (values in ms).
pub fn lat_summary(samples: &[f64]) -> serde_json::Value {
    let mut s = samples.to_vec();
    s.sort_by(|a, b| a.partial_cmp(b).unwrap());
    serde_json::json!({
        "n": s.len(),
        "min_ms": s.first(),
        "p50_ms": percentile(&s, 50.0),
        "p90_ms": percentile(&s, 90.0),
        "p99_ms": percentile(&s, 99.0),
        "p999_ms": percentile(&s, 99.9),
        "max_ms": s.last(),
        "mean_ms": mean(&s),
    })
}

/// System-wide CPU jiffies from /proc/stat "cpu" line:
/// user nice system idle iowait irq softirq steal.
#[derive(Clone, Copy, Debug, Default)]
pub struct ProcStat {
    pub v: [u64; 8],
}

pub fn proc_stat() -> ProcStat {
    let mut out = ProcStat::default();
    if let Ok(s) = std::fs::read_to_string("/proc/stat") {
        if let Some(line) = s.lines().next() {
            for (i, f) in line.split_whitespace().skip(1).take(8).enumerate() {
                out.v[i] = f.parse().unwrap_or(0);
            }
        }
    }
    out
}

pub fn proc_stat_delta_json(a: &ProcStat, b: &ProcStat) -> serde_json::Value {
    let hz = unsafe { libc::sysconf(libc::_SC_CLK_TCK) } as f64;
    let d = |i: usize| (b.v[i].saturating_sub(a.v[i])) as f64 / hz;
    serde_json::json!({
        "user_sec": d(0) + d(1),
        "system_sec": d(2),
        "idle_sec": d(3),
        "irq_sec": d(5),
        "softirq_sec": d(6),
        "steal_sec": d(7),
        "busy_sec": d(0) + d(1) + d(2) + d(5) + d(6),
    })
}

/// Process memory from `/proc/self/status` (kB). Used by soak runs to watch
/// for unbounded RSS growth.
#[derive(Clone, Copy, Debug, Default)]
pub struct ProcMem {
    pub vmrss_kb: u64,
    pub vmsize_kb: u64,
    pub vmdata_kb: u64,
}

impl ProcMem {
    pub fn to_json(self) -> serde_json::Value {
        serde_json::json!({
            "vmrss_kb": self.vmrss_kb,
            "vmsize_kb": self.vmsize_kb,
            "vmdata_kb": self.vmdata_kb,
        })
    }
}

pub fn proc_mem() -> ProcMem {
    let mut m = ProcMem::default();
    let Ok(s) = std::fs::read_to_string("/proc/self/status") else {
        return m;
    };
    for line in s.lines() {
        let mut it = line.split_whitespace();
        let Some(k) = it.next() else { continue };
        let Some(v) = it.next().and_then(|x| x.parse().ok()) else { continue };
        match k {
            "VmRSS:" => m.vmrss_kb = v,
            "VmSize:" => m.vmsize_kb = v,
            "VmData:" => m.vmdata_kb = v,
            _ => {}
        }
    }
    m
}

/// Zero-throughput streaks on the per-second byte series, skipping warmup
/// and an optional mid-stream pause window (1-based seconds, inclusive).
pub fn stall_stats(agg: &[u64], warmup: u64, pause_after: u64, pause_for: u64) -> serde_json::Value {
    let mut max_streak = 0usize;
    let mut events = 0usize;
    let mut cur = 0usize;
    let mut first = None;
    let mut zero_secs = 0usize;
    for (i, &b) in agg.iter().enumerate() {
        let sec = (i as u64) + 1;
        if (i as u64) < warmup {
            continue;
        }
        if pause_for > 0 && sec > pause_after && sec <= pause_after + pause_for {
            continue;
        }
        if b == 0 {
            zero_secs += 1;
            cur += 1;
            if cur == 1 {
                first.get_or_insert(sec);
            }
            max_streak = max_streak.max(cur);
        } else {
            if cur >= 2 {
                events += 1;
            }
            cur = 0;
        }
    }
    if cur >= 2 {
        events += 1;
    }
    serde_json::json!({
        "zero_throughput_secs": zero_secs,
        "max_zero_streak": max_streak,
        "stall_events_ge2s": events,
        "first_zero_sec": first,
    })
}

/// Sum of the per-CPU "dropped" column of /proc/net/softnet_stat (backlog drops).
pub fn softnet_dropped() -> u64 {
    let Ok(s) = std::fs::read_to_string("/proc/net/softnet_stat") else {
        return 0;
    };
    s.lines().filter_map(|l| l.split_whitespace().nth(1)).filter_map(|h| u64::from_str_radix(h, 16).ok()).sum()
}

/// Set TCP_CONGESTION on a kernel socket.
pub fn set_tcp_cc(fd: std::os::fd::RawFd, name: &str) -> std::io::Result<()> {
    let rc = unsafe { libc::setsockopt(fd, libc::IPPROTO_TCP, libc::TCP_CONGESTION, name.as_ptr() as *const _, name.len() as libc::socklen_t) };
    if rc != 0 {
        return Err(std::io::Error::last_os_error());
    }
    Ok(())
}

/// Commit of the zfstack tree this binary was built from: `git rev-parse`, or
/// the `.zfstack_commit` file that `wan_ab.sh` writes next to an rsynced tree
/// without `.git` (or without git installed).
pub fn zfstack_commit() -> String {
    let root = concat!(env!("CARGO_MANIFEST_DIR"), "/..");
    let g = crate::tun::sh_output(&format!("git -C {root} rev-parse --short HEAD")).trim().to_string();
    if !g.is_empty() {
        return g;
    }
    std::fs::read_to_string(format!("{root}/.zfstack_commit")).map(|s| s.trim().to_string()).unwrap_or_default()
}
