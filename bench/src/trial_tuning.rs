//! Opt-in experiment controls; absent controls preserve library profiles.
use std::io;

#[derive(Clone, Copy, Debug, Default)]
pub struct TrialTuning {
    pub min_prefetch: Option<u32>,
    pub prefetch_ms: Option<u64>,
    pub quantum: Option<usize>,
    pub round_cap: Option<usize>,
    pub connect_parallel: usize,
    pub progress: bool,
}

impl TrialTuning {
    pub fn from_env() -> io::Result<Self> {
        let read = |key: &str| -> io::Result<Option<u64>> {
            match std::env::var(key) {
                Ok(v) => positive(&v).map(Some).map_err(|e| io::Error::new(io::ErrorKind::InvalidInput, format!("{key}: {e}"))),
                Err(std::env::VarError::NotPresent) => Ok(None),
                Err(e) => Err(io::Error::new(io::ErrorKind::InvalidInput, e)),
            }
        };
        let invalid = |msg| io::Error::new(io::ErrorKind::InvalidInput, msg);
        let min_prefetch = read("ZFBENCH_MIN_PREFETCH")?.map(u32::try_from).transpose().map_err(invalid)?;
        let prefetch_ms = read("ZFBENCH_PREFETCH_MS")?;
        let quantum = read("ZFBENCH_QUANTUM")?.map(usize::try_from).transpose().map_err(invalid)?;
        let round_cap = read("ZFBENCH_ROUND_CAP")?.map(usize::try_from).transpose().map_err(invalid)?;
        let connect_parallel = read("ZFBENCH_CONNECT_PARALLEL")?.unwrap_or(1);
        if connect_parallel > 256 {
            return Err(io::Error::new(io::ErrorKind::InvalidInput, "ZFBENCH_CONNECT_PARALLEL exceeds 256"));
        }
        let progress = match std::env::var("ZFBENCH_HEAVY_PROGRESS").as_deref() {
            Ok("1") => true,
            Ok("0") | Err(std::env::VarError::NotPresent) => false,
            _ => return Err(io::Error::new(io::ErrorKind::InvalidInput, "ZFBENCH_HEAVY_PROGRESS must be 0 or 1")),
        };
        Ok(Self { min_prefetch, prefetch_ms, quantum, round_cap, connect_parallel: connect_parallel as usize, progress })
    }

    #[cfg(feature = "zfstack")]
    pub fn apply(self, cfg: &mut zfstack::StackConfig) {
        if let Some(n) = self.min_prefetch { cfg.min_prefetch = n; }
        if let Some(n) = self.prefetch_ms { cfg.prefetch_time = std::time::Duration::from_millis(n); }
        if let Some(n) = self.quantum { cfg.max_quantum = n; }
        if let Some(n) = self.round_cap { cfg.round_bytes_cap = n; }
    }

    pub fn json(self) -> serde_json::Value {
        serde_json::json!({"min_prefetch_bytes": self.min_prefetch, "prefetch_ms": self.prefetch_ms,
            "quantum_bytes": self.quantum, "round_cap_bytes": self.round_cap,
            "connect_parallel": self.connect_parallel, "heavy_progress": self.progress})
    }
}

fn positive(v: &str) -> Result<u64, &'static str> {
    v.parse::<u64>().ok().filter(|n| *n > 0).ok_or("expected a positive integer")
}

#[cfg(all(test, feature = "zfstack"))]
mod tests {
    use super::*;
    #[test]
    fn profiles_keep_their_inflight_window_and_defaults() {
        for mut cfg in [zfstack::StackConfig::default(), zfstack::StackConfig::client()] {
            let before = format!("{cfg:?}");
            TrialTuning::default().apply(&mut cfg);
            assert_eq!(format!("{cfg:?}"), before);
            let flight = cfg.max_snd_inflight;
            let total = cfg.max_snd_buf;
            TrialTuning { min_prefetch: Some(16384), prefetch_ms: Some(4), quantum: Some(16384), round_cap: Some(65536), ..Default::default() }.apply(&mut cfg);
            assert_eq!(cfg.max_snd_inflight, flight);
            assert_eq!(cfg.max_snd_buf, total);
            assert_eq!(cfg.min_prefetch, 16384);
            assert_eq!(cfg.prefetch_time.as_millis(), 4);
        }
        for v in ["", "0", "-1", "4ms", "18446744073709551616"] { assert!(positive(v).is_err()); }
    }
}
