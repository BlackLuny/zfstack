//! Bounded, generation-aware application readiness and reproducible diagnostics.
use std::collections::{BTreeMap, VecDeque};

use serde_json::{json, Value};
use zfstack::ConnId;

#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub enum AppDriveMode {
    #[default]
    Eager,
    EventOnly,
}

impl AppDriveMode {
    pub fn as_str(self) -> &'static str {
        match self {
            Self::Eager => "eager",
            Self::EventOnly => "event-only",
        }
    }

    pub fn parse(s: &str) -> Result<Self, String> {
        match s {
            "eager" => Ok(Self::Eager),
            "event-only" => Ok(Self::EventOnly),
            _ => Err(format!("unknown app-drive {s}")),
        }
    }
}

#[derive(Clone, Copy, Debug, Default)]
pub(crate) struct Interest {
    pub read: bool,
    pub write: bool,
}

impl Interest {
    pub const READ: Self = Self { read: true, write: false };
    pub const WRITE: Self = Self { read: false, write: true };
    pub const BOTH: Self = Self { read: true, write: true };
}

/// Full ConnId keys retain the slot generation. A queue item can never become
/// ready for a replacement connection merely because its slot was reused.
#[derive(Default)]
pub(crate) struct ReadyQueue(BTreeMap<ConnId, Interest>);

impl ReadyQueue {
    pub fn insert(&mut self, id: ConnId, interest: Interest) {
        let value = self.0.entry(id).or_default();
        value.read |= interest.read;
        value.write |= interest.write;
    }
    pub fn remove(&mut self, id: ConnId) {
        self.0.remove(&id);
    }
    pub fn is_empty(&self) -> bool {
        self.0.is_empty()
    }
    pub fn len(&self) -> usize {
        self.0.len()
    }
    pub fn take(&mut self, budget: usize) -> Vec<(ConnId, Interest)> {
        (0..budget).map_while(|_| self.0.pop_first()).collect()
    }
}

/// Stable FNV-1a over versioned JSON records, deliberately diagnostic rather
/// than a security hash. The bounded tail is retained; all records feed digest.
pub struct Trace {
    hash: u64,
    count: u64,
    recent: VecDeque<Value>,
    capacity: usize,
}

impl Default for Trace {
    fn default() -> Self {
        Self::new(128)
    }
}

impl Trace {
    pub fn new(capacity: usize) -> Self {
        Self { hash: 0xcbf29ce484222325, count: 0, recent: VecDeque::new(), capacity }
    }
    pub fn push(&mut self, event: Value) {
        for byte in serde_json::to_vec(&event).expect("trace value serializes").into_iter().chain([b'\n']) {
            self.hash ^= u64::from(byte);
            self.hash = self.hash.wrapping_mul(0x100000001b3);
        }
        self.count += 1;
        if self.capacity > 0 {
            if self.recent.len() == self.capacity {
                self.recent.pop_front();
            }
            self.recent.push_back(event);
        }
    }
    pub fn value(&self, include_tail: bool) -> Value {
        json!({"version": 1, "algorithm": "fnv1a64-json-v1", "digest": format!("{:016x}", self.hash),
            "records": self.count, "capacity": self.capacity,
            "recent": if include_tail { self.recent.iter().cloned().collect::<Vec<_>>() } else { Vec::new() }})
    }
}

/// Stable packet-content identity for replay diagnostics (not an authenticity hash).
pub fn content_digest(bytes: &[u8]) -> String {
    let hash = bytes.iter().fold(0xcbf29ce484222325u64, |h, b| (h ^ u64::from(*b)).wrapping_mul(0x100000001b3));
    format!("{hash:016x}")
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn readiness_merges_interest_but_never_slot_generations() {
        let mut q = ReadyQueue::default();
        let old = ConnId(5);
        let new = ConnId((1 << 32) | 5);
        q.insert(old, Interest::READ);
        q.insert(old, Interest::WRITE);
        q.insert(new, Interest::READ);
        q.remove(old);
        let items = q.take(1);
        assert_eq!(items[0].0, new);
        assert!(items[0].1.read && !items[0].1.write);
        assert!(q.is_empty());
    }
    #[test]
    fn bounded_tail_retains_full_trace_identity() {
        let mut a = Trace::new(2);
        let mut b = Trace::new(5);
        for n in 0..10 {
            a.push(json!({"n": n}));
            b.push(json!({"n": n}));
        }
        assert_eq!(a.value(true)["digest"], b.value(true)["digest"]);
        assert_eq!(a.value(true)["recent"].as_array().unwrap().len(), 2);
    }
}
