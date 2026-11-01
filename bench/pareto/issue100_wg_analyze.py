#!/usr/bin/env python3
"""Independently check #100 endpoint, kernel path and pcap evidence."""
import argparse
import hashlib
import json
from pathlib import Path
import re
import struct


def read(path):
    path = Path(path)
    assert path.stat().st_size <= 16 << 20, path
    return json.loads(path.read_text())


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def pcap(path):
    raw = Path(path).read_bytes()
    assert raw[:4] in (b"\xd4\xc3\xb2\xa1", b"\xa1\xb2\xc3\xd4"), "expected ordinary pcap"
    endian = "<" if raw[:4] == b"\xd4\xc3\xb2\xa1" else ">"
    linktype = struct.unpack_from(endian + "I", raw, 20)[0]
    offset = {276: 20, 113: 16, 1: 14}[linktype]
    pos, tcp, wg, handshakes, max_inner, max_outer = 24, 0, 0, 0, 0, 0
    while pos < len(raw):
        _, _, n, original = struct.unpack_from(endian + "IIII", raw, pos)
        assert n == original, "truncated capture"
        pkt = raw[pos + 16:pos + 16 + n]
        assert len(pkt) == n
        pos += 16 + n
        ip = pkt[offset:]
        if len(ip) < 20 or ip[0] >> 4 != 4:
            continue
        header = (ip[0] & 15) * 4
        total = int.from_bytes(ip[2:4], "big")
        assert 20 <= header <= total <= len(ip)
        if ip[9] == 6:
            tcp += 1
            max_inner = max(max_inner, total)
            assert total <= 1420, "oversized TCP packet in real kernel capture"
        elif ip[9] == 17:
            udp = ip[header:total]
            if 51820 in struct.unpack_from("!HH", udp):
                message = int.from_bytes(udp[8:12], "little")
                wg += message == 4
                handshakes += message in (1, 2)
                max_outer = max(max_outer, total)
                assert total <= 1500, "WG packet exceeds underlay MTU"
    assert tcp > 0 and wg > 0 and handshakes > 0, "missing actual TCP/WG path"
    return {"tcp_packets":tcp,"wg_data_packets":wg,"wg_handshake_packets":handshakes,
            "max_tcp_ip_bytes":max_inner,"max_wg_ip_bytes":max_outer,"sha256":digest(path)}


def check_case(path, expected, binary_hash):
    data = read(path / "result.json")
    assert data["functional"] == expected, (path, data.get("error"))
    assert data["binary_sha256"] == binary_hash
    assert data["cleanup"] == {"children_reaped":True,"namespaces":""}
    rows = []
    for i, side in enumerate("ab"):
        end = data["endpoints"][i]
        assert end == read(path / f"{side}.result.json")
        assert end["runs"] > 0 and end["tun_received"] > 0
        assert end["reserved_after_drop"] == 0 and end["sink_errors"] == []
        assert end["max_packet_bytes"] <= 1420
        assert max(e["bytes"] for e in end["sink_events"]) == end["max_packet_bytes"]
        path_data = data["path_after"][side]
        _, rx, tx = path_data["wg_transfer"].split()
        assert int(rx) > 0 and int(tx) > 0
        assert int(path_data["wg_handshakes"].split()[1]) > 0
        assert any(r.get("dev") == "i100wg" for r in path_data["routes"])
        assert any(r.get("dev") == "i100t0" for r in path_data["routes"])
        row = {"side":side,"rto_count":end["info"]["rto_count"],"record_failures":end["send_record_failures"],
               "received":end["received"],"expected":end["expected"],"wg_rx":int(rx),"wg_tx":int(tx),
               "capture":pcap(path / f"{side}.pcap")}
        capture_log = (path / f"{side}.tcpdump.log").read_text()
        row["capture"]["kernel_capture_drops"] = int(re.search(r"(\d+) packets dropped by kernel", capture_log).group(1))
        if data["fault"]:
            blocked = data["both_blocked_after_rto"][i]
            assert blocked["record_waiters"] == 1 and blocked["send_record_failures"] > 0
            assert blocked["info"]["ooo_bytes"] == 7 and blocked["info"]["rto_count"] >= 1
            assert blocked["held_bytes"] > 0 and blocked["reserved"] <= blocked["high"]
            counters = []
            for item in data["fault_counters"][side]["nftables"]:
                if "rule" in item:
                    counters.extend(e["counter"]["packets"] for e in item["rule"]["expr"] if "counter" in e)
            # nft is first-match: a piggyback ACK on a full data packet is
            # already dropped by rule 1, so the pure-ACK counter can be zero.
            # Actual data loss is mandatory, and both real RTOs are proven above.
            assert len(counters) == 2 and counters[0] > 0, "kernel did not drop the full-sized head"
            row["kernel_drop_counters"] = counters
        if expected == "PASS":
            assert end["phase"] == "complete" and end["received"] == end["expected"]
            assert end["info"]["tx_queued"] == end["info"]["ooo_bytes"] == end["info"]["pipe"] == 0
            recovered = end["recovered_while_blocked"]
            assert recovered["received"] == recovered["expected"] == end["expected"] - 256
            assert recovered["info"]["tx_queued"] == recovered["info"]["pipe"] == 0
            if data["fault"]:
                assert recovered["held_bytes"] == blocked["held_bytes"], "budget was released before repair"
                assert recovered["info"]["rto_count"] >= 1
                repairs = [e for e in end["sink_events"] if e["retransmit"] and e["payload"] == end["info"]["mss"]]
                assert repairs and all(e["sack_blocks"] == 0 and e["bytes"] <= 1420 for e in repairs)
                row["full_mss_repairs_without_sack"] = len(repairs)
        else:
            assert end["phase"] == "blocked" and end["received"] == 0 and end["info"]["ooo_bytes"] == 7
            assert end["record_waiters"] == 1 and end["info"]["rto_count"] >= 2 and end["info"]["bytes_retrans"] == 0
        rows.append(row)
    return {"case":path.name,"expected":expected,"observed":data["functional"],"timestamps":data["timestamps"],"endpoints":rows}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("suite", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    protocol = read(args.suite / "protocol.json")
    cases = [check_case(args.suite / "healthy", "PASS", protocol["fixed_binary_sha256"])]
    for index in range(1, protocol["rounds"] + 1):
        for arm, expected in (("base", "STALL"), ("fixed", "PASS")):
            cases.append(check_case(args.suite / f"{index:02d}-{arm}", expected, protocol[f"{arm}_binary_sha256"]))
    report = {"verified":True,"scope":"functional, real Linux Shards/TUN/kernel WG over isolated veth; not WAN or zfc release qualification",
              "positive_cases":sum(c["observed"] == "PASS" for c in cases),"expected_stall_controls":sum(c["observed"] == "STALL" for c in cases),
              "emsgsize":0,"cases":cases,"protocol_sha256":digest(args.suite / "protocol.json"),
              "analyzer_sha256":digest(__file__),
              "capture_note":"Capture drops are retained per endpoint. Full TUN writes and application bytes are independently checked by endpoint records."}
    args.output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    print(json.dumps({k:v for k,v in report.items() if k != "cases"}))


if __name__ == "__main__":
    main()
