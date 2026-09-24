//! Userspace-stack adapter trait and the dedicated stack thread.

use std::sync::atomic::{AtomicBool, AtomicU64, Ordering::Relaxed};
use std::sync::Arc;
use std::time::{Duration, Instant};

use crossbeam_channel::{Receiver, RecvTimeoutError, Sender};

use crate::util;

/// A batch of IP packets moving between the link thread and the stack thread.
pub type Batch = Vec<Vec<u8>>;

pub trait UserStack: Send {
    /// Feed one IP packet (from client towards server). `now` is monotonic.
    fn ingress(&mut self, now: Instant, pkt: &[u8]);
    /// Run protocol + the embedded app server; push every outgoing IP packet via `out`.
    fn poll(&mut self, now: Instant, out: &mut dyn FnMut(&[u8]));
    /// Earliest time `poll` must be called again even without input (None = only on input).
    fn next_deadline(&mut self, now: Instant) -> Option<Instant>;
    fn stats(&self) -> serde_json::Value {
        serde_json::Value::Null
    }
}

/// Live counters of the stack thread, readable by the orchestrator.
#[derive(Default)]
pub struct StackLive {
    pub wakeups: AtomicU64,
    /// Wakeups caused by a deadline expiring (no input).
    pub timer_wakeups: AtomicU64,
    pub pkts_in: AtomicU64,
    pub pkts_out: AtomicU64,
    pub polls: AtomicU64,
    /// Sum / max of how late the thread woke up relative to the stack's requested
    /// deadline (only timer wakeups), ns.
    pub deadline_late_sum_ns: AtomicU64,
    pub deadline_late_max_ns: AtomicU64,
}

pub struct StackResult {
    pub cpu_sec: f64,
    pub stats: serde_json::Value,
}

/// Longest the stack thread sleeps without input, so it notices `stop`.
const MAX_IDLE: Duration = Duration::from_millis(50);

/// Stack thread body. Blocks on the ingress channel until the stack's next
/// deadline, drains every queued batch, feeds them, polls once and forwards
/// all produced packets to the link as one batch.
pub fn run_stack_thread(mut stack: Box<dyn UserStack>, rx: Receiver<Batch>, tx: Sender<Batch>, stop: Arc<AtomicBool>, live: Arc<StackLive>) -> StackResult {
    util::set_timerslack_ns(1);
    let cpu0 = util::thread_cpu_now();
    let mut out_batch: Batch = Vec::with_capacity(256);
    let mut spare: Vec<Batch> = Vec::new();
    let mut disconnected = false;
    while !stop.load(Relaxed) && !disconnected {
        let now = Instant::now();
        let dl = stack.next_deadline(now);
        let wait_until = match dl {
            Some(d) => d.min(now + MAX_IDLE),
            None => now + MAX_IDLE,
        };
        let first = if wait_until <= now {
            match rx.try_recv() {
                Ok(b) => Ok(b),
                Err(crossbeam_channel::TryRecvError::Empty) => Err(RecvTimeoutError::Timeout),
                Err(crossbeam_channel::TryRecvError::Disconnected) => Err(RecvTimeoutError::Disconnected),
            }
        } else {
            rx.recv_deadline(wait_until)
        };
        let now = Instant::now();
        live.wakeups.fetch_add(1, Relaxed);
        let mut n_in = 0u64;
        match first {
            Ok(b) => {
                for p in &b {
                    stack.ingress(now, p);
                }
                n_in += b.len() as u64;
                recycle(&mut spare, b);
                while let Ok(b) = rx.try_recv() {
                    for p in &b {
                        stack.ingress(now, p);
                    }
                    n_in += b.len() as u64;
                    recycle(&mut spare, b);
                }
            }
            Err(RecvTimeoutError::Timeout) => {
                match dl {
                    Some(d) if d <= now => {
                        live.timer_wakeups.fetch_add(1, Relaxed);
                        let late = (now - d).as_nanos() as u64;
                        live.deadline_late_sum_ns.fetch_add(late, Relaxed);
                        live.deadline_late_max_ns.fetch_max(late, Relaxed);
                    }
                    _ => {
                        // idle cap expired, nothing requested: skip the poll.
                        continue;
                    }
                }
            }
            Err(RecvTimeoutError::Disconnected) => disconnected = true,
        }
        live.pkts_in.fetch_add(n_in, Relaxed);
        stack.poll(now, &mut |p: &[u8]| out_batch.push(p.to_vec()));
        live.polls.fetch_add(1, Relaxed);
        if !out_batch.is_empty() {
            live.pkts_out.fetch_add(out_batch.len() as u64, Relaxed);
            let next = spare.pop().unwrap_or_else(|| Vec::with_capacity(256));
            let b = std::mem::replace(&mut out_batch, next);
            if tx.send(b).is_err() {
                break;
            }
        }
    }
    StackResult { cpu_sec: util::thread_cpu_now() - cpu0, stats: stack.stats() }
}

fn recycle(spare: &mut Vec<Batch>, mut b: Batch) {
    if spare.len() < 8 {
        b.clear();
        spare.push(b);
    }
}
