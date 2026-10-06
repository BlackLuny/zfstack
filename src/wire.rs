//! IPv4/IPv6 + TCP parsing and header emission.
//!
//! Scope (§0): no IP fragments (dropped and counted), no IP options interpretation
//! (skipped), IPv6 extension headers other than hop-by-hop / routing / destination
//! options are rejected.

use crate::seq::Seq;
use std::net::{IpAddr, Ipv4Addr, Ipv6Addr};

pub const PROTO_TCP: u8 = 6;

pub const FIN: u8 = 0x01;
pub const SYN: u8 = 0x02;
pub const RST: u8 = 0x04;
pub const PSH: u8 = 0x08;
pub const ACK: u8 = 0x10;
pub const URG: u8 = 0x20;

pub const MAX_SACK_BLOCKS: usize = 4;

#[derive(Copy, Clone, Debug, PartialEq, Eq)]
pub enum DropReason {
    Truncated,
    BadVersion,
    BadIpChecksum,
    Fragment,
    NotTcp,
    BadTcpChecksum,
    BadTcpHeader,
}

#[derive(Copy, Clone, Debug)]
pub struct IpInfo {
    pub src: IpAddr,
    pub dst: IpAddr,
    /// Offset of the TCP header inside the packet.
    pub l4_off: usize,
    pub l4_len: usize,
}

/// Parse the IP layer. Returns the location of the TCP segment.
pub fn parse_ip(pkt: &[u8]) -> Result<IpInfo, DropReason> {
    if pkt.is_empty() {
        return Err(DropReason::Truncated);
    }
    match pkt[0] >> 4 {
        4 => {
            if pkt.len() < 20 {
                return Err(DropReason::Truncated);
            }
            let ihl = ((pkt[0] & 0x0f) as usize) * 4;
            let total = u16::from_be_bytes([pkt[2], pkt[3]]) as usize;
            if ihl < 20 || total < ihl || total > pkt.len() {
                return Err(DropReason::Truncated);
            }
            if checksum_fold(sum_bytes(0, &pkt[..ihl])) != 0 {
                return Err(DropReason::BadIpChecksum);
            }
            let frag = u16::from_be_bytes([pkt[6], pkt[7]]);
            if frag & 0x3fff != 0 {
                // MF set or non-zero fragment offset.
                return Err(DropReason::Fragment);
            }
            if pkt[9] != PROTO_TCP {
                return Err(DropReason::NotTcp);
            }
            let src = Ipv4Addr::new(pkt[12], pkt[13], pkt[14], pkt[15]);
            let dst = Ipv4Addr::new(pkt[16], pkt[17], pkt[18], pkt[19]);
            Ok(IpInfo { src: src.into(), dst: dst.into(), l4_off: ihl, l4_len: total - ihl })
        }
        6 => {
            if pkt.len() < 40 {
                return Err(DropReason::Truncated);
            }
            let plen = u16::from_be_bytes([pkt[4], pkt[5]]) as usize;
            if 40 + plen > pkt.len() {
                return Err(DropReason::Truncated);
            }
            let mut nh = pkt[6];
            let mut off = 40usize;
            let end = 40 + plen;
            loop {
                match nh {
                    PROTO_TCP => break,
                    0 | 43 | 60 => {
                        if off + 8 > end {
                            return Err(DropReason::Truncated);
                        }
                        nh = pkt[off];
                        off += (pkt[off + 1] as usize + 1) * 8;
                        if off > end {
                            return Err(DropReason::Truncated);
                        }
                    }
                    44 => return Err(DropReason::Fragment),
                    _ => return Err(DropReason::NotTcp),
                }
            }
            let mut s = [0u8; 16];
            s.copy_from_slice(&pkt[8..24]);
            let mut d = [0u8; 16];
            d.copy_from_slice(&pkt[24..40]);
            Ok(IpInfo { src: Ipv6Addr::from(s).into(), dst: Ipv6Addr::from(d).into(), l4_off: off, l4_len: end - off })
        }
        _ => Err(DropReason::BadVersion),
    }
}

#[derive(Copy, Clone, Debug, Default, PartialEq, Eq)]
pub struct TcpOptions {
    pub mss: Option<u16>,
    pub wscale: Option<u8>,
    pub sack_perm: bool,
    /// (TSval, TSecr)
    pub ts: Option<(u32, u32)>,
    pub sack: [(Seq, Seq); MAX_SACK_BLOCKS],
    pub sack_n: u8,
}

