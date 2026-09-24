//! 32-bit TCP sequence numbers and their mapping to 64-bit stream offsets.
//!
//! Internally every connection tracks positions as `u64` offsets counted from the
//! initial sequence number (ISN = offset 0, first data byte = offset 1). Wire
//! sequence numbers only appear at the parse/emit boundary, which removes
//! wrap-around comparisons from the state machine.

#[derive(Copy, Clone, Debug, Default, PartialEq, Eq, Hash)]
pub struct Seq(pub u32);

impl Seq {
    #[inline]
    pub fn add(self, n: u32) -> Seq {
        Seq(self.0.wrapping_add(n))
    }
    /// Signed distance `self - other`.
    #[inline]
    pub fn diff(self, other: Seq) -> i32 {
        self.0.wrapping_sub(other.0) as i32
    }
    #[inline]
    pub fn lt(self, o: Seq) -> bool {
        self.diff(o) < 0
    }
    #[inline]
    pub fn le(self, o: Seq) -> bool {
        self.diff(o) <= 0
    }
    #[inline]
    pub fn gt(self, o: Seq) -> bool {
        self.diff(o) > 0
    }
    #[inline]
    pub fn ge(self, o: Seq) -> bool {
        self.diff(o) >= 0
    }
}

/// Maps between wire sequence numbers and 64-bit offsets from an ISN.
#[derive(Copy, Clone, Debug)]
pub struct SeqSpace {
    pub isn: Seq,
}

impl SeqSpace {
    #[inline]
    pub fn seq(&self, off: u64) -> Seq {
        self.isn.add(off as u32)
    }
    /// Resolve `s` to the offset nearest to `reference` (within ±2^31).
    #[inline]
    pub fn off(&self, s: Seq, reference: u64) -> i64 {
        let d = s.diff(self.seq(reference)) as i64;
        reference as i64 + d
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn wrap() {
        let sp = SeqSpace { isn: Seq(u32::MAX - 5) };
        assert_eq!(sp.seq(0), Seq(u32::MAX - 5));
        assert_eq!(sp.seq(10), Seq(4));
        assert_eq!(sp.off(Seq(4), 3), 10);
        assert_eq!(sp.off(Seq(u32::MAX - 6), 3), -1);
        assert!(Seq(1).gt(Seq(u32::MAX)));
        let big = 5u64 << 32;
        assert_eq!(sp.off(sp.seq(big + 7), big), (big + 7) as i64);
    }
}
