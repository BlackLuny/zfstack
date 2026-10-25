//! Explicit test-only disturbances. The core controller cannot access these.
use std::io;
use std::sync::OnceLock;
use std::time::{Duration, Instant};

static WINDOW: OnceLock<Instant> = OnceLock::new();

pub fn mark_window(start: Instant) { let _ = WINDOW.set(start); }
pub fn window() -> Option<Instant> { WINDOW.get().copied() }

fn steps(value: &str) -> io::Result<Vec<(Duration, u64)>> {
    let invalid = || io::Error::new(io::ErrorKind::InvalidInput, "expected increasing millisecond:positive-rate pairs");
    if value.is_empty() { return Err(invalid()); }
    let mut result = Vec::new();
    for item in value.split(',') {
        let (at, rate) = item.split_once(':').ok_or_else(invalid)?;
        let at = at.parse::<u64>().map_err(|_| invalid())?;
        let rate = rate.parse::<u64>().map_err(|_| invalid())?;
        if at == 0 || rate == 0 || result.last().is_some_and(|(old, _)| *old >= Duration::from_millis(at)) { return Err(invalid()); }
        result.push((Duration::from_millis(at), rate));
    }
    Ok(result)
}

#[derive(Default, Clone)]
pub struct Controls {
    pub rate_steps: Vec<(Duration, u64)>,
    pub down_pause: Option<(Duration, Duration)>,
    pub trace: bool,
}

impl Controls {
    pub fn from_env() -> io::Result<Self> {
        let mut result = Self::default();
        match std::env::var("ZFBENCH_LINK_RATE_STEPS") {
            Ok(v) => result.rate_steps = steps(&v)?,
            Err(std::env::VarError::NotPresent) => {},
            Err(e) => return Err(io::Error::new(io::ErrorKind::InvalidInput, e)),
        }
        match std::env::var("ZFBENCH_DOWN_PAUSE_MS") {
            Ok(v) => {
                let values = steps(&v)?;
                if values.len() != 1 || values[0].1 <= values[0].0.as_millis() as u64 {
                    return Err(io::Error::new(io::ErrorKind::InvalidInput, "pause must be start-ms:end-ms, end > start"));
                }
                result.down_pause = Some((values[0].0, Duration::from_millis(values[0].1)));
            },
            Err(std::env::VarError::NotPresent) => {},
            Err(e) => return Err(io::Error::new(io::ErrorKind::InvalidInput, e)),
        }
        result.trace = match std::env::var("ZFBENCH_ADAPTIVE_TRACE").as_deref() {
            Ok("1") => true,
            Ok("0") | Err(std::env::VarError::NotPresent) => false,
            _ => return Err(io::Error::new(io::ErrorKind::InvalidInput, "ZFBENCH_ADAPTIVE_TRACE must be 0 or 1")),
        };
        Ok(result)
    }

    pub fn enabled(&self) -> bool { !self.rate_steps.is_empty() || self.down_pause.is_some() || self.trace }

    pub fn pause_remaining(&self, now: Instant) -> Option<Duration> {
        let (from, until) = self.down_pause?;
        let elapsed = now.saturating_duration_since(window()?);
        (elapsed >= from && elapsed < until).then(|| until - elapsed)
    }

    pub fn json(&self) -> serde_json::Value {
        serde_json::json!({"rate_steps":self.rate_steps.iter().map(|(at, rate)| serde_json::json!({"at_ms":at.as_millis(),"rate_bps":rate})).collect::<Vec<_>>(),
            "down_pause_ms":self.down_pause.map(|(a,b)|[a.as_millis(),b.as_millis()]),"trace":self.trace,
            "clock":"relative to the isolate useful-work window; test fixture only"})
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn rejects_ambiguous_or_unbounded_capacity_schedules() {
        assert_eq!(steps("1000:20000000,3000:100000000").unwrap().len(), 2);
        for v in ["", "0:1", "1:0", "2:10,1:20", "1:10,1:20", "1:10,", "1:2:3", "-1:10"] { assert!(steps(v).is_err(), "{v}"); }
    }
}