impl TcpOptions {
    pub fn sack_blocks(&self) -> &[(Seq, Seq)] {
        &self.sack[..self.sack_n as usize]
    }
}

#[derive(Copy, Clone, Debug)]
pub struct TcpHeader {
    pub src_port: u16,
    pub dst_port: u16,
    pub seq: Seq,
    pub ack: Seq,
    pub flags: u8,
    pub window: u16,
    pub opts: TcpOptions,
    /// Payload offset within the TCP segment.
    pub data_off: usize,
}

impl TcpHeader {
    #[inline]
    pub fn has(&self, f: u8) -> bool {
        self.flags & f != 0
    }
}

/// Parse (and checksum-verify) the TCP segment located by `ip`.
pub fn parse_tcp(pkt: &[u8], ip: &IpInfo) -> Result<TcpHeader, DropReason> {
    parse_tcp_with(pkt, ip, true)
}

/// As [`parse_tcp`]; `verify = false` skips the TCP checksum, for packets
/// whose checksum the host's device vouches for (see `RxChecksum`).
pub fn parse_tcp_with(pkt: &[u8], ip: &IpInfo, verify: bool) -> Result<TcpHeader, DropReason> {
    let seg = &pkt[ip.l4_off..ip.l4_off + ip.l4_len];
    if seg.len() < 20 {
        return Err(DropReason::Truncated);
    }
    let doff = ((seg[12] >> 4) as usize) * 4;
    if doff < 20 || doff > seg.len() {
        return Err(DropReason::BadTcpHeader);
    }
    if verify {
        let sum = pseudo_sum(ip.src, ip.dst, seg.len() as u32);
        if checksum_fold(sum_bytes(sum, seg)) != 0 {
            return Err(DropReason::BadTcpChecksum);
        }
    }
    let mut opts = TcpOptions::default();
    let mut o = &seg[20..doff];
    while !o.is_empty() {
        match o[0] {
            0 => break,
            1 => {
                o = &o[1..];
                continue;
            }
            kind => {
                if o.len() < 2 {
                    return Err(DropReason::BadTcpHeader);
                }
                let len = o[1] as usize;
                if len < 2 || len > o.len() {
                    return Err(DropReason::BadTcpHeader);
                }
                let v = &o[2..len];
                match (kind, len) {
                    (2, 4) => opts.mss = Some(u16::from_be_bytes([v[0], v[1]])),
                    (3, 3) => opts.wscale = Some(v[0].min(14)),
                    (4, 2) => opts.sack_perm = true,
                    (5, _) if (len - 2) % 8 == 0 => {
                        for b in v.chunks_exact(8) {
                            if (opts.sack_n as usize) < MAX_SACK_BLOCKS {
                                let l = u32::from_be_bytes([b[0], b[1], b[2], b[3]]);
                                let r = u32::from_be_bytes([b[4], b[5], b[6], b[7]]);
                                opts.sack[opts.sack_n as usize] = (Seq(l), Seq(r));
                                opts.sack_n += 1;
                            }
                        }
                    }
                    (8, 10) => opts.ts = Some((u32::from_be_bytes([v[0], v[1], v[2], v[3]]), u32::from_be_bytes([v[4], v[5], v[6], v[7]]))),
                    _ => {}
                }
                o = &o[len..];
            }
        }
    }
    Ok(TcpHeader {
        src_port: u16::from_be_bytes([seg[0], seg[1]]),
        dst_port: u16::from_be_bytes([seg[2], seg[3]]),
        seq: Seq(u32::from_be_bytes([seg[4], seg[5], seg[6], seg[7]])),
        ack: Seq(u32::from_be_bytes([seg[8], seg[9], seg[10], seg[11]])),
        flags: seg[13],
        window: u16::from_be_bytes([seg[14], seg[15]]),
        opts,
        data_off: doff,
    })
}

// ---------------------------------------------------------------------------
// Checksums

