//! Standalone executable over the actual production pacer, without rebuilding zfbench.
#[path = "../../src/time.rs"]
mod time;
pub use time::Instant;
#[allow(dead_code)]
#[path = "../../src/iface_pacing.rs"]
mod iface_pacing;

use iface_pacing::IfacePacing;

fn main() {
    let rates = [0, 1, 7, 100, 12_375, 1_237_500, 12_375_000, 123_750_000, u64::MAX];
    let mtus = [68, 576, 1280, 1420, 1500, 9000, u16::MAX];
    let times = [0, 1, 999_999, 1_000_000, 2_000_000, 50_000_000, 500_000_000, 1_000_000_000, u64::MAX - 1, u64::MAX];
    let mut checked = 0;
    for rate in rates {
        for mtu in mtus {
            let mut p = IfacePacing::default();
            p.set_rate(rate, mtu);
            for now in times {
                assert_eq!(p.allowance(Instant::from_nanos(now), false, mtu), (usize::MAX, false));
                assert_eq!(p.deadline(), None);
                checked += 1;
            }
            // Changing a configured inactive rate and MTU also preserves bypass.
            for next in rates {
                p.set_rate(next, mtu);
                p.set_mtu(mtu);
                assert_eq!(p.allowance(Instant::from_nanos(17), false, mtu), (usize::MAX, false));
                assert_eq!(p.deadline(), None);
                checked += 1;
            }
        }
    }
    println!(
        "{{\"bypass_states_checked\":{checked},\"hot_state_bytes\":{},\"cold_clock_bytes\":{},\"production_source_included\":true}}",
        std::mem::size_of::<IfacePacing>(),
        std::mem::size_of::<iface_pacing::IfacePacer>()
    );
}
