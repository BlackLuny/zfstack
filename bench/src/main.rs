//! The real-machine benchmark requires Linux TUN devices and network namespaces.

#![recursion_limit = "256"]

#[cfg(target_os = "linux")]
include!("linux.rs");

#[cfg(not(target_os = "linux"))]
fn main() -> std::process::ExitCode {
    eprintln!("zfbench requires Linux (TUN devices and network namespaces); use zfharness for portable sans-IO scenarios.");
    std::process::ExitCode::from(3)
}
