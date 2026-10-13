//! Also run unchanged on the pre-configuration main to compare wire behavior.
#![cfg(feature = "test-peer")]
use std::time::Duration;
use zfstack::sim::{AppConn, LinkParams, Sim};
use zfstack::{PeerId, StackConfig};

#[test]
fn default_profiles_wire_trace() {
    for (profile, cfg) in [("default", StackConfig::default()), ("client", StackConfig::client())] {
        for loss in [0.0, 0.01] {
            let p = LinkParams { loss, reorder: 0.02, duplicate: 0.01, ..Default::default() };
            let mut s = Sim::new(2900, cfg.clone(), cfg.clone(), p.clone(), p);
            s.enable_trace();
            s.a.accept_template = AppConn { to_send: 256 * 1024 + 17, fin_after_send: true, ..Default::default() };
            for i in 0..8 {
                s.connect_as(PeerId(1 + i % 2), 47000 + i as u16, AppConn { to_send: 64 * 1024 + 9, fin_after_send: true, ..Default::default() });
            }
            s.run_until(s.now + Duration::from_secs(10));
            assert_eq!(s.a.conns.len(), 8);
            for c in s.b.conns.values() {
                assert!(c.eof && !c.corrupt && c.received == 256 * 1024 + 17);
            }
            for c in s.a.conns.values() {
                assert!(c.eof && !c.corrupt && c.received == 64 * 1024 + 9);
            }
            println!("TX_DEFAULT_TRACE profile={profile} loss={loss} digest={:?} steps={} time={:?}", s.trace_digest().unwrap(), s.steps, s.now);
        }
    }
}
