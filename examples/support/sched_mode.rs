//! Shared experimental input decoding for the probes and harness only.
pub fn apply(cfg: &mut zfstack::StackConfig, mode: &str) -> Result<(), String> {
    cfg.peer_deficit_drr = mode != "off" && !mode.is_empty();
    cfg.peer_drr_quantum = match mode {
        "" | "off" | "drr-max" => 0,
        "drr-16k" => 16 << 10,
        // Default iface 1420, timestamped IPv4 MSS 1368. Jumbo iface floors
        // are applied by the scheduler, independently of connection rates.
        "drr-2mss" => 2 * 1368,
        _ => return Err(format!("unknown scheduling mode {mode}")),
    };
    Ok(())
}