/// Add `data` (starting at an even offset of the summed byte string) to a
/// big-endian one's-complement accumulator; fold with [`checksum_fold`].
#[inline]
pub fn sum_bytes(mut acc: u64, data: &[u8]) -> u64 {
    let words = data.len() & !3;
    if words >= 64 {
        acc += sum_words(&data[..words]);
    } else {
        for c in data[..words].chunks_exact(4) {
            acc += u32::from_be_bytes([c[0], c[1], c[2], c[3]]) as u64;
        }
    }
    let rem = &data[words..];
    match rem.len() {
        1 => acc += (rem[0] as u64) << 8,
        2 => acc += u16::from_be_bytes([rem[0], rem[1]]) as u64,
        3 => acc += (u16::from_be_bytes([rem[0], rem[1]]) as u64) + ((rem[2] as u64) << 8),
        _ => {}
    }
    acc
}

/// One's-complement sum of a multiple of 4 bytes, folded to 16 bits and
/// returned in the big-endian domain of [`sum_bytes`].
///
/// Native-endian 32-bit words go into independent 64-bit lanes, which the
/// compiler vectorizes; the one's-complement sum only differs between byte
/// orders by a final byte swap (RFC 1071 §2(B)). Payload checksums are the
/// main per-byte cost of TUN-facing hosts, so AVX2 is used when present.
#[inline]
fn sum_words(data: &[u8]) -> u64 {
    #[cfg(all(target_arch = "x86_64", not(target_feature = "avx2")))]
    {
        if std::is_x86_feature_detected!("avx2") {
            // SAFETY: the CPU supports AVX2 (checked above).
            return unsafe { sum_words_avx2(data) };
        }
    }
    sum_words_portable(data)
}

#[cfg(all(target_arch = "x86_64", not(target_feature = "avx2")))]
#[target_feature(enable = "avx2")]
unsafe fn sum_words_avx2(data: &[u8]) -> u64 {
    sum_words_portable(data)
}

#[inline(always)]
fn sum_words_portable(data: &[u8]) -> u64 {
    debug_assert_eq!(data.len() % 4, 0);
    let mut lanes = [0u64; 8];
    let mut blocks = data.chunks_exact(32);
    for b in &mut blocks {
        for (i, lane) in lanes.iter_mut().enumerate() {
            *lane += u32::from_ne_bytes([b[4 * i], b[4 * i + 1], b[4 * i + 2], b[4 * i + 3]]) as u64;
        }
    }
    // Each lane holds at most 2^16 · 2^32 for a 64 KiB packet; adding with
    // end-around carry keeps any length exact.
    let mut s = lanes.iter().fold(0u64, |a, &l| {
        let (v, c) = a.overflowing_add(l);
        v + c as u64
    });
    for c in blocks.remainder().chunks_exact(4) {
        let (v, carry) = s.overflowing_add(u32::from_ne_bytes([c[0], c[1], c[2], c[3]]) as u64);
        s = v + carry as u64;
    }
    while s >> 16 != 0 {
        s = (s & 0xffff) + (s >> 16);
    }
    if cfg!(target_endian = "little") {
        (s as u16).swap_bytes() as u64
    } else {
        s
    }
}

/// Sum of possibly-odd-length slices treated as one contiguous byte string.
pub fn sum_slices(mut acc: u64, parts: &[&[u8]]) -> u64 {
    let mut odd = false;
    for p in parts {
        if p.is_empty() {
            continue;
        }
        if odd {
            // Previous slice ended on an odd byte: this slice's first byte is a low byte.
            acc += p[0] as u64;
            let s = sum_bytes(0, &p[1..]);
            acc += s;
            odd = (p.len() - 1) % 2 == 1;
        } else {
            acc += sum_bytes(0, p);
            odd = p.len() % 2 == 1;
        }
    }
    acc
}

#[inline]
pub fn checksum_fold(mut acc: u64) -> u16 {
    while acc >> 16 != 0 {
        acc = (acc & 0xffff) + (acc >> 16);
    }
    !(acc as u16)
}

pub fn pseudo_sum(src: IpAddr, dst: IpAddr, l4_len: u32) -> u64 {
    let mut acc = 0u64;
    match (src, dst) {
        (IpAddr::V4(s), IpAddr::V4(d)) => {
            acc = sum_bytes(acc, &s.octets());
            acc = sum_bytes(acc, &d.octets());
            acc += PROTO_TCP as u64 + l4_len as u64;
        }
        (IpAddr::V6(s), IpAddr::V6(d)) => {
            acc = sum_bytes(acc, &s.octets());
            acc = sum_bytes(acc, &d.octets());
            acc += l4_len as u64 + PROTO_TCP as u64;
        }
        _ => {}
    }
    acc
}

