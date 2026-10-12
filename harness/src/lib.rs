//! Multi-tenant harness for zfstack (issue #24).
//!
//! L0 lives here: real TCP packets, virtual clock, many ifaces and peers, no
//! WireGuard and no TUN. L1 is `zfbench --test isolate`.

pub mod link;
pub mod report;
pub mod scenario;
pub mod world;

pub use scenario::{run_named, scenario_names, Overrides};
pub use world::execute;
