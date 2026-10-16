//! Linux-only diagnostic example.

#[cfg(target_os = "linux")]
include!("linux/pool_idle_probe.rs");

#[cfg(not(target_os = "linux"))]
fn main() -> std::process::ExitCode {
    eprintln!("pool_idle_probe requires Linux; use zfharness for portable sans-IO scenarios.");
    std::process::ExitCode::from(3)
}
