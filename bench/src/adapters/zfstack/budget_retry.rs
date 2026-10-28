//! Budget-blocked writer recovery for the single-threaded benchmark host.
//! Memory/aggregate-quota stalls need a budget release, not an ACK on a
//! connection that may not have managed to send its first byte yet.
use std::collections::HashSet;
use zfstack::{ConnId, Shard, WriteResult};

pub struct BudgetRetry {
    enabled: bool,
    waiting: HashSet<ConnId>,
    observed: Option<u64>,
    last_progress: Option<(u64, u64, u64)>,
    attempts: u64,
    partial_attempts: u64,
    memory_stalls: u64,
    quota_stalls: u64,
}

impl BudgetRetry {
    pub fn new(enabled: bool) -> Self {
        Self { enabled, waiting: HashSet::new(), observed: None, last_progress: None, attempts: 0, partial_attempts: 0, memory_stalls: 0, quota_stalls: 0 }
    }

    pub fn write(&mut self, shard: &mut Shard, id: ConnId, src: &[u8]) -> WriteResult {
        let mut result = shard.write(id, src);
        if !self.enabled {
            return result;
        }
        self.remove(id);
        // An atomic multi-block write can fail while one block still fits.
        // A partial first write admits this sender into the core's share tally.
        if matches!(result, WriteResult::MemoryBlocked | WriteResult::QuotaBlocked) && src.len() > zfstack::buf::TX_BLOCK && shard.send_space(id) > 0 {
            self.partial_attempts += 1;
            result = shard.write(id, &src[..zfstack::buf::TX_BLOCK]);
        }
        if matches!(result, WriteResult::MemoryBlocked | WriteResult::QuotaBlocked) && shard.send_space(id) > 0 {
            if result == WriteResult::MemoryBlocked {
                self.memory_stalls += 1;
            } else {
                self.quota_stalls += 1;
            }
            self.waiting.insert(id);
            // All managed releases on this fixture occur on the same stack
            // thread. Observe after the failed attempt's own rollback.
            self.observed.get_or_insert_with(|| shard.budget().global().release_epoch());
        }
        result
    }

    pub fn ready(&self, epoch: u64) -> bool {
        self.observed.is_some_and(|old| old != epoch) && !self.waiting.is_empty()
    }

    pub fn activate(&mut self, epoch: u64, progress: (u64, u64, u64), active: &mut HashSet<ConnId>) {
        // Allocation rollback/cache reclaim also advances release_epoch. It
        // cannot fuel another batch until real ingress, close, or app-read
        // progress occurs. No scan when the waiting set is empty.
        if self.ready(epoch) && self.last_progress != Some(progress) {
            self.last_progress = Some(progress);
            self.attempts += self.waiting.len() as u64;
            active.extend(self.waiting.drain());
            self.observed = None;
        }
    }

    pub fn remove(&mut self, id: ConnId) {
        self.waiting.remove(&id);
        if self.waiting.is_empty() {
            self.observed = None;
        }
    }

    pub fn json(&self) -> serde_json::Value {
        serde_json::json!({"enabled": self.enabled, "retry_attempts": self.attempts,
            "partial_write_attempts": self.partial_attempts, "memory_stalls": self.memory_stalls,
            "aggregate_quota_stalls": self.quota_stalls, "waiting_at_exit": self.waiting.len()})
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use zfstack::{budget::GlobalBudget, PeerId, StackConfig};

    #[test]
    fn a_real_budget_release_rearms_once_and_cancel_removes_the_waiter() {
        let global = GlobalBudget::new(8 << 20);
        let mut shard = Shard::with_budget(StackConfig::default(), global.clone());
        let memory = shard.memory_handle(PeerId(1));
        let held = memory.try_allocate(65536).unwrap();
        let mut retry = BudgetRetry::new(true);
        let id = ConnId(17);
        retry.waiting.insert(id);
        retry.observed = Some(global.release_epoch());
        let mut active = HashSet::new();
        retry.activate(global.release_epoch(), (1, 0, 0), &mut active);
        assert!(active.is_empty(), "unchanged budget must not spin the app");
        drop(held);
        retry.activate(global.release_epoch(), (1, 0, 0), &mut active);
        assert_eq!(active, HashSet::from([id]));
        assert_eq!(retry.attempts, 1);
        active.clear();
        retry.activate(global.release_epoch(), (1, 0, 0), &mut active);
        assert!(active.is_empty(), "one release cannot repeatedly enqueue a writer");
        retry.waiting.insert(id);
        retry.observed = Some(global.release_epoch());
        retry.remove(id);
        assert!(!retry.ready(global.release_epoch().wrapping_add(1)));
    }

    #[test]
    fn allocator_rollback_alone_cannot_fuel_a_retry_loop() {
        let mut retry = BudgetRetry::new(true);
        let mut active = HashSet::new();
        let id = ConnId(23);
        retry.waiting.insert(id);
        retry.observed = Some(0);
        retry.activate(1, (10, 0, 0), &mut active);
        assert_eq!(retry.attempts, 1);
        active.clear();
        retry.waiting.insert(id);
        retry.observed = Some(1);
        retry.activate(2, (10, 0, 0), &mut active);
        assert!(active.is_empty(), "self rollback is not new input or drain progress");
        retry.activate(2, (11, 0, 0), &mut active);
        assert_eq!(active, HashSet::from([id]));
        assert_eq!(retry.attempts, 2);
    }
}
