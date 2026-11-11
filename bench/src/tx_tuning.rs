//! Opt-in, test-side TX capacity controls. No library defaults are changed.
use std::io;

#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub struct TxTuning {
    pub max_snd_buf: Option<u64>,
    pub prefetch_max: Option<u32>,
}

impl TxTuning {
    pub fn parse(cap: Option<&str>, prefetch: Option<&str>) -> Result<Self, String> {
        fn positive(value: Option<&str>, name: &str) -> Result<Option<u64>, String> {
            value.map(|s| s.parse::<u64>().ok().filter(|n| *n > 0).ok_or_else(|| format!("{name} must be a positive byte count"))).transpose()
        }
        let max_snd_buf = positive(cap, "ZFBENCH_MAX_SND_BUF")?;
        let prefetch_max = positive(prefetch, "ZFBENCH_PREFETCH_MAX")?
            .map(|n| u32::try_from(n).map_err(|_| "ZFBENCH_PREFETCH_MAX exceeds u32".to_string())).transpose()?;
        Ok(Self { max_snd_buf, prefetch_max })
    }

    pub fn from_env() -> io::Result<Self> {
        let read = |name| match std::env::var(name) {
            Ok(s) => Ok(Some(s)),
            Err(std::env::VarError::NotPresent) => Ok(None),
            Err(e) => Err(io::Error::new(io::ErrorKind::InvalidInput, e)),
        };
        let cap = read("ZFBENCH_MAX_SND_BUF")?;
        let prefetch = read("ZFBENCH_PREFETCH_MAX")?;
        Self::parse(cap.as_deref(), prefetch.as_deref()).map_err(|e| io::Error::new(io::ErrorKind::InvalidInput, e))
    }

    pub fn json(self) -> serde_json::Value {
        serde_json::json!({"max_snd_buf_bytes": self.max_snd_buf, "prefetch_max_bytes": self.prefetch_max})
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[cfg(feature = "zfstack")]
    #[test]
    fn absent_overrides_preserve_both_profiles_and_independent_controls() {
        for mut cfg in [zfstack::StackConfig::default(), zfstack::StackConfig::client()] {
            let before = format!("{cfg:?}");
            let original_inflight = cfg.max_snd_inflight;
            TxTuning::parse(None, None).unwrap().apply(&mut cfg);
            assert_eq!(format!("{cfg:?}"), before);
            let original_prefetch = cfg.prefetch_max;
            TxTuning::parse(Some("262144"), None).unwrap().apply(&mut cfg);
            assert_eq!(cfg.max_snd_buf, 262144);
            assert_eq!(cfg.prefetch_max, original_prefetch);
            TxTuning::parse(None, Some("65536")).unwrap().apply(&mut cfg);
            assert_eq!(cfg.max_snd_buf, 262144);
            assert_eq!(cfg.prefetch_max, 65536);
            assert_eq!(cfg.max_snd_inflight, original_inflight);
        }
    }

    #[test]
    fn invalid_or_overflowing_controls_are_rejected() {
        for value in ["", "0", "-1", "1KiB", "18446744073709551616"] {
            assert!(TxTuning::parse(Some(value), None).is_err());
            assert!(TxTuning::parse(None, Some(value)).is_err());
        }
        assert!(TxTuning::parse(None, Some("4294967296")).is_err());
    }
}
