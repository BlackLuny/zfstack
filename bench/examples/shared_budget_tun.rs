//! Linux-only diagnostic example.

#[cfg(target_os = "linux")]
include!("linux/shared_budget_tun.rs");

#[cfg(not(target_os = "linux"))]
fn main() -> std::process::ExitCode {
    eprintln!("shared_budget_tun requires Linux; use zfharness for portable sans-IO scenarios.");
    std::process::ExitCode::from(3)
}
