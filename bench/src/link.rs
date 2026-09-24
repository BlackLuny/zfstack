//! Userspace link emulator, one thread per direction.
//!
//! Semantics (mirrors the fixed testbed of 0002 §2.1):
//! 1. random loss `p` is decided at arrival (counted separately);
//! 2. bottleneck: drop-tail queue of `queue_limit` bytes in front of a
//!    serializer of rate R. A packet is dropped iff the bytes queued but not
//!    yet fully serialized + its length exceed the limit.
//!    departure = max(now, last_departure) + len*8/R;
//! 3. propagation delay D (unbounded, never drops): deliver_at = departure + D.
//!
//! Rates count IP-packet bytes (no L2 overhead). Because deliver_at is monotonic
//! the delay line is a FIFO.

use std::collections::VecDeque;
use std::os::fd::RawFd;
use std::sync::atomic::{AtomicBool, Ordering::Relaxed};
use std::sync::Arc;
use std::time::{Duration, Instant};

use crossbeam_channel::{Receiver, RecvTimeoutError, Sender};

use crate::stack::Batch;
use crate::util;

#[derive(Clone, Debug)]
pub struct DirParams {
    /// Bottleneck rate in bits/s, 0 = unlimited.
    pub rate_bps: u64,
    /// Drop-tail limit of the bottleneck queue in bytes.
    pub queue_limit: usize,
    /// One-way propagation delay.
    pub delay: Duration,
    /// Random loss probability.
    pub loss: f64,
    pub seed: u64,
}

pub enum Source {
    Tun(RawFd),
    Chan(Receiver<Batch>),
}

pub enum Sink {
    Tun(RawFd),
    Chan(Sender<Batch>),
}

const LATE_BUCKETS_US: [u64; 8] = [10, 25, 50, 100, 200, 500, 1000, 5000];

#[derive(Default, Debug)]
pub struct DirCounters {
    pub pkts_in: u64,
    pub bytes_in: u64,
    pub delivered_pkts: u64,
    pub delivered_bytes: u64,
    pub bottleneck_drops: u64,
    pub bottleneck_drop_bytes: u64,
    pub random_drops: u64,
    pub random_drop_bytes: u64,
    pub max_queue_bytes: usize,
    pub sink_errors: u64,
    pub wakeups: u64,
    pub late_sum_ns: u128,
    pub late_max_ns: u64,
    pub late_hist: [u64; LATE_BUCKETS_US.len() + 1],
    pub max_delay_line_pkts: usize,
    pub cpu_sec: f64,
}

impl DirCounters {
    pub fn to_json(&self, p: &DirParams) -> serde_json::Value {
        let hist: serde_json::Map<String, serde_json::Value> = LATE_BUCKETS_US
            .iter()
            .map(|b| format!("lt_{b}us"))
            .chain(std::iter::once("ge_5000us".to_string()))
            .zip(self.late_hist.iter().map(|v| serde_json::json!(v)))
            .collect();
        serde_json::json!({
            "params": {
                "rate_bps": p.rate_bps,
                "queue_limit_bytes": p.queue_limit,
                "delay_us": p.delay.as_micros() as u64,
                "loss": p.loss,
            },
            "pkts_in": self.pkts_in,
            "bytes_in": self.bytes_in,
            "delivered_pkts": self.delivered_pkts,
            "delivered_bytes": self.delivered_bytes,
            "bottleneck_drops": self.bottleneck_drops,
            "bottleneck_drop_bytes": self.bottleneck_drop_bytes,
            "random_drops": self.random_drops,
            "random_drop_bytes": self.random_drop_bytes,
            "max_queue_bytes": self.max_queue_bytes,
            "max_delay_line_pkts": self.max_delay_line_pkts,
            "sink_errors": self.sink_errors,
            "wakeups": self.wakeups,
            "timing_late_mean_us": if self.delivered_pkts > 0 {
                self.late_sum_ns as f64 / self.delivered_pkts as f64 / 1e3 } else { 0.0 },
            "timing_late_max_us": self.late_max_ns as f64 / 1e3,
            "timing_late_hist": hist,
            "cpu_sec": self.cpu_sec,
        })
    }
}

struct Emu {
    p: DirParams,
    rng: util::Rng,
    /// (departure time, len) of packets not yet fully serialized.
    bn: VecDeque<(Instant, usize)>,
    bn_bytes: usize,
    last_dep: Instant,
    ns_per_byte: f64,
    line: VecDeque<(Instant, Vec<u8>)>,
    c: DirCounters,
}

impl Emu {
    fn arrive(&mut self, now: Instant, pkt: Vec<u8>) {
        let len = pkt.len();
        self.c.pkts_in += 1;
        self.c.bytes_in += len as u64;
        if self.p.loss > 0.0 && self.rng.next_f64() < self.p.loss {
            self.c.random_drops += 1;
            self.c.random_drop_bytes += len as u64;
            return;
        }
        let deliver_at = if self.p.rate_bps > 0 {
            while let Some(&(dep, l)) = self.bn.front() {
                if dep <= now {
                    self.bn.pop_front();
                    self.bn_bytes -= l;
                } else {
                    break;
                }
            }
            if self.bn_bytes + len > self.p.queue_limit {
                self.c.bottleneck_drops += 1;
                self.c.bottleneck_drop_bytes += len as u64;
                return;
            }
            let start = self.last_dep.max(now);
            let dep = start + Duration::from_nanos((len as f64 * self.ns_per_byte) as u64);
            self.last_dep = dep;
            self.bn.push_back((dep, len));
            self.bn_bytes += len;
            self.c.max_queue_bytes = self.c.max_queue_bytes.max(self.bn_bytes);
            dep + self.p.delay
        } else {
            now + self.p.delay
        };
        self.line.push_back((deliver_at, pkt));
        self.c.max_delay_line_pkts = self.c.max_delay_line_pkts.max(self.line.len());
    }