// ---------------------------------------------------------------------------
// Emission

/// Maximum IP+TCP header size we ever emit (IPv6 40 + TCP 60).
pub const MAX_HEADER: usize = 100;

#[derive(Clone, Debug, Default)]
pub struct EmitOptions {
    pub mss: Option<u16>,
    pub wscale: Option<u8>,
    pub sack_perm: bool,
    pub ts: Option<(u32, u32)>,
    pub sack: [(Seq, Seq); MAX_SACK_BLOCKS],
    pub sack_n: u8,
}

pub struct EmitParams<'a> {
    pub src: IpAddr,
    pub dst: IpAddr,
    pub src_port: u16,
    pub dst_port: u16,
    pub seq: Seq,
    pub ack: Seq,
    pub flags: u8,
    pub window: u16,
    pub opts: &'a EmitOptions,
    pub payload: &'a [&'a [u8]],
    pub ttl: u8,
    /// Leave the TCP checksum partial for the device to complete (TX checksum
    /// offload): the field holds the folded pseudo-header sum, as Linux
    /// `CHECKSUM_PARTIAL` expects, and the payload is not summed.
    pub csum_partial: bool,
}

fn options_len(o: &EmitOptions) -> usize {
    let mut n = 0;
    if o.mss.is_some() {
        n += 4;
    }
    if o.wscale.is_some() {
        n += 4; // NOP + 3
    }
    if o.sack_perm {
        n += if o.ts.is_some() { 2 } else { 4 };
    }
    if o.ts.is_some() {
        n += if o.sack_perm { 10 } else { 12 };
    }
    if o.sack_n > 0 {
        n += 4 + 8 * o.sack_n as usize;
    }
    (n + 3) & !3
}

/// Number of SACK blocks that fit next to the other options.
pub fn max_sack_blocks(ts: bool) -> usize {
    if ts {
        3
    } else {
        4
    }
}

/// TCP header length (incl. options) for the given options.
pub fn tcp_header_len(o: &EmitOptions) -> usize {
    20 + options_len(o)
}

