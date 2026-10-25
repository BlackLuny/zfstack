//! Test-side bottleneck queue: identical serializer and global byte limit for
//! FIFO and per-peer DRR controls. No packet is prioritised by size or protocol.
use std::collections::{BTreeMap, VecDeque};
use std::time::{Duration, Instant};

struct Group {
    packets: VecDeque<Vec<u8>>,
    bytes: usize,
    deficit: usize,
}
pub struct Queue {
    peer_mode: bool,
    groups: BTreeMap<u32, Group>,
    ready: VecDeque<u32>,
    pub bytes: usize,
    service: Option<(Instant, usize)>,
    ns_per_byte: f64,
    delay: Duration,
}
impl Queue {
    pub fn new(peer_mode: bool, rate_bps: u64, delay: Duration) -> Self {
        Self { peer_mode, groups: BTreeMap::new(), ready: VecDeque::new(), bytes: 0, service: None, ns_per_byte: 8e9 / rate_bps as f64, delay }
    }
    fn key(&self, p: &[u8]) -> u32 {
        if !self.peer_mode || p.len() < 20 {
            return 0;
        }
        if p[12..14] == [10, 210] {
            ((p[18] as u32) << 16) | p[14] as u32
        } else if p[16..18] == [10, 210] {
            ((p[14] as u32) << 16) | p[18] as u32
        } else {
            0
        }
    }
    pub fn deadline(&self) -> Option<Instant> {
        self.service.map(|s| s.0)
    }
    /// A packet already in service keeps its scheduled completion; subsequent
    /// packets use the new rate. The transition error is at most one packet.
    pub fn set_rate(&mut self, rate_bps: u64) {
        assert!(rate_bps > 0);
        self.ns_per_byte = 8e9 / rate_bps as f64;
    }
    pub fn enqueue(&mut self, p: Vec<u8>, limit: usize) -> Vec<Vec<u8>> {
        let key = self.key(&p);
        let mut dropped = Vec::new();
        if p.len() > limit {
            dropped.push(p);
            return dropped;
        }
        // Shared limit is unchanged. Under contention, evict only a tail from
        // a larger backlog, never in-service data or this peer's own FIFO.
        while self.bytes + p.len() > limit {
            let own = self.groups.get(&key).map_or(0, |q| q.bytes);
            let victim = self.groups.iter().filter(|(k, q)| **k != key && q.bytes > own).max_by_key(|(_, q)| q.bytes).map(|(k, _)| *k);
            let Some(v) = victim else {
                dropped.push(p);
                return dropped;
            };
            let q = self.groups.get_mut(&v).unwrap();
            let old = q.packets.pop_back().unwrap();
            q.bytes -= old.len();
            self.bytes -= old.len();
            dropped.push(old);
            if q.packets.is_empty() {
                self.groups.remove(&v);
                self.ready.retain(|k| *k != v);
            }
        }
        if !self.groups.contains_key(&key) {
            self.ready.push_back(key);
            self.groups.insert(key, Group { packets: VecDeque::new(), bytes: 0, deficit: 0 });
        }
        self.bytes += p.len();
        let q = self.groups.get_mut(&key).unwrap();
        q.bytes += p.len();
        q.packets.push_back(p);
        dropped
    }
    fn pop(&mut self) -> Option<Vec<u8>> {
        loop {
            let key = self.ready.pop_front()?;
            let q = self.groups.get_mut(&key).unwrap();
            if q.deficit < q.packets.front().unwrap().len() {
                q.deficit += 1420;
            }
            if q.deficit < q.packets.front().unwrap().len() {
                self.ready.push_back(key);
                continue;
            }
            let p = q.packets.pop_front().unwrap();
            q.deficit -= p.len();
            q.bytes -= p.len();
            if q.packets.is_empty() {
                self.groups.remove(&key);
            } else if q.deficit >= q.packets.front().unwrap().len() {
                self.ready.push_front(key);
            } else {
                self.ready.push_back(key);
            }
            return Some(p);
        }
    }
    pub fn advance(&mut self, now: Instant, line: &mut VecDeque<(Instant, Vec<u8>)>) {
        let mut available = now;
        loop {
            if let Some((end, len)) = self.service {
                if end > now {
                    break;
                }
                self.bytes -= len;
                self.service = None;
                available = end;
            }
            let Some(p) = self.pop() else { break };
            let end = available + Duration::from_nanos((p.len() as f64 * self.ns_per_byte) as u64);
            let len = p.len();
            line.push_back((end + self.delay, p));
            self.service = Some((end, len));
            if end > now {
                break;
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn rate_change_preserves_in_service_packet_and_prices_following_packets() {
        let now = Instant::now();
        let mut q = Queue::new(false, 100_000_000, Duration::ZERO);
        let mut line = VecDeque::new();
        for marker in 1..=3 { q.enqueue(packet(1, marker), 4260); }
        q.advance(now, &mut line);
        let first = line[0].0;
        q.set_rate(10_000_000);
        assert_eq!(q.deadline(), Some(first));
        q.advance(now + Duration::from_millis(3), &mut line);
        assert_eq!(line.len(), 3);
        assert_eq!(line[0].0 - now, Duration::from_nanos(113600));
        assert_eq!(line[1].0 - line[0].0, Duration::from_nanos(1136000));
        assert_eq!(line[2].0 - line[1].0, Duration::from_nanos(1136000));
        assert_eq!(q.bytes, 0);
    }
    fn packet(peer: u8, marker: u8) -> Vec<u8> {
        let mut p = vec![marker; 1420];
        p[12..16].copy_from_slice(&[10, 211, 0, 2]);
        p[16..20].copy_from_slice(&[10, 210, peer, 1]);
        p
    }
    #[test]
    fn shared_limit_pushout_preserves_peer_order_and_rate() {
        let now = Instant::now();
        let mut q = Queue::new(true, 100_000_000, Duration::from_millis(5));
        let mut line = VecDeque::new();
        q.enqueue(packet(1, 1), 4260);
        q.advance(now, &mut line);
        q.enqueue(packet(1, 2), 4260);
        q.enqueue(packet(1, 3), 4260);
        let drops = q.enqueue(packet(2, 4), 4260);
        assert_eq!(drops.len(), 1);
        assert_eq!(drops[0][0], 3);
        assert_eq!(q.bytes, 4260);
        q.advance(now + Duration::from_millis(1), &mut line);
        assert_eq!(line.iter().map(|(_, p)| p[0]).collect::<Vec<_>>(), vec![1, 2, 4]);
        assert_eq!(line[2].0 - line[0].0, Duration::from_nanos(2 * 113600));
        assert_eq!(q.bytes, 0);
    }
    #[test]
    fn fifo_control_has_same_limit_and_cannot_drop_in_service_packet() {
        let now = Instant::now();
        let mut q = Queue::new(false, 100_000_000, Duration::ZERO);
        let mut line = VecDeque::new();
        q.enqueue(packet(1, 1), 1420);
        q.advance(now, &mut line);
        assert_eq!(q.enqueue(packet(2, 2), 1420)[0][0], 2);
        assert_eq!(q.bytes, 1420);
        q.advance(now + Duration::from_millis(1), &mut line);
        assert_eq!(q.bytes, 0);
        assert_eq!(line.len(), 1);
    }
    #[test]
    fn a_busy_peer_cannot_put_two_hundred_packets_before_another_peer() {
        let now = Instant::now();
        let mut q = Queue::new(true, 100_000_000, Duration::ZERO);
        let mut line = VecDeque::new();
        for _ in 0..200 {
            assert!(q.enqueue(packet(1, 1), 400000).is_empty());
        }
        assert!(q.enqueue(packet(2, 2), 400000).is_empty());
        q.advance(now, &mut line);
        q.advance(now + Duration::from_secs(1), &mut line);
        assert_eq!(line.len(), 201);
        assert!(line.iter().position(|(_, p)| p[18] == 2).unwrap() <= 1);
        assert_eq!(line.back().unwrap().0 - now, Duration::from_nanos(201 * 113600));
        assert_eq!(q.bytes, 0);
    }
    #[test]
    fn fifo_serializer_matches_legacy_arrivals_departures_and_drops() {
        let start = Instant::now();
        let mut q = Queue::new(false, 100_000_000, Duration::from_millis(5));
        let mut line = VecDeque::new();
        let mut old_queue = VecDeque::new();
        let mut old_bytes = 0;
        let mut last = start;
        let mut expected = Vec::new();
        let mut offset = 0u64;
        let mut seed = 71u64;
        for i in 0..200u8 {
            seed = seed.wrapping_mul(6364136223846793005).wrapping_add(1);
            offset += (seed >> 32) % 180_000;
            let now = start + Duration::from_nanos(offset);
            let len = [52, 308, 1420][i as usize % 3];
            let mut pkt = packet(1, i);
            pkt.truncate(len);
            while old_queue.front().is_some_and(|&(at, _)| at <= now) {
                let (_, n) = old_queue.pop_front().unwrap();
                old_bytes -= n;
            }
            let drop = old_bytes + len > 5000;
            if !drop {
                let departure = last.max(now) + Duration::from_nanos(len as u64 * 80);
                last = departure;
                old_bytes += len;
                old_queue.push_back((departure, len));
                expected.push((departure + Duration::from_millis(5), i));
            }
            q.advance(now, &mut line);
            assert_eq!(!q.enqueue(pkt, 5000).is_empty(), drop);
            q.advance(now, &mut line);
            assert!(q.bytes <= 5000);
        }
        q.advance(start + Duration::from_secs(1), &mut line);
        assert_eq!(line.into_iter().map(|(at, p)| (at, p[0])).collect::<Vec<_>>(), expected);
        assert_eq!(q.bytes, 0);
    }
}
