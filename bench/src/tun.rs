//! TUN devices, network namespace and cleanup helpers.

use std::io;
use std::os::fd::RawFd;
use std::process::Command;

pub const TUN_A: &str = "zfbA";
pub const TUN_B: &str = "zfbB";
pub const NETNS: &str = "zfbns";
pub const CLIENT_ADDR: &str = "10.201.0.1";
pub const SERVER_ADDR: &str = "10.201.0.2";
pub const SERVER_PORT: u16 = 5201;
pub const MTU: usize = 1420;

const IFF_TUN: libc::c_short = 0x0001;
const IFF_NO_PI: libc::c_short = 0x1000;
const TUNSETIFF: libc::c_ulong = 0x4004_54ca;

#[repr(C)]
struct IfReq {
    name: [u8; libc::IFNAMSIZ],
    flags: libc::c_short,
    _pad: [u8; 22],
}

/// Create (attach to) a non-persistent TUN device in the calling thread's netns.
/// The device disappears when the fd is closed. The fd is non-blocking.
pub fn open_tun(name: &str) -> io::Result<RawFd> {
    let fd = unsafe {
        libc::open(c"/dev/net/tun".as_ptr(), libc::O_RDWR | libc::O_NONBLOCK | libc::O_CLOEXEC)
    };
    if fd < 0 {
        return Err(io::Error::last_os_error());
    }
    let mut req = IfReq { name: [0; libc::IFNAMSIZ], flags: IFF_TUN | IFF_NO_PI, _pad: [0; 22] };
    req.name[..name.len()].copy_from_slice(name.as_bytes());
    if unsafe { libc::ioctl(fd, TUNSETIFF as _, &mut req) } < 0 {
        let e = io::Error::last_os_error();
        unsafe { libc::close(fd) };
        return Err(e);
    }
    Ok(fd)
}

/// Run a command given as a whitespace-separated string; error on non-zero exit.
pub fn sh(cmd: &str) -> io::Result<()> {
    let parts: Vec<&str> = cmd.split_whitespace().collect();
    let out = Command::new(parts[0]).args(&parts[1..]).output()?;
    if !out.status.success() {
        return Err(io::Error::other(format!(
            "`{cmd}` failed: {}",
            String::from_utf8_lossy(&out.stderr).trim()
        )));
    }
    Ok(())
}

/// Like `sh`, ignoring failures (for cleanup).
pub fn sh_quiet(cmd: &str) {
    let parts: Vec<&str> = cmd.split_whitespace().collect();
    let _ = Command::new(parts[0])
        .args(&parts[1..])
        .stdout(std::process::Stdio::null())
        .stderr(std::process::Stdio::null())
        .status();
}

pub fn sh_output(cmd: &str) -> String {
    let parts: Vec<&str> = cmd.split_whitespace().collect();
    Command::new(parts[0])
        .args(&parts[1..])
        .output()
        .map(|o| String::from_utf8_lossy(&o.stdout).into_owned())
        .unwrap_or_default()
}

/// Remove leftovers from a previous (crashed) run.
pub fn cleanup() {
    sh_quiet(&format!("ip netns del {NETNS}"));
    sh_quiet(&format!("ip link del {TUN_A}"));
}

/// Move the calling thread into the named netns.
pub fn enter_netns(name: &str) -> io::Result<()> {
    let path = std::ffi::CString::new(format!("/run/netns/{name}")).unwrap();
    let fd = unsafe { libc::open(path.as_ptr(), libc::O_RDONLY | libc::O_CLOEXEC) };
    if fd < 0 {
        return Err(io::Error::last_os_error());
    }
    let rc = unsafe { libc::setns(fd, libc::CLONE_NEWNET) };
    let e = io::Error::last_os_error();
    unsafe { libc::close(fd) };
    if rc != 0 {
        return Err(e);
    }
    Ok(())
}

fn configure_dev(prefix: &str, dev: &str, addr: &str) -> io::Result<()> {
    sh(&format!("{prefix}ip addr add {addr}/24 dev {dev}"))?;
    sh(&format!("{prefix}ip link set {dev} mtu {MTU} txqueuelen 10000 up"))?;
    // Replace the default (fq_codel) qdisc: we don't want an AQM in front of
    // the emulator. A plain deep FIFO; its drops are reported.
    sh(&format!("{prefix}tc qdisc replace dev {dev} root pfifo limit 10000"))?;
    Ok(())
}

/// Client-side TUN in the root netns.
pub fn setup_a() -> io::Result<RawFd> {
    let fd = open_tun(TUN_A)?;
    configure_dev("", TUN_A, CLIENT_ADDR)?;
    Ok(fd)
}

/// Server-side TUN inside netns `zfbns` (kernel baseline).
pub fn setup_ns_b() -> io::Result<RawFd> {
    sh(&format!("ip netns add {NETNS}"))?;
    let fd = std::thread::spawn(|| -> io::Result<RawFd> {
        enter_netns(NETNS)?;
        open_tun(TUN_B)
    })
    .join()
    .map_err(|_| io::Error::other("netns thread panicked"))??;
    let prefix = format!("ip netns exec {NETNS} ");
    sh(&format!("ip -n {NETNS} link set lo up"))?;
    configure_dev(&prefix, TUN_B, SERVER_ADDR)?;
    Ok(fd)
}

/// Qdisc drop counter of a device ("dropped N" in `tc -s qdisc`).
pub fn qdisc_drops(netns: Option<&str>, dev: &str) -> u64 {
    let cmd = match netns {
        Some(ns) => format!("ip netns exec {ns} tc -s qdisc show dev {dev}"),
        None => format!("tc -s qdisc show dev {dev}"),
    };
    let out = sh_output(&cmd);
    let mut total = 0;
    let mut it = out.split_whitespace();
    while let Some(w) = it.next() {
        if w == "dropped" {
            if let Some(n) = it.next() {
                total += n.trim_end_matches(',').parse::<u64>().unwrap_or(0);
            }
        }
    }
    total
}

/// /sys/class/net/<dev>/statistics/<name> (root netns only).
pub fn dev_stat(dev: &str, name: &str) -> u64 {
    std::fs::read_to_string(format!("/sys/class/net/{dev}/statistics/{name}"))
        .ok()
        .and_then(|s| s.trim().parse().ok())
        .unwrap_or(0)
}