/// Write IP + TCP headers into `buf`, returning the header length.
/// The TCP checksum covers `p.payload`.
pub fn emit(buf: &mut [u8; MAX_HEADER], p: &EmitParams) -> usize {
    let payload_len: usize = p.payload.iter().map(|s| s.len()).sum();
    let olen = options_len(p.opts);
    let tcp_hlen = 20 + olen;
    let ip_hlen = match p.src {
        IpAddr::V4(_) => 20,
        IpAddr::V6(_) => 40,
    };
    let l4_len = tcp_hlen + payload_len;
    match (p.src, p.dst) {
        (IpAddr::V4(s), IpAddr::V4(d)) => {
            let h = &mut buf[..20];
            h[0] = 0x45;
            h[1] = 0;
            h[2..4].copy_from_slice(&((20 + l4_len) as u16).to_be_bytes());
            h[4..6].copy_from_slice(&[0, 0]);
            h[6..8].copy_from_slice(&[0x40, 0]); // DF
            h[8] = p.ttl;
            h[9] = PROTO_TCP;
            h[10..12].copy_from_slice(&[0, 0]);
            h[12..16].copy_from_slice(&s.octets());
            h[16..20].copy_from_slice(&d.octets());
            let c = checksum_fold(sum_bytes(0, h));
            h[10..12].copy_from_slice(&c.to_be_bytes());
        }
        (IpAddr::V6(s), IpAddr::V6(d)) => {
            let h = &mut buf[..40];
            h[0..4].copy_from_slice(&[0x60, 0, 0, 0]);
            h[4..6].copy_from_slice(&(l4_len as u16).to_be_bytes());
            h[6] = PROTO_TCP;
            h[7] = p.ttl;
            h[8..24].copy_from_slice(&s.octets());
            h[24..40].copy_from_slice(&d.octets());
        }
        _ => panic!("mixed address families"),
    }
    let t = &mut buf[ip_hlen..ip_hlen + tcp_hlen];
    t[0..2].copy_from_slice(&p.src_port.to_be_bytes());
    t[2..4].copy_from_slice(&p.dst_port.to_be_bytes());
    t[4..8].copy_from_slice(&p.seq.0.to_be_bytes());
    t[8..12].copy_from_slice(&p.ack.0.to_be_bytes());
    t[12] = ((tcp_hlen / 4) as u8) << 4;
    t[13] = p.flags;
    t[14..16].copy_from_slice(&p.window.to_be_bytes());
    t[16..20].copy_from_slice(&[0, 0, 0, 0]);
    let mut i = 20;
    let o = p.opts;
    if let Some(m) = o.mss {
        t[i..i + 4].copy_from_slice(&[2, 4, (m >> 8) as u8, m as u8]);
        i += 4;
    }
    if let Some(ws) = o.wscale {
        t[i..i + 4].copy_from_slice(&[1, 3, 3, ws]);
        i += 4;
    }
    match (o.sack_perm, o.ts) {
        (true, Some((v, e))) => {
            t[i..i + 2].copy_from_slice(&[4, 2]);
            t[i + 2..i + 4].copy_from_slice(&[8, 10]);
            t[i + 4..i + 8].copy_from_slice(&v.to_be_bytes());
            t[i + 8..i + 12].copy_from_slice(&e.to_be_bytes());
            i += 12;
        }
        (true, None) => {
            t[i..i + 4].copy_from_slice(&[1, 1, 4, 2]);
            i += 4;
        }
        (false, Some((v, e))) => {
            t[i..i + 4].copy_from_slice(&[1, 1, 8, 10]);
            t[i + 4..i + 8].copy_from_slice(&v.to_be_bytes());
            t[i + 8..i + 12].copy_from_slice(&e.to_be_bytes());
            i += 12;
        }
        (false, None) => {}
    }
    if o.sack_n > 0 {
        t[i..i + 4].copy_from_slice(&[1, 1, 5, 2 + 8 * o.sack_n]);
        i += 4;
        for &(l, r) in &o.sack[..o.sack_n as usize] {
            t[i..i + 4].copy_from_slice(&l.0.to_be_bytes());
            t[i + 4..i + 8].copy_from_slice(&r.0.to_be_bytes());
            i += 8;
        }
    }
    while i < tcp_hlen {
        t[i] = 0; // EOL padding
        i += 1;
    }
    let mut acc = pseudo_sum(p.src, p.dst, l4_len as u32);
    if p.csum_partial {
        // Not inverted: the device adds the segment and complements.
        let c = !checksum_fold(acc);
        t[16..18].copy_from_slice(&c.to_be_bytes());
        return ip_hlen + tcp_hlen;
    }
    acc = sum_bytes(acc, t);
    // TCP header length is a multiple of 4, so payload starts at an even offset.
    acc = sum_slices(acc, p.payload);
    let c = checksum_fold(acc);
    t[16..18].copy_from_slice(&c.to_be_bytes());
    ip_hlen + tcp_hlen
}

