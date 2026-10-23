//! Apply portable experiment controls inside the existing zfstack feature boundary.
use crate::trial_tuning::TrialTuning;
use crate::tx_tuning::TxTuning;

impl TxTuning {
    pub fn apply(self, cfg: &mut zfstack::StackConfig) {
        if let Some(n) = self.max_snd_buf {
            cfg.max_snd_buf = n;
        }
        if let Some(n) = self.prefetch_max {
            cfg.prefetch_max = n;
        }
    }
}

impl TrialTuning {
    pub fn apply(self, cfg: &mut zfstack::StackConfig) {
        if let Some(n) = self.min_prefetch {
            cfg.min_prefetch = n;
        }
        if let Some(n) = self.prefetch_ms {
            cfg.prefetch_time = std::time::Duration::from_millis(n);
        }
        if let Some(n) = self.quantum {
            cfg.max_quantum = n;
        }
        if let Some(n) = self.round_cap {
            cfg.round_bytes_cap = n;
        }
    }
}
