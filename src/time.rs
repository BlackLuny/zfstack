//! Caller-supplied monotonic time. The core never reads a clock (sans-IO, §3.2).

use core::ops::{Add, AddAssign, Sub};
use core::time::Duration;

/// Monotonic instant in nanoseconds from an arbitrary, caller-chosen epoch.
#[derive(Copy, Clone, Debug, Default, PartialEq, Eq, PartialOrd, Ord, Hash)]
pub struct Instant(u64);

impl Instant {
    pub const ZERO: Instant = Instant(0);
    pub const MAX: Instant = Instant(u64::MAX);

    pub const fn from_nanos(ns: u64) -> Self {
        Instant(ns)
    }
    pub const fn from_micros(us: u64) -> Self {
        Instant(us * 1_000)
    }
    pub const fn from_millis(ms: u64) -> Self {
        Instant(ms * 1_000_000)
    }
    pub const fn as_nanos(self) -> u64 {
        self.0
    }
    pub const fn as_micros(self) -> u64 {
        self.0 / 1_000
    }
    pub const fn as_millis(self) -> u64 {
        self.0 / 1_000_000
    }
    /// `self - earlier`, or zero if `earlier` is later.
    pub fn saturating_since(self, earlier: Instant) -> Duration {
        Duration::from_nanos(self.0.saturating_sub(earlier.0))
    }
}

#[inline]
fn dur_ns(d: Duration) -> u64 {
    d.as_nanos().min(u64::MAX as u128) as u64
}

impl Add<Duration> for Instant {
    type Output = Instant;
    fn add(self, d: Duration) -> Instant {
        Instant(self.0.saturating_add(dur_ns(d)))
    }
}
impl AddAssign<Duration> for Instant {
    fn add_assign(&mut self, d: Duration) {
        *self = *self + d;
    }
}
impl Sub<Duration> for Instant {
    type Output = Instant;
    fn sub(self, d: Duration) -> Instant {
        Instant(self.0.saturating_sub(dur_ns(d)))
    }
}
impl Sub<Instant> for Instant {
    type Output = Duration;
    fn sub(self, o: Instant) -> Duration {
        self.saturating_since(o)
    }
}
