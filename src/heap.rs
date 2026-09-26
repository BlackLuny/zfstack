//! Indexed 4-ary min-heap keyed by deadline (§7.4). Each connection slot appears at
//! most once; updates adjust the key in place, so capacity never grows with the
//! number of re-arms (no lazy/tombstone entries).

use crate::time::Instant;

const NONE: u32 = u32::MAX;

#[derive(Default)]
pub struct IndexedHeap {
    heap: Vec<(Instant, u32)>,
    pos: Vec<u32>,
}

impl IndexedHeap {
    pub fn len(&self) -> usize {
        self.heap.len()
    }
    pub fn capacity(&self) -> usize {
        self.heap.capacity()
    }
    #[cfg(test)]
    pub(crate) fn backing_capacity_bytes(&self) -> usize {
        self.heap.capacity() * std::mem::size_of::<(Instant, u32)>()
            + self.pos.capacity() * std::mem::size_of::<u32>()
    }
    pub fn contains(&self, idx: u32) -> bool {
        self.pos.get(idx as usize).is_some_and(|&p| p != NONE)
    }
    pub fn peek(&self) -> Option<(Instant, u32)> {
        self.heap.first().copied()
    }

    /// Insert or move `idx` to `key`.
    pub fn set(&mut self, idx: u32, key: Instant) {
        let i = idx as usize;
        if i >= self.pos.len() {
            self.pos.resize(i + 1, NONE);
        }
        let p = self.pos[i];
        if p == NONE {
            self.heap.push((key, idx));
            let at = self.heap.len() - 1;
            self.pos[i] = at as u32;
            self.up(at);
        } else {
            let p = p as usize;
            let old = self.heap[p].0;
            self.heap[p].0 = key;
            if key < old {
                self.up(p);
            } else {
                self.down(p);
            }
        }
    }

    pub fn remove(&mut self, idx: u32) {
        let i = idx as usize;
        let Some(&p) = self.pos.get(i) else { return };
        if p == NONE {
            return;
        }
        let p = p as usize;
        let last = self.heap.len() - 1;
        self.swap(p, last);
        self.heap.pop();
        self.pos[i] = NONE;
        if p < self.heap.len() {
            self.down(p);
            self.up(p);
        }
    }

    /// Pop the minimum if its key is `<= now`.
    pub fn pop_due(&mut self, now: Instant) -> Option<u32> {
        let &(k, idx) = self.heap.first()?;
        if k > now {
            return None;
        }
        self.remove(idx);
        Some(idx)
    }

    fn swap(&mut self, a: usize, b: usize) {
        self.heap.swap(a, b);
        self.pos[self.heap[a].1 as usize] = a as u32;
        self.pos[self.heap[b].1 as usize] = b as u32;
    }

    fn up(&mut self, mut i: usize) {
        while i > 0 {
            let parent = (i - 1) / 4;
            if self.heap[i].0 < self.heap[parent].0 {
                self.swap(i, parent);
                i = parent;
            } else {
                break;
            }
        }
    }

    fn down(&mut self, mut i: usize) {
        let n = self.heap.len();
        loop {
            let first = 4 * i + 1;
            if first >= n {
                break;
            }
            let mut best = first;
            for c in first + 1..(first + 4).min(n) {
                if self.heap[c].0 < self.heap[best].0 {
                    best = c;
                }
            }
            if self.heap[best].0 < self.heap[i].0 {
                self.swap(i, best);
                i = best;
            } else {
                break;
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn ordered_pop_and_update() {
        let mut h = IndexedHeap::default();
        let keys = [50u64, 10, 70, 30, 90, 20, 60, 40, 80, 0];
        for (i, &k) in keys.iter().enumerate() {
            h.set(i as u32, Instant::from_nanos(k));
        }
        // Re-arming many times must not grow the heap.
        for r in 0..1000u64 {
            h.set(3, Instant::from_nanos(30 + r % 7));
        }
        assert_eq!(h.len(), keys.len());
        h.set(9, Instant::from_nanos(100));
        h.remove(1);
        let mut out = Vec::new();
        while let Some(i) = h.pop_due(Instant::MAX) {
            out.push(i);
        }
        assert_eq!(out, vec![5, 3, 7, 0, 6, 2, 8, 4, 9]);
        assert!(h.len() == 0);
    }
}
