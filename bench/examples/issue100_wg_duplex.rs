//! Functional diagnostic, run only by issue100_wg_case.py in private namespaces.
#[cfg(target_os = "linux")]
include!("linux/issue100_wg_duplex.rs");

#[cfg(not(target_os = "linux"))]
fn main() {
    eprintln!("issue100_wg_duplex requires Linux TUN and WireGuard");
    std::process::exit(3);
}
