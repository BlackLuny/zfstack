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
            if !ipv4_header_checksum_ok(&pkt[..ihl]) {
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

#[inline]
fn fold_u64(acc: u64) -> u16 {
    let (a, c1) = (acc as u32).overflowing_add((acc >> 32) as u32);
    let a = a.wrapping_add(c1 as u32);
    let (b, c2) = (a as u16).overflowing_add((a >> 16) as u16);
    b.wrapping_add(c2 as u16)
}

#[inline]
fn ipv4_header_checksum_ok(hdr: &[u8]) -> bool {
    debug_assert!(hdr.len() >= 20 && hdr.len() % 4 == 0);
    let mut s = u32::from_ne_bytes([hdr[0], hdr[1], hdr[2], hdr[3]]) as u64
        + u32::from_ne_bytes([hdr[4], hdr[5], hdr[6], hdr[7]]) as u64
        + u32::from_ne_bytes([hdr[8], hdr[9], hdr[10], hdr[11]]) as u64
        + u32::from_ne_bytes([hdr[12], hdr[13], hdr[14], hdr[15]]) as u64
        + u32::from_ne_bytes([hdr[16], hdr[17], hdr[18], hdr[19]]) as u64;
    if hdr.len() > 20 {
        for c in hdr[20..].chunks_exact(4) {
            s += u32::from_ne_bytes([c[0], c[1], c[2], c[3]]) as u64;
        }
    }
    fold_u64(s) == 0xffff
}

/// Add `data` (starting at an even offset of the summed byte string) to a
/// big-endian one's-complement accumulator; fold with [`checksum_fold`].
#[inline]
pub fn sum_bytes(mut acc: u64, data: &[u8]) -> u64 {
    let words = data.len() & !3;
    let rem = &data[words..];
    if !rem.is_empty() {
        if rem.len() >= 2 {
            acc += u16::from_be_bytes([rem[0], rem[1]]) as u64;
        }
        if (rem.len() & 1) != 0 {
            acc += (rem[rem.len() - 1] as u64) << 8;
        }
    }
    if words >= 64 {
        sum_words(acc, &data[..words])
    } else if words != 0 {
        acc + sum_small_words(&data[..words])
    } else {
        acc
    }
}

#[inline]
fn sum_small_words(mut d: &[u8]) -> u64 {
    debug_assert!(d.len() < 64 && d.len() % 4 == 0);
    let mut s = 0u128;
    if d.len() >= 32 {
        let (a, b) = d.split_at(32);
        let q0 = u64::from_ne_bytes([a[0], a[1], a[2], a[3], a[4], a[5], a[6], a[7]]) as u128;
        let q1 = u64::from_ne_bytes([a[8], a[9], a[10], a[11], a[12], a[13], a[14], a[15]]) as u128;
        let q2 = u64::from_ne_bytes([a[16], a[17], a[18], a[19], a[20], a[21], a[22], a[23]]) as u128;
        let q3 = u64::from_ne_bytes([a[24], a[25], a[26], a[27], a[28], a[29], a[30], a[31]]) as u128;
        s += q0 + q1 + q2 + q3;
        d = b;
    }
    if d.len() >= 16 {
        let (a, b) = d.split_at(16);
        let q0 = u64::from_ne_bytes([a[0], a[1], a[2], a[3], a[4], a[5], a[6], a[7]]) as u128;
        let q1 = u64::from_ne_bytes([a[8], a[9], a[10], a[11], a[12], a[13], a[14], a[15]]) as u128;
        s += q0 + q1;
        d = b;
    }
    if d.len() >= 8 {
        let (a, b) = d.split_at(8);
        s += u64::from_ne_bytes([a[0], a[1], a[2], a[3], a[4], a[5], a[6], a[7]]) as u128;
        d = b;
    }
    if d.len() >= 4 {
        s += u32::from_ne_bytes([d[0], d[1], d[2], d[3]]) as u128;
    }
    let (lo, c) = (s as u64).overflowing_add((s >> 64) as u64);
    let folded = fold_u64(lo.wrapping_add(c as u64));
    if cfg!(target_endian = "little") {
        folded.swap_bytes() as u64
    } else {
        folded as u64
    }
}

/// One's-complement sum of a multiple of 4 bytes, folded to 16 bits and
/// returned in the big-endian domain of [`sum_bytes`].
///
/// 32-bit words (in `sum_words_portable`) or 16-bit words (in `sum_words_avx2`)
/// are accumulated in native byte order; the one's-complement sum only differs
/// between byte orders by a final byte swap (RFC 1071 §2(B)).
/// The 8-lane `u64` structure in `sum_words_portable` with tree reduction lets
/// LLVM auto-vectorize into NEON `uaddw`/`uaddw2` on AArch64, SSE2 `punpckldq`/
/// `punpckhdq` + `paddq` on baseline `x86_64`, and `vpmovzxdq` + `vpaddq` when
/// compiled with target-feature `avx2`.
#[inline(never)]
fn sum_words(acc: u64, data: &[u8]) -> u64 {
    #[cfg(all(target_arch = "x86_64", not(target_feature = "avx2")))]
    {
        if std::is_x86_feature_detected!("avx2") {
            // SAFETY: the CPU supports AVX2 (checked above).
            return unsafe { sum_words_avx2(acc, data) };
        }
    }
    acc + sum_words_portable(data)
}

#[cfg(all(target_arch = "x86_64", not(target_feature = "avx2")))]
#[target_feature(enable = "avx2")]
unsafe fn sum_words_avx2(acc: u64, data: &[u8]) -> u64 {
    use core::arch::x86_64::*;
    debug_assert_eq!(data.len() % 4, 0);
    const MAX_CHUNK: usize = 262_144;
    let reduce = |acc0: __m256i, acc1: __m256i, acc2: __m256i, acc3: __m256i| -> u64 {
        let sum01 = _mm256_add_epi32(acc0, acc1);
        let sum23 = _mm256_add_epi32(acc2, acc3);
        let sum256 = _mm256_add_epi32(sum01, sum23);
        let sum128 = _mm_add_epi32(_mm256_castsi256_si128(sum256), _mm256_extracti128_si256::<1>(sum256));
        let lo64 = _mm_cvtsi128_si64(sum128) as u64;
        let hi64 = _mm_extract_epi64::<1>(sum128) as u64;
        let pair = lo64 + hi64;
        (pair & 0xffff_ffff) + (pair >> 32)
    };
    let mut p = data.as_ptr();
    let mut rem = data.len();
    let mut total = 0u64;
    let mask = _mm256_set1_epi32(0x0000_ffff);

    while rem > MAX_CHUNK {
        let mut acc0 = _mm256_setzero_si256();
        let mut acc1 = _mm256_setzero_si256();
        let mut acc2 = _mm256_setzero_si256();
        let mut acc3 = _mm256_setzero_si256();
        let end = p.add(MAX_CHUNK);
        while p < end {
            let v0 = _mm256_loadu_si256(p.cast());
            let v1 = _mm256_loadu_si256(p.add(128).cast());
            acc0 = _mm256_add_epi32(acc0, _mm256_and_si256(v0, mask));
            acc1 = _mm256_add_epi32(acc1, _mm256_srli_epi32::<16>(v0));
            acc2 = _mm256_add_epi32(acc2, _mm256_cvtepu16_epi32(_mm_loadu_si128(p.add(32).cast())));
            acc3 = _mm256_add_epi32(acc3, _mm256_cvtepu16_epi32(_mm_loadu_si128(p.add(48).cast())));
            acc0 = _mm256_add_epi32(acc0, _mm256_cvtepu16_epi32(_mm_loadu_si128(p.add(64).cast())));
            acc1 = _mm256_add_epi32(acc1, _mm256_cvtepu16_epi32(_mm_loadu_si128(p.add(80).cast())));
            acc2 = _mm256_add_epi32(acc2, _mm256_cvtepu16_epi32(_mm_loadu_si128(p.add(96).cast())));
            acc3 = _mm256_add_epi32(acc3, _mm256_cvtepu16_epi32(_mm_loadu_si128(p.add(112).cast())));
            acc0 = _mm256_add_epi32(acc0, _mm256_and_si256(v1, mask));
            acc1 = _mm256_add_epi32(acc1, _mm256_srli_epi32::<16>(v1));
            acc2 = _mm256_add_epi32(acc2, _mm256_cvtepu16_epi32(_mm_loadu_si128(p.add(160).cast())));
            acc3 = _mm256_add_epi32(acc3, _mm256_cvtepu16_epi32(_mm_loadu_si128(p.add(176).cast())));
            acc0 = _mm256_add_epi32(acc0, _mm256_cvtepu16_epi32(_mm_loadu_si128(p.add(192).cast())));
            acc1 = _mm256_add_epi32(acc1, _mm256_cvtepu16_epi32(_mm_loadu_si128(p.add(208).cast())));
            acc2 = _mm256_add_epi32(acc2, _mm256_cvtepu16_epi32(_mm_loadu_si128(p.add(224).cast())));
            acc3 = _mm256_add_epi32(acc3, _mm256_cvtepu16_epi32(_mm_loadu_si128(p.add(240).cast())));
            p = p.add(256);
        }
        total = (total & 0xffff_ffff) + (total >> 32) + reduce(acc0, acc1, acc2, acc3);
        rem -= MAX_CHUNK;
    }

    let mut acc0 = _mm256_setzero_si256();
    let mut acc1 = _mm256_setzero_si256();
    let mut acc2 = _mm256_setzero_si256();
    let mut acc3 = _mm256_setzero_si256();
    let n256 = rem & !255;
    if n256 != 0 {
        let end = p.add(n256);
        while p < end {
            let v0 = _mm256_loadu_si256(p.cast());
            let v1 = _mm256_loadu_si256(p.add(128).cast());
            acc0 = _mm256_add_epi32(acc0, _mm256_and_si256(v0, mask));
            acc1 = _mm256_add_epi32(acc1, _mm256_srli_epi32::<16>(v0));
            acc2 = _mm256_add_epi32(acc2, _mm256_cvtepu16_epi32(_mm_loadu_si128(p.add(32).cast())));
            acc3 = _mm256_add_epi32(acc3, _mm256_cvtepu16_epi32(_mm_loadu_si128(p.add(48).cast())));
            acc0 = _mm256_add_epi32(acc0, _mm256_cvtepu16_epi32(_mm_loadu_si128(p.add(64).cast())));
            acc1 = _mm256_add_epi32(acc1, _mm256_cvtepu16_epi32(_mm_loadu_si128(p.add(80).cast())));
            acc2 = _mm256_add_epi32(acc2, _mm256_cvtepu16_epi32(_mm_loadu_si128(p.add(96).cast())));
            acc3 = _mm256_add_epi32(acc3, _mm256_cvtepu16_epi32(_mm_loadu_si128(p.add(112).cast())));
            acc0 = _mm256_add_epi32(acc0, _mm256_and_si256(v1, mask));
            acc1 = _mm256_add_epi32(acc1, _mm256_srli_epi32::<16>(v1));
            acc2 = _mm256_add_epi32(acc2, _mm256_cvtepu16_epi32(_mm_loadu_si128(p.add(160).cast())));
            acc3 = _mm256_add_epi32(acc3, _mm256_cvtepu16_epi32(_mm_loadu_si128(p.add(176).cast())));
            acc0 = _mm256_add_epi32(acc0, _mm256_cvtepu16_epi32(_mm_loadu_si128(p.add(192).cast())));
            acc1 = _mm256_add_epi32(acc1, _mm256_cvtepu16_epi32(_mm_loadu_si128(p.add(208).cast())));
            acc2 = _mm256_add_epi32(acc2, _mm256_cvtepu16_epi32(_mm_loadu_si128(p.add(224).cast())));
            acc3 = _mm256_add_epi32(acc3, _mm256_cvtepu16_epi32(_mm_loadu_si128(p.add(240).cast())));
            p = p.add(256);
        }
    }
    if rem & 128 != 0 {
        let v0 = _mm256_loadu_si256(p.cast());
        acc0 = _mm256_add_epi32(acc0, _mm256_and_si256(v0, mask));
        acc1 = _mm256_add_epi32(acc1, _mm256_srli_epi32::<16>(v0));
        acc2 = _mm256_add_epi32(acc2, _mm256_cvtepu16_epi32(_mm_loadu_si128(p.add(32).cast())));
        acc3 = _mm256_add_epi32(acc3, _mm256_cvtepu16_epi32(_mm_loadu_si128(p.add(48).cast())));
        acc0 = _mm256_add_epi32(acc0, _mm256_cvtepu16_epi32(_mm_loadu_si128(p.add(64).cast())));
        acc1 = _mm256_add_epi32(acc1, _mm256_cvtepu16_epi32(_mm_loadu_si128(p.add(80).cast())));
        acc2 = _mm256_add_epi32(acc2, _mm256_cvtepu16_epi32(_mm_loadu_si128(p.add(96).cast())));
        acc3 = _mm256_add_epi32(acc3, _mm256_cvtepu16_epi32(_mm_loadu_si128(p.add(112).cast())));
        p = p.add(128);
    }
    if rem & 64 != 0 {
        acc0 = _mm256_add_epi32(acc0, _mm256_cvtepu16_epi32(_mm_loadu_si128(p.cast())));
        acc1 = _mm256_add_epi32(acc1, _mm256_cvtepu16_epi32(_mm_loadu_si128(p.add(16).cast())));
        acc2 = _mm256_add_epi32(acc2, _mm256_cvtepu16_epi32(_mm_loadu_si128(p.add(32).cast())));
        acc3 = _mm256_add_epi32(acc3, _mm256_cvtepu16_epi32(_mm_loadu_si128(p.add(48).cast())));
        p = p.add(64);
    }
    if rem & 32 != 0 {
        acc0 = _mm256_add_epi32(acc0, _mm256_cvtepu16_epi32(_mm_loadu_si128(p.cast())));
        acc1 = _mm256_add_epi32(acc1, _mm256_cvtepu16_epi32(_mm_loadu_si128(p.add(16).cast())));
        p = p.add(32);
    }
    if rem & 16 != 0 {
        acc0 = _mm256_add_epi32(acc0, _mm256_cvtepu16_epi32(_mm_loadu_si128(p.cast())));
        p = p.add(16);
    }
    let mut s = total + reduce(acc0, acc1, acc2, acc3);
    if rem & 8 != 0 {
        let w0 = p.cast::<u32>().read_unaligned() as u64;
        let w1 = p.add(4).cast::<u32>().read_unaligned() as u64;
        s += w0 + w1;
        p = p.add(8);
    }
    if rem & 4 != 0 {
        s += p.cast::<u32>().read_unaligned() as u64;
    }
    acc + (fold_u64(s).swap_bytes() as u64)
}

#[inline(always)]
fn sum_words_portable(data: &[u8]) -> u64 {
    debug_assert_eq!(data.len() % 4, 0);
    let mut lanes = [0u64; 8];
    let (bulk, mut rem) = data.split_at(data.len() & !31);
    for b in bulk.chunks_exact(32) {
        for (lane, w) in lanes.iter_mut().zip(b.chunks_exact(4)) {
            *lane += u32::from_ne_bytes([w[0], w[1], w[2], w[3]]) as u64;
        }
    }
    let s0 = (lanes[0] + lanes[1]) + (lanes[2] + lanes[3]);
    let s1 = (lanes[4] + lanes[5]) + (lanes[6] + lanes[7]);
    let mut s = s0 + s1;
    if rem.len() >= 16 {
        let (a, b) = rem.split_at(16);
        let w0 = u32::from_ne_bytes([a[0], a[1], a[2], a[3]]) as u64;
        let w1 = u32::from_ne_bytes([a[4], a[5], a[6], a[7]]) as u64;
        let w2 = u32::from_ne_bytes([a[8], a[9], a[10], a[11]]) as u64;
        let w3 = u32::from_ne_bytes([a[12], a[13], a[14], a[15]]) as u64;
        s += (w0 + w1) + (w2 + w3);
        rem = b;
    }
    if rem.len() >= 8 {
        let (a, b) = rem.split_at(8);
        let w0 = u32::from_ne_bytes([a[0], a[1], a[2], a[3]]) as u64;
        let w1 = u32::from_ne_bytes([a[4], a[5], a[6], a[7]]) as u64;
        s += w0 + w1;
        rem = b;
    }
    if rem.len() >= 4 {
        s += u32::from_ne_bytes([rem[0], rem[1], rem[2], rem[3]]) as u64;
    }
    let folded = fold_u64(s);
    if cfg!(target_endian = "little") {
        folded.swap_bytes() as u64
    } else {
        folded as u64
    }
}

/// Sum of possibly-odd-length slices treated as one contiguous byte string.
#[inline]
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
pub fn checksum_fold(acc: u64) -> u16 {
    !fold_u64(acc)
}

#[inline]
fn ipv4_addr_sum(s: Ipv4Addr, d: Ipv4Addr) -> u64 {
    u32::from(s) as u64 + u32::from(d) as u64
}

#[inline]
fn ipv4_pseudo_sum(s: Ipv4Addr, d: Ipv4Addr, l4_len: u32) -> u64 {
    ipv4_addr_sum(s, d) + l4_len as u64 + PROTO_TCP as u64
}

#[inline]
fn ipv6_pseudo_sum(s: Ipv6Addr, d: Ipv6Addr, l4_len: u32) -> u64 {
    let a = s.octets();
    let b = d.octets();
    let addr_sum = u32::from_be_bytes([a[0], a[1], a[2], a[3]]) as u64
        + u32::from_be_bytes([a[4], a[5], a[6], a[7]]) as u64
        + u32::from_be_bytes([a[8], a[9], a[10], a[11]]) as u64
        + u32::from_be_bytes([a[12], a[13], a[14], a[15]]) as u64
        + u32::from_be_bytes([b[0], b[1], b[2], b[3]]) as u64
        + u32::from_be_bytes([b[4], b[5], b[6], b[7]]) as u64
        + u32::from_be_bytes([b[8], b[9], b[10], b[11]]) as u64
        + u32::from_be_bytes([b[12], b[13], b[14], b[15]]) as u64;
    addr_sum + l4_len as u64 + PROTO_TCP as u64
}

#[inline]
pub fn pseudo_sum(src: IpAddr, dst: IpAddr, l4_len: u32) -> u64 {
    match (src, dst) {
        (IpAddr::V4(s), IpAddr::V4(d)) => ipv4_pseudo_sum(s, d, l4_len),
        (IpAddr::V6(s), IpAddr::V6(d)) => ipv6_pseudo_sum(s, d, l4_len),
        _ => 0,
    }
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
    let l4_len = tcp_hlen + payload_len;
    let (ip_hlen, mut acc) = match (p.src, p.dst) {
        (IpAddr::V4(s), IpAddr::V4(d)) => {
            let total_len = (20 + l4_len) as u16;
            let addr_sum = ipv4_addr_sum(s, d);
            let c = checksum_fold(addr_sum + total_len as u64 + ((p.ttl as u64) << 8) + (0x4500 + 0x4000 + PROTO_TCP as u64));
            let h = &mut buf[..20];
            h[0] = 0x45;
            h[1] = 0;
            h[2..4].copy_from_slice(&total_len.to_be_bytes());
            h[4..8].copy_from_slice(&[0, 0, 0x40, 0]); // DF
            h[8] = p.ttl;
            h[9] = PROTO_TCP;
            h[10..12].copy_from_slice(&c.to_be_bytes());
            h[12..16].copy_from_slice(&s.octets());
            h[16..20].copy_from_slice(&d.octets());
            (20, addr_sum + l4_len as u32 as u64 + PROTO_TCP as u64)
        }
        (IpAddr::V6(s), IpAddr::V6(d)) => {
            let h = &mut buf[..40];
            h[0..4].copy_from_slice(&[0x60, 0, 0, 0]);
            h[4..6].copy_from_slice(&(l4_len as u16).to_be_bytes());
            h[6] = PROTO_TCP;
            h[7] = p.ttl;
            h[8..24].copy_from_slice(&s.octets());
            h[24..40].copy_from_slice(&d.octets());
            (40, ipv6_pseudo_sum(s, d, l4_len as u32))
        }
        _ => panic!("mixed address families"),
    };
    let doff_byte = ((tcp_hlen / 4) as u8) << 4;
    let doff_flags = ((doff_byte as u16) << 8) | (p.flags as u16);
    let t = &mut buf[ip_hlen..ip_hlen + tcp_hlen];
    t[0..2].copy_from_slice(&p.src_port.to_be_bytes());
    t[2..4].copy_from_slice(&p.dst_port.to_be_bytes());
    t[4..8].copy_from_slice(&p.seq.0.to_be_bytes());
    t[8..12].copy_from_slice(&p.ack.0.to_be_bytes());
    t[12] = doff_byte;
    t[13] = p.flags;
    t[14..16].copy_from_slice(&p.window.to_be_bytes());
    t[16..20].copy_from_slice(&[0, 0, 0, 0]);
    let mut tcp_sum = p.src_port as u64 + p.dst_port as u64 + p.seq.0 as u64 + p.ack.0 as u64 + doff_flags as u64 + p.window as u64;
    let mut i = 20;
    let o = p.opts;
    if let Some(m) = o.mss {
        t[i..i + 4].copy_from_slice(&[2, 4, (m >> 8) as u8, m as u8]);
        tcp_sum += 0x0204 + m as u64;
        i += 4;
    }
    if let Some(ws) = o.wscale {
        t[i..i + 4].copy_from_slice(&[1, 3, 3, ws]);
        tcp_sum += 0x0403 + ws as u64;
        i += 4;
    }
    match (o.sack_perm, o.ts) {
        (true, Some((v, e))) => {
            t[i..i + 4].copy_from_slice(&[4, 2, 8, 10]);
            t[i + 4..i + 8].copy_from_slice(&v.to_be_bytes());
            t[i + 8..i + 12].copy_from_slice(&e.to_be_bytes());
            tcp_sum += 0x0c0c + v as u64 + e as u64;
            i += 12;
        }
        (true, None) => {
            t[i..i + 4].copy_from_slice(&[1, 1, 4, 2]);
            tcp_sum += 0x0503;
            i += 4;
        }
        (false, Some((v, e))) => {
            t[i..i + 4].copy_from_slice(&[1, 1, 8, 10]);
            t[i + 4..i + 8].copy_from_slice(&v.to_be_bytes());
            t[i + 8..i + 12].copy_from_slice(&e.to_be_bytes());
            tcp_sum += 0x090b + v as u64 + e as u64;
            i += 12;
        }
        (false, None) => {}
    }
    if o.sack_n > 0 {
        let sack_len = 2 + 8 * o.sack_n;
        t[i..i + 4].copy_from_slice(&[1, 1, 5, sack_len]);
        tcp_sum += 0x0601 + sack_len as u64;
        i += 4;
        for &(l, r) in &o.sack[..o.sack_n as usize] {
            t[i..i + 4].copy_from_slice(&l.0.to_be_bytes());
            t[i + 4..i + 8].copy_from_slice(&r.0.to_be_bytes());
            tcp_sum += l.0 as u64 + r.0 as u64;
            i += 8;
        }
    }
    debug_assert_eq!(i, tcp_hlen);
    if p.csum_partial {
        // Not inverted: the device adds the segment and complements.
        let c = !checksum_fold(acc);
        t[16..18].copy_from_slice(&c.to_be_bytes());
        return ip_hlen + tcp_hlen;
    }
    acc += tcp_sum;
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

    fn scalar_reference_sum(mut acc: u64, data: &[u8]) -> u64 {
        for c in data.chunks(2) {
            acc += if c.len() == 2 { u16::from_be_bytes([c[0], c[1]]) as u64 } else { (c[0] as u64) << 8 };
            acc = (acc & 0xffff) + (acc >> 16);
        }
        acc
    }

    fn scalar_reference_fold(mut acc: u64) -> u16 {
        while acc >> 16 != 0 {
            acc = (acc & 0xffff) + (acc >> 16);
        }
        !(acc as u16)
    }

    #[test]
    fn checksum_fold_and_long_chunk_differential() {
        for &acc in &[
            0u64,
            1,
            0xfffe,
            0xffff,
            0x1_0000,
            0x1_fffe,
            0x1_ffff,
            0x2_0000,
            0x2_fffe,
            0xffff_fffe,
            0xffff_ffff,
            0x1_0000_0000,
            0x1_ffff_fffe,
            0x1_ffff_ffff,
            0x2_0000_0004,
            0xffff_ffff_0000_0000,
            0xffff_ffff_0000_0001,
            0xffff_ffff_ffff_fffe,
            u64::MAX,
        ] {
            assert_eq!(checksum_fold(acc), scalar_reference_fold(acc), "acc {acc:#x}");
        }

        let mut x = 0x243f_6a88u32;
        let random_buf: Vec<u8> = (0..600_032)
            .map(|_| {
                x ^= x << 13;
                x ^= x >> 17;
                x ^= x << 5;
                (x >> 3) as u8
            })
            .collect();
        let ff_buf = vec![0xffu8; 600_032];
        let zero_buf = vec![0u8; 600_032];

        for (label, buf) in [("random", &random_buf), ("ff", &ff_buf), ("zero", &zero_buf)] {
            for &len in &[
                0usize, 1, 2, 3, 4, 7, 8, 12, 15, 16, 19, 20, 28, 31, 32, 33, 48, 60, 63, 64, 65, 96, 127, 128, 129, 192, 255, 256, 257, 511, 512, 1459, 1460,
                4096, 65532, 65535, 262_140, 262_143, 262_144, 262_145, 262_148, 524_284, 524_288, 524_303, 600_000,
            ] {
                for off in [0usize, 1, 2, 3, 7, 15] {
                    let slice = &buf[off..off + len];
                    let expected = scalar_reference_fold(scalar_reference_sum(0, slice));
                    assert_eq!(checksum_fold(sum_bytes(0, slice)), expected, "{label} len={len} off={off}");
                    let words = len & !3;
                    let wslice = &slice[..words];
                    let expected_w = scalar_reference_fold(scalar_reference_sum(0, wslice));
                    assert_eq!(checksum_fold(sum_words_portable(wslice)), expected_w, "portable {label} words={words} off={off}");
                    assert_eq!(checksum_fold(sum_words(0, wslice)), expected_w, "dispatch {label} words={words} off={off}");
                }
            }
        }
    }

    fn reference_pseudo_sum(src: IpAddr, dst: IpAddr, l4_len: u32) -> u64 {
        let mut s = 0u64;
        match (src, dst) {
            (IpAddr::V4(a), IpAddr::V4(b)) => {
                s = scalar_reference_sum(s, &a.octets());
                s = scalar_reference_sum(s, &b.octets());
            }
            (IpAddr::V6(a), IpAddr::V6(b)) => {
                s = scalar_reference_sum(s, &a.octets());
                s = scalar_reference_sum(s, &b.octets());
            }
            _ => unreachable!(),
        }
        s = scalar_reference_sum(s, &[0, PROTO_TCP]);
        scalar_reference_sum(s, &l4_len.to_be_bytes())
    }

    fn reference_emit(buf: &mut [u8; MAX_HEADER], p: &EmitParams) -> usize {
        let payload_len: usize = p.payload.iter().map(|s| s.len()).sum();
        let olen = options_len(p.opts);
        let tcp_hlen = 20 + olen;
        let l4_len = tcp_hlen + payload_len;
        let ip_hlen = match (p.src, p.dst) {
            (IpAddr::V4(s), IpAddr::V4(d)) => {
                let h = &mut buf[..20];
                h[0] = 0x45;
                h[1] = 0;
                h[2..4].copy_from_slice(&((20 + l4_len) as u16).to_be_bytes());
                h[4..8].copy_from_slice(&[0, 0, 0x40, 0]);
                h[8] = p.ttl;
                h[9] = PROTO_TCP;
                h[10..12].copy_from_slice(&[0, 0]);
                h[12..16].copy_from_slice(&s.octets());
                h[16..20].copy_from_slice(&d.octets());
                let c = scalar_reference_fold(scalar_reference_sum(0, h));
                h[10..12].copy_from_slice(&c.to_be_bytes());
                20
            }
            (IpAddr::V6(s), IpAddr::V6(d)) => {
                let h = &mut buf[..40];
                h[0..4].copy_from_slice(&[0x60, 0, 0, 0]);
                h[4..6].copy_from_slice(&(l4_len as u16).to_be_bytes());
                h[6] = PROTO_TCP;
                h[7] = p.ttl;
                h[8..24].copy_from_slice(&s.octets());
                h[24..40].copy_from_slice(&d.octets());
                40
            }
            _ => unreachable!(),
        };
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
                t[i..i + 4].copy_from_slice(&[4, 2, 8, 10]);
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
            t[i] = 0;
            i += 1;
        }
        let acc = reference_pseudo_sum(p.src, p.dst, l4_len as u32);
        if p.csum_partial {
            let c = !scalar_reference_fold(acc);
            t[16..18].copy_from_slice(&c.to_be_bytes());
            return ip_hlen + tcp_hlen;
        }
        let mut flat = t.to_vec();
        for s in p.payload {
            flat.extend_from_slice(s);
        }
        let c = scalar_reference_fold(scalar_reference_sum(acc, &flat));
        t[16..18].copy_from_slice(&c.to_be_bytes());
        ip_hlen + tcp_hlen
    }

    #[test]
    fn emit_and_parse_differential_matrix() {
        let mut x = 0x1319_8a2eu32;
        let data: Vec<u8> = (0..65_475)
            .map(|_| {
                x ^= x << 13;
                x ^= x >> 17;
                x ^= x << 5;
                x as u8
            })
            .collect();

        // IPv4 header with options (IHL = 20..=60) and single-bit corruption detection.
        for ihl in (20..=60).step_by(4) {
            let total = ihl + 20;
            let mut pkt = vec![0u8; total];
            pkt[0] = 0x40 | ((ihl / 4) as u8);
            pkt[2..4].copy_from_slice(&(total as u16).to_be_bytes());
            pkt[6..8].copy_from_slice(&0x4000u16.to_be_bytes());
            pkt[8] = 64;
            pkt[9] = PROTO_TCP;
            pkt[12..16].copy_from_slice(&[192, 0, 2, 1]);
            pkt[16..20].copy_from_slice(&[198, 51, 100, 2]);
            for (idx, b) in pkt[20..ihl].iter_mut().enumerate() {
                *b = (idx as u8).wrapping_mul(29).wrapping_add(1);
            }
            let csum = scalar_reference_fold(scalar_reference_sum(0, &pkt[..ihl]));
            pkt[10..12].copy_from_slice(&csum.to_be_bytes());
            pkt[ihl + 12] = 5 << 4;
            let ip = parse_ip(&pkt).unwrap();
            assert_eq!(ip.l4_off, ihl);
            assert_eq!(ip.l4_len, 20);
            for byte_idx in 0..ihl {
                pkt[byte_idx] ^= 0x01;
                if byte_idx <= 3 {
                    assert!(matches!(parse_ip(&pkt), Err(DropReason::Truncated | DropReason::BadIpChecksum)));
                } else {
                    assert_eq!(parse_ip(&pkt).unwrap_err(), DropReason::BadIpChecksum);
                }
                pkt[byte_idx] ^= 0x01;
            }
        }

        // Exhaustive 80-combination TCP options padding invariant & sentinel-leakage test.
        let v4_src: IpAddr = "10.20.30.40".parse().unwrap();
        let v4_dst: IpAddr = "172.16.0.99".parse().unwrap();
        for mss in [None, Some(1460)] {
            for wscale in [None, Some(7)] {
                for sack_perm in [false, true] {
                    for ts in [None, Some((0x1122_3344, 0x5566_7788))] {
                        for sack_n in 0..=(MAX_SACK_BLOCKS as u8) {
                            let opts = EmitOptions {
                                mss,
                                wscale,
                                sack_perm,
                                ts,
                                sack: [(Seq(10), Seq(20)), (Seq(30), Seq(40)), (Seq(50), Seq(60)), (Seq(70), Seq(80))],
                                sack_n,
                            };
                            if tcp_header_len(&opts) > 60 {
                                continue;
                            }
                            let mut cand_buf = [0xa5u8; MAX_HEADER];
                            let mut ref_buf = [0x5au8; MAX_HEADER];
                            let p = EmitParams {
                                src: v4_src,
                                dst: v4_dst,
                                src_port: 1234,
                                dst_port: 5678,
                                seq: Seq(0x1020_3040),
                                ack: Seq(0x5060_7080),
                                flags: SYN | ACK,
                                window: 8192,
                                opts: &opts,
                                payload: &[&data[..37]],
                                ttl: 64,
                                csum_partial: false,
                            };
                            let hl_c = emit(&mut cand_buf, &p);
                            let hl_r = reference_emit(&mut ref_buf, &p);
                            assert_eq!(hl_c, hl_r);
                            assert_eq!(&cand_buf[..hl_c], &ref_buf[..hl_r]);
                        }
                    }
                }
            }
        }

        let option_cases = [
            EmitOptions::default(),
            EmitOptions { mss: Some(1460), ..Default::default() },
            EmitOptions { wscale: Some(8), ..Default::default() },
            EmitOptions { sack_perm: true, ..Default::default() },
            EmitOptions { ts: Some((0x1122_3344, 0x5566_7788)), ..Default::default() },
            EmitOptions { sack_perm: true, ts: Some((0xdead_beef, 0x0102_0304)), ..Default::default() },
            EmitOptions { mss: Some(1200), wscale: Some(14), sack_perm: true, ts: Some((1, 2)), ..Default::default() },
            EmitOptions {
                ts: Some((1, 2)),
                sack: [(Seq(10), Seq(20)), (Seq(30), Seq(40)), (Seq(50), Seq(60)), (Seq(0), Seq(0))],
                sack_n: 3,
                ..Default::default()
            },
            EmitOptions { sack: [(Seq(1), Seq(2)), (Seq(3), Seq(4)), (Seq(5), Seq(6)), (Seq(7), Seq(8))], sack_n: 4, ..Default::default() },
        ];

        let split_cases: &[&[usize]] = &[&[], &[1], &[64], &[1, 1], &[1, 2, 3], &[33, 1, 99, 2], &[701, 759], &[32_769, 32_666]];

        for (src, dst) in [
            ("10.20.30.40".parse::<IpAddr>().unwrap(), "172.16.0.99".parse::<IpAddr>().unwrap()),
            ("2001:db8::1".parse::<IpAddr>().unwrap(), "2001:db8::2".parse::<IpAddr>().unwrap()),
        ] {
            for opts in &option_cases {
                for &splits in split_cases {
                    let mut slices: Vec<&[u8]> = Vec::with_capacity(splits.len());
                    let mut pos = 0;
                    for &len in splits {
                        slices.push(&data[pos..pos + len]);
                        pos += len;
                    }
                    let mut hdr_full = [0xa5u8; MAX_HEADER];
                    let mut hdr_part = [0xa5u8; MAX_HEADER];
                    let mut ref_full = [0x5au8; MAX_HEADER];
                    let mut ref_part = [0x5au8; MAX_HEADER];
                    let params = |csum_partial| EmitParams {
                        src,
                        dst,
                        src_port: 0x8001,
                        dst_port: 0x01bb,
                        seq: Seq(0xfedc_ba98),
                        ack: Seq(0x0123_4567),
                        flags: ACK | PSH,
                        window: 0xfffe,
                        opts,
                        payload: &slices,
                        ttl: 57,
                        csum_partial,
                    };
                    let hl_full = emit(&mut hdr_full, &params(false));
                    let hl_part = emit(&mut hdr_part, &params(true));
                    let ref_hl_full = reference_emit(&mut ref_full, &params(false));
                    let ref_hl_part = reference_emit(&mut ref_part, &params(true));

                    assert_eq!(hl_full, ref_hl_full);
                    assert_eq!(hl_part, ref_hl_part);
                    assert_eq!(&hdr_full[..hl_full], &ref_full[..ref_hl_full]);
                    assert_eq!(&hdr_part[..hl_part], &ref_part[..ref_hl_part]);

                    let ip_hlen = ip_header_len(src);
                    let mut pkt_full = hdr_full[..hl_full].to_vec();
                    let mut pkt_part = hdr_part[..hl_part].to_vec();
                    for s in &slices {
                        pkt_full.extend_from_slice(s);
                        pkt_part.extend_from_slice(s);
                    }
                    // Verify hardware/kernel CHECKSUM_PARTIAL completion yields the exact full packet.
                    let dev_csum = scalar_reference_fold(scalar_reference_sum(0, &pkt_part[ip_hlen..]));
                    pkt_part[ip_hlen + 16..ip_hlen + 18].copy_from_slice(&dev_csum.to_be_bytes());
                    assert_eq!(pkt_part, pkt_full);

                    if src.is_ipv4() {
                        assert_eq!(scalar_reference_fold(scalar_reference_sum(0, &pkt_full[..20])), 0);
                    }
                    let ip = parse_ip(&pkt_full).unwrap();
                    let tcp = parse_tcp(&pkt_full, &ip).unwrap();
                    assert_eq!(tcp.src_port, 0x8001);
                    assert_eq!(tcp.dst_port, 0x01bb);
                    assert_eq!(tcp.seq, Seq(0xfedc_ba98));
                    assert_eq!(tcp.ack, Seq(0x0123_4567));
                    assert_eq!(tcp.window, 0xfffe);
                    assert_eq!(&pkt_full[ip.l4_off + tcp.data_off..], &data[..pos]);
                }
            }
        }
    }
}
