//! Device offload helpers for hosts that feed the stack from a Linux TUN
//! opened with `IFF_VNET_HDR` (or any virtio-net style device): every packet
//! read or written is preceded by a 10-byte `struct virtio_net_hdr`.
//!
//! With `TUNSETOFFLOAD(TUN_F_CSUM)` the local kernel stops computing TCP
//! checksums for packets it routes into the TUN (they arrive with
//! `VIRTIO_NET_HDR_F_NEEDS_CSUM`) and accepts packets whose checksum we leave
//! partial, so neither side sums payload bytes on this hop: pass
//! [`VirtioNetHdr::rx_checksum`] to `Shard::ingress_with`, enable
//! `Shard::set_iface_tx_checksum_offload`, and prefix each `OutPacket` with
//! [`VirtioNetHdr::for_packet`]. Adding `TUN_F_TSO4 | TUN_F_TSO6` also lets the
//! kernel hand over whole TCP super-segments (up to 64 KiB) even with a small
//! MTU; the stack ingests them as single segments.
//!
//! Field byte order is the host's (the TUN default unless `TUNSETVNETLE/BE`).

use crate::shard::{OutPacket, RxChecksum};

pub const VIRTIO_NET_HDR_LEN: usize = 10;

pub const VIRTIO_NET_HDR_F_NEEDS_CSUM: u8 = 1;
pub const VIRTIO_NET_HDR_F_DATA_VALID: u8 = 2;

pub const VIRTIO_NET_HDR_GSO_NONE: u8 = 0;
pub const VIRTIO_NET_HDR_GSO_TCPV4: u8 = 1;
pub const VIRTIO_NET_HDR_GSO_UDP: u8 = 3;
pub const VIRTIO_NET_HDR_GSO_TCPV6: u8 = 4;
pub const VIRTIO_NET_HDR_GSO_ECN: u8 = 0x80;

/// TCP checksum field offset inside the TCP header.
const TCP_CSUM_OFFSET: u16 = 16;

#[derive(Copy, Clone, Debug, Default, PartialEq, Eq)]
pub struct VirtioNetHdr {
    pub flags: u8,
    pub gso_type: u8,
    pub hdr_len: u16,
    pub gso_size: u16,
    pub csum_start: u16,
    pub csum_offset: u16,
}

impl VirtioNetHdr {
    /// Parse the header at the front of a packet read from the device.
    pub fn decode(b: &[u8]) -> Option<Self> {
        if b.len() < VIRTIO_NET_HDR_LEN {
            return None;
        }
        let u = |i: usize| u16::from_ne_bytes([b[i], b[i + 1]]);
        Some(VirtioNetHdr { flags: b[0], gso_type: b[1], hdr_len: u(2), gso_size: u(4), csum_start: u(6), csum_offset: u(8) })
    }

    pub fn encode(&self) -> [u8; VIRTIO_NET_HDR_LEN] {
        let mut o = [0u8; VIRTIO_NET_HDR_LEN];
        o[0] = self.flags;
        o[1] = self.gso_type;
        o[2..4].copy_from_slice(&self.hdr_len.to_ne_bytes());
        o[4..6].copy_from_slice(&self.gso_size.to_ne_bytes());
        o[6..8].copy_from_slice(&self.csum_start.to_ne_bytes());
        o[8..10].copy_from_slice(&self.csum_offset.to_ne_bytes());
        o
    }

    /// The checksum verdict to pass to `Shard::ingress_with`. A partial
    /// (`NEEDS_CSUM`) checksum is only a pseudo-header sum and would fail
    /// verification; `DATA_VALID` was already checked by the device.
    pub fn rx_checksum(&self) -> RxChecksum {
        if self.flags & (VIRTIO_NET_HDR_F_NEEDS_CSUM | VIRTIO_NET_HDR_F_DATA_VALID) != 0 {
            RxChecksum::Trusted
        } else {
            RxChecksum::Verify
        }
    }

    /// The header to write in front of `pkt`: `NEEDS_CSUM` over the TCP
    /// segment when the stack left its checksum partial, plus the GSO type
    /// and size for a TSO super-segment; all zero otherwise.
    pub fn for_packet(pkt: &OutPacket<'_>) -> Self {
        if !pkt.csum_partial {
            return VirtioNetHdr::default();
        }
        let v4 = pkt.header.first().map(|b| b >> 4) == Some(4);
        // The stack emits IPv6 without extension headers.
        let ip_len = if v4 { ((pkt.header[0] & 0x0f) as u16) * 4 } else { 40 };
        let mut h = VirtioNetHdr { flags: VIRTIO_NET_HDR_F_NEEDS_CSUM, csum_start: ip_len, csum_offset: TCP_CSUM_OFFSET, ..Default::default() };
        if pkt.gso_size > 0 {
            h.gso_type = if v4 { VIRTIO_NET_HDR_GSO_TCPV4 } else { VIRTIO_NET_HDR_GSO_TCPV6 };
            h.gso_size = pkt.gso_size;
            h.hdr_len = pkt.header.len() as u16;
        }
        h
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::PeerId;

    #[test]
    fn roundtrip_and_partial_header() {
        let h = VirtioNetHdr { flags: 1, gso_type: VIRTIO_NET_HDR_GSO_TCPV4, hdr_len: 52, gso_size: 1448, csum_start: 20, csum_offset: 16 };
        assert_eq!(VirtioNetHdr::decode(&h.encode()), Some(h));
        assert_eq!(h.rx_checksum(), RxChecksum::Trusted);
        assert_eq!(VirtioNetHdr::default().rx_checksum(), RxChecksum::Verify);
        assert_eq!(VirtioNetHdr { flags: VIRTIO_NET_HDR_F_DATA_VALID, ..Default::default() }.rx_checksum(), RxChecksum::Trusted);
        let v4 = [0x45u8; 40];
        let p = OutPacket { peer: PeerId(0), header: &v4, payload: [&[], &[]], csum_partial: true, gso_size: 0 };
        assert_eq!(VirtioNetHdr::for_packet(&p), VirtioNetHdr { flags: 1, csum_start: 20, csum_offset: 16, ..Default::default() });
        let v6 = [0x60u8; 60];
        let p = OutPacket { peer: PeerId(0), header: &v6, payload: [&[], &[]], csum_partial: true, gso_size: 1440 };
        let h = VirtioNetHdr::for_packet(&p);
        assert_eq!((h.csum_start, h.gso_type, h.gso_size, h.hdr_len), (40, VIRTIO_NET_HDR_GSO_TCPV6, 1440, 60));
        let p = OutPacket { peer: PeerId(0), header: &v6, payload: [&[], &[]], csum_partial: false, gso_size: 0 };
        assert_eq!(VirtioNetHdr::for_packet(&p), VirtioNetHdr::default());
    }
}