    /// Pop every packet due at `now`.
    fn due(&mut self, now: Instant, out: &mut Batch) {
        while let Some((at, _)) = self.line.front() {
            if *at > now {
                break;
            }
            let (at, pkt) = self.line.pop_front().unwrap();
            let late = (now - at).as_nanos() as u64;
            self.c.late_sum_ns += late as u128;
            self.c.late_max_ns = self.c.late_max_ns.max(late);
            let us = late / 1000;
            let idx = LATE_BUCKETS_US.iter().position(|b| us < *b).unwrap_or(LATE_BUCKETS_US.len());
            self.c.late_hist[idx] += 1;
            self.c.delivered_pkts += 1;
            self.c.delivered_bytes += pkt.len() as u64;
            out.push(pkt);
        }
    }
}

const IDLE_WAIT: Duration = Duration::from_millis(20);

/// Run one direction until `stop` is set. Returns its counters.
pub fn run_direction(p: DirParams, source: Source, sink: Sink, stop: Arc<AtomicBool>) -> DirCounters {
    util::set_timerslack_ns(1);
    let cpu0 = util::thread_cpu_now();
    let ns_per_byte = if p.rate_bps > 0 { 8e9 / p.rate_bps as f64 } else { 0.0 };
    let now = Instant::now();
    let mut emu = Emu {
        rng: util::Rng::new(p.seed),
        p,
        bn: VecDeque::new(),
        bn_bytes: 0,
        last_dep: now,
        ns_per_byte,
        line: VecDeque::new(),
        c: DirCounters::default(),
    };
    let mut rbuf = vec![0u8; 65536];
    let mut out: Batch = Vec::with_capacity(256);
    loop {
        let now = Instant::now();
        emu.due(now, &mut out);
        if !out.is_empty() {
            match &sink {
                Sink::Tun(fd) => {
                    for pkt in out.drain(..) {
                        let r = unsafe { libc::write(*fd, pkt.as_ptr() as *const _, pkt.len()) };
                        if r < 0 {
                            emu.c.sink_errors += 1;
                        }
                    }
                }
                Sink::Chan(tx) => {
                    let b = std::mem::replace(&mut out, Vec::with_capacity(256));
                    if tx.send(b).is_err() {
                        emu.c.sink_errors += 1;
                    }
                }
            }
        }
        if stop.load(Relaxed) {
            break;
        }
        let now = Instant::now();
        let deadline = match emu.line.front() {
            Some((at, _)) => (*at).min(now + IDLE_WAIT),
            None => now + IDLE_WAIT,
        };
        match &source {
            Source::Tun(fd) => {
                let mut pfd = libc::pollfd { fd: *fd, events: libc::POLLIN, revents: 0 };
                if deadline > now {
                    let ts = util::dur_to_timespec(deadline - now);
                    unsafe { libc::ppoll(&mut pfd, 1, &ts, std::ptr::null()) };
                    emu.c.wakeups += 1;
                }
                let t = Instant::now();
                // Drain what is readable (bounded so deliveries stay timely).
                for _ in 0..1024 {
                    let r = unsafe { libc::read(*fd, rbuf.as_mut_ptr() as *mut _, rbuf.len()) };
                    if r <= 0 {
                        break;
                    }
                    emu.arrive(t, rbuf[..r as usize].to_vec());
                }
            }
            Source::Chan(rx) => {
                let r = if deadline > now {
                    emu.c.wakeups += 1;
                    rx.recv_deadline(deadline)
                } else {
                    rx.try_recv().map_err(|e| match e {
                        crossbeam_channel::TryRecvError::Empty => RecvTimeoutError::Timeout,
                        crossbeam_channel::TryRecvError::Disconnected => RecvTimeoutError::Disconnected,
                    })
                };
                match r {
                    Ok(b) => {
                        let t = Instant::now();
                        for pkt in b {
                            emu.arrive(t, pkt);
                        }
                        while let Ok(b) = rx.try_recv() {
                            for pkt in b {
                                emu.arrive(t, pkt);
                            }
                        }
                    }
                    Err(RecvTimeoutError::Timeout) => {}
                    Err(RecvTimeoutError::Disconnected) => {
                        if emu.line.is_empty() {
                            break;
                        }
                        // flush the delay line before exiting
                        if let Some((at, _)) = emu.line.front() {
                            let at = *at;
                            let n = Instant::now();
                            if at > n {
                                std::thread::sleep(at - n);
                            }
                        }
                    }
                }
            }
        }
    }
    emu.c.cpu_sec = util::thread_cpu_now() - cpu0;
    emu.c
}