pub fn ip_header_len(a: IpAddr) -> usize {
    match a {
        IpAddr::V4(_) => 20,
        IpAddr::V6(_) => 40,
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn roundtrip(src: IpAddr, dst: IpAddr) {
        let mut opts = EmitOptions { mss: Some(1400), wscale: Some(7), sack_perm: true, ts: Some((123, 456)), ..Default::default() };
        opts.sack[0] = (Seq(10), Seq(20));
        opts.sack_n = 1;
        let a: &[u8] = b"hello";
        let b: &[u8] = b" world!";
        let payload = [a, b];
        let mut hdr = [0u8; MAX_HEADER];
        let hl = emit(
            &mut hdr,
            &EmitParams {
                src,
                dst,
                src_port: 80,
                dst_port: 12345,
                seq: Seq(1000),
                ack: Seq(2000),
                flags: ACK | PSH,
                window: 777,
                opts: &opts,
                payload: &payload,
                ttl: 64,
                csum_partial: false,
            },
        );
        let mut pkt = hdr[..hl].to_vec();
        pkt.extend_from_slice(a);
        pkt.extend_from_slice(b);
        let ip = parse_ip(&pkt).unwrap();
        assert_eq!(ip.src, src);
        let t = parse_tcp(&pkt, &ip).unwrap();
        assert_eq!(t.seq, Seq(1000));
        assert_eq!(t.ack, Seq(2000));
        assert_eq!(t.window, 777);
        assert_eq!(t.opts.mss, Some(1400));
        assert_eq!(t.opts.wscale, Some(7));
        assert!(t.opts.sack_perm);
        assert_eq!(t.opts.ts, Some((123, 456)));
        assert_eq!(t.opts.sack_blocks(), &[(Seq(10), Seq(20))]);
        assert_eq!(&pkt[ip.l4_off + t.data_off..], b"hello world!");
        // Corrupt payload -> checksum failure.
        let n = pkt.len();
        pkt[n - 1] ^= 1;
        assert_eq!(parse_tcp(&pkt, &ip).unwrap_err(), DropReason::BadTcpChecksum);
    }

    #[test]
    fn v4_roundtrip() {
        roundtrip("10.0.0.1".parse().unwrap(), "10.0.0.2".parse().unwrap());
    }

    #[test]
    fn v6_roundtrip() {
        roundtrip("fd00::1".parse().unwrap(), "fd00::2".parse().unwrap());
    }

    #[test]
    fn partial_checksum_completes_like_a_device() {
        for (src, dst) in [("10.0.0.1", "10.0.0.2"), ("fd00::1", "fd00::2")] {
            let (src, dst): (IpAddr, IpAddr) = (src.parse().unwrap(), dst.parse().unwrap());
            let opts = EmitOptions { ts: Some((1, 2)), ..Default::default() };
            let a: &[u8] = &[7u8; 1001];
            let b: &[u8] = &[9u8; 33];
            let mut hdr = [0u8; MAX_HEADER];
            let payload = [a, b];
            let params = |csum_partial| EmitParams {
                src,
                dst,
                src_port: 443,
                dst_port: 50000,
                seq: Seq(5),
                ack: Seq(6),
                flags: ACK,
                window: 100,
                opts: &opts,
                payload: &payload,
                ttl: 64,
                csum_partial,
            };
            let hl = emit(&mut hdr, &params(true));
            let mut pkt = [&hdr[..hl], a, b].concat();
            let ip = parse_ip(&pkt).unwrap();
            assert_eq!(parse_tcp(&pkt, &ip).unwrap_err(), DropReason::BadTcpChecksum);
            assert!(parse_tcp_with(&pkt, &ip, false).is_ok());
            // What the device does: sum from csum_start, store the complement.
            let start = ip.l4_off;
            let c = checksum_fold(sum_bytes(0, &pkt[start..]));
            pkt[start + 16..start + 18].copy_from_slice(&c.to_be_bytes());
            assert!(parse_tcp(&pkt, &ip).is_ok());
            let full = emit(&mut hdr, &params(false));
            assert_eq!(&pkt[..full], &hdr[..full]);
        }
    }

    #[test]
    fn vectorized_sum_matches_reference() {
        fn reference(data: &[u8]) -> u16 {
            let mut acc = 0u64;
            for c in data.chunks(2) {
                acc += if c.len() == 2 { u16::from_be_bytes([c[0], c[1]]) as u64 } else { (c[0] as u64) << 8 };
            }
            checksum_fold(acc)
        }
        let mut x = 0x9e37_79b9u32;
        let data: Vec<u8> = (0..70_000)
            .map(|_| {
                x ^= x << 13;
                x ^= x >> 17;
                x ^= x << 5;
                (x >> 7) as u8
            })
            .collect();
        for len in (0..200).chain([1459, 1460, 4011, 8947, 65495, 65535, 69_999]) {
            for off in [0, 1, 3, 4] {
                let d = &data[off..(off + len).min(data.len())];
                assert_eq!(checksum_fold(sum_bytes(0, d)), reference(d), "len {len} off {off}");
            }
        }
        assert_eq!(checksum_fold(sum_bytes(0, &[0xff; 4096])), reference(&[0xff; 4096]));
        assert_eq!(checksum_fold(sum_bytes(0, &[0; 4096])), 0xffff);
    }

    #[test]
    fn odd_slices_checksum() {
        let whole: Vec<u8> = (0..101u8).collect();
        let s = checksum_fold(sum_bytes(0, &whole));
        for cut in 0..whole.len() {
            let (a, b) = whole.split_at(cut);
            assert_eq!(checksum_fold(sum_slices(0, &[a, b])), s, "cut {cut}");
        }
        let (a, rest) = whole.split_at(3);
        let (b, c) = rest.split_at(5);
        assert_eq!(checksum_fold(sum_slices(0, &[a, b, c])), s);
    }
}
