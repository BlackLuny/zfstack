#!/usr/bin/env python3
"""Functional WG/TUN acceptance for #100; no throughput/performance claim.

The outer process holds a physical-domain lease. All network commands and test
processes run under qa/isolate-linux.sh, including its private /run registry.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
import traceback

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from qa.lease import Lease


def save(path, value):
    path = Path(path)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")
    tmp.replace(path)


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def command(args, *, data=None, timeout=15):
    result = subprocess.run(args, input=data, text=True, capture_output=True, timeout=timeout)
    if result.returncode:
        raise RuntimeError(f"command failed {args[:5]}: {result.stderr[:1500]}")
    return result.stdout


def statuses(output):
    return [json.loads((output / f"{s}.status.json").read_text()) for s in "ab"]


def wait_for(predicate, children, timeout):
    until = time.monotonic() + timeout
    while time.monotonic() < until:
        if any(p.poll() is not None for p in children):
            raise RuntimeError("endpoint exited before its control barrier")
        try:
            if predicate():
                return
        except FileNotFoundError:
            pass
        time.sleep(0.01)
    raise TimeoutError("control barrier timeout")


def net(side, *args):
    return command(["ip", "netns", "exec", f"i100{side}", *args])


def observe():
    return {side: {"wg_transfer": net(side, "wg", "show", "i100wg", "transfer"),
                   "wg_handshakes": net(side, "wg", "show", "i100wg", "latest-handshakes"),
                   "links": json.loads(net(side, "ip", "-j", "-s", "link", "show")),
                   "routes": json.loads(net(side, "ip", "-j", "route", "show"))}
            for side in "ab"}


def setup():
    for side in "ab":
        command(["ip", "netns", "add", f"i100{side}"])
    command(["ip", "link", "add", "i100va", "type", "veth", "peer", "name", "i100vb"])
    for i, side in enumerate("ab", 1):
        command(["ip", "link", "set", f"i100v{side}", "netns", f"i100{side}"])
        net(side, "ip", "link", "set", "lo", "up")
        net(side, "ip", "addr", "add", f"192.0.2.{i}/30", "dev", f"i100v{side}")
        net(side, "ip", "link", "set", f"i100v{side}", "mtu", "1500", "up")
        net(side, "sysctl", "-qw", "net.ipv4.ip_forward=1", "net.ipv4.conf.all.rp_filter=0", "net.ipv4.conf.default.rp_filter=0")
        # Ephemeral keys stay in the private /run, never in evidence or stdout.
        key = command(["wg", "genkey"])
        path = Path(f"/run/i100-{side}.key")
        path.write_text(key)
        path.chmod(0o600)
        Path(f"/run/i100-{side}.pub").write_text(command(["wg", "pubkey"], data=key).strip())
        net(side, "ip", "link", "add", "i100wg", "type", "wireguard")
        net(side, "wg", "set", "i100wg", "private-key", str(path), "listen-port", "51820")
        net(side, "ip", "link", "set", "i100wg", "mtu", "1420", "up")
    for i, side in enumerate("ab", 1):
        peer = "b" if side == "a" else "a"
        other = 3 - i
        public = Path(f"/run/i100-{peer}.pub").read_text()
        net(side, "wg", "set", "i100wg", "peer", public, "allowed-ips", f"10.100.0.{other}/32", "endpoint", f"192.0.2.{other}:51820")
        net(side, "ip", "route", "add", f"10.100.0.{other}/32", "dev", "i100wg")
        Path(f"/run/i100-{side}.key").unlink()


def impair():
    for side in "ab":
        # All application writes end in PSH. Suppress pure ACKs/SACKs to force
        # RTO, and drop the full-sized first data segment in the kernel path.
        rules = ('table inet i100 { chain forward { type filter hook forward priority 0; policy accept;\n'
                 ' iifname "i100t0" ip length > 400 counter drop\n'
                 ' iifname "i100t0" tcp flags & (syn | fin | rst | psh | ack) == ack counter drop\n'
                 '} }\n')
        command(["ip", "netns", "exec", f"i100{side}", "nft", "-f", "-"], data=rules)


def case(binary, output, fault, timestamps):
    output.mkdir()
    children, handles, captures = [], [], []
    result = {"fault": fault, "timestamps": timestamps, "binary_sha256": sha(binary), "functional": "NOT_RUN"}
    started = time.monotonic()
    try:
        setup()
        for side in "ab":
            log = (output / f"{side}.log").open("w")
            handles.append(log)
            args = ["ip", "netns", "exec", f"i100{side}", str(binary), "--side", side,
                    "--control", str(output), "--timestamps", str(timestamps).lower()]
            if fault:
                args.append("--pressure")
            children.append(subprocess.Popen(args, stdout=log, stderr=subprocess.STDOUT))
        def interfaces_ready():
            return all(subprocess.run(["ip", "-n", f"i100{s}", "link", "show", "i100t0"],
                                      stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=2).returncode == 0 for s in "ab")
        wait_for(interfaces_ready, children, 3)
        for i, side in enumerate("ab", 1):
            net(side, "ip", "link", "set", "i100t0", "mtu", "1420", "txqueuelen", "4096", "up")
            net(side, "ip", "route", "add", f"10.100.0.{i}/32", "dev", "i100t0")
            caplog = (output / f"{side}.tcpdump.log").open("w")
            handles.append(caplog)
            captures.append(subprocess.Popen(["ip", "netns", "exec", f"i100{side}", "tcpdump", "-U", "-n", "-i", "any",
                                              "-w", str(output / f"{side}.pcap"), "tcp or udp port 51820"], stdout=caplog, stderr=subprocess.STDOUT))
        (output / "network-ready").touch()
        wait_for(lambda: all(s["phase"] == "connected" for s in statuses(output)), children, 4)
        result["connected"] = observe()
        if fault:
            impair()
        (output / "start").touch()
        wait_for(lambda: all(s["phase"] == "filled" and (not fault or s["info"]["ooo_bytes"] == 7)
                             for s in statuses(output)), children, 2)
        result["before_pressure"] = statuses(output)
        (output / "pressure").touch()
        if fault:
            wait_for(lambda: all(s["phase"] == "blocked" and s["record_waiters"] > 0 and s["send_record_failures"] > 0
                                 and s["info"]["ooo_bytes"] == 7 and s["info"]["rto_count"] >= 1
                                 for s in statuses(output)), children, 3)
            result["both_blocked_after_rto"] = statuses(output)
            result["fault_counters"] = {s: json.loads(net(s, "nft", "-j", "list", "table", "inet", "i100")) for s in "ab"}
            for side in "ab":
                net(side, "nft", "delete", "table", "inet", "i100")
        (output / "released").touch()
        result["fault_released_ms"] = (time.monotonic() - started) * 1000
        try:
            wait_for(lambda: all(s["phase"] == "recovered" for s in statuses(output)), children, 7)
            result["recovered"] = statuses(output)
            if fault:
                assert all(s["held_bytes"] > 0 and s["info"]["rto_count"] >= 1 for s in result["recovered"])
            (output / "resume").touch()
            wait_for(lambda: all(s["phase"] == "complete" for s in statuses(output)), children, 2)
            result["functional"] = "PASS"
        except TimeoutError:
            result["functional"] = "STALL"
            result["stalled"] = statuses(output)
        result["path_after"] = observe()
        (output / "finish").touch()
        for process in children:
            process.wait(timeout=2)
        result["exit_codes"] = [p.returncode for p in children]
        result["endpoints"] = [json.loads((output / f"{s}.result.json").read_text()) for s in "ab"]
        for endpoint in result["endpoints"]:
            assert not endpoint["sink_errors"]
            assert endpoint["max_packet_bytes"] <= 1420
            assert endpoint["reserved_after_drop"] == 0
            assert endpoint["tun_received"] > 0
        for side in "ab":
            fields = result["path_after"][side]["wg_transfer"].strip().split()
            assert len(fields) == 3 and int(fields[1]) > 0 and int(fields[2]) > 0
        if result["functional"] == "PASS":
            assert result["exit_codes"] == [0, 0]
        else:
            assert fault and all(s["info"]["ooo_bytes"] == 7 and s["record_waiters"] > 0 and s["info"]["rto_count"] >= 2
                                 for s in result["stalled"])
    except BaseException as error:
        result["functional"] = "ERROR"
        result["error"] = f"{type(error).__name__}: {error}"
        result["traceback"] = traceback.format_exc()
    finally:
        for process in captures:
            if process.poll() is None:
                process.send_signal(signal.SIGINT)
        for process in children + captures:
            if process.poll() is None:
                process.terminate()
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=2)
        for handle in handles:
            handle.close()
        for side in "ab":
            subprocess.run(["ip", "netns", "del", f"i100{side}"], capture_output=True, timeout=5)
        result["cleanup"] = {"children_reaped": all(p.poll() is not None for p in children + captures),
                             "namespaces": command(["ip", "netns", "list"]).strip()}
        assert result["cleanup"] == {"children_reaped": True, "namespaces": ""}
        result["elapsed_secs"] = time.monotonic() - started
        save(output / "result.json", result)
    print(json.dumps({"case":output.name, "functional":result["functional"], "error":result.get("error")}), flush=True)
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--fixed-root", type=Path, required=True)
    parser.add_argument("--base-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--physical-domain", required=True)
    parser.add_argument("--generation", required=True)
    parser.add_argument("--lease-dir", type=Path, required=True)
    parser.add_argument("--rounds", type=int, default=3)
    parser.add_argument("--inside", action="store_true")
    args = parser.parse_args()
    args.output = args.output.resolve()
    args.fixed_root, args.base_root = args.fixed_root.resolve(), args.base_root.resolve()
    if args.inside:
        assert os.environ.get("QA_NETNS_ISOLATED") == "1" and os.geteuid() == 0
        bins = {name: args.output / f"target-{name}/release/examples/issue100_wg_duplex" for name in ("fixed", "base")}
        results = [case(bins["fixed"], args.output / "healthy", False, True)]
        for repeat in range(args.rounds):
            timestamps = repeat % 2 == 0
            for arm in ("base", "fixed"):
                results.append(case(bins[arm], args.output / f"{repeat + 1:02d}-{arm}", True, timestamps))
        expected = ["PASS"] + [item for _ in range(args.rounds) for item in ("STALL", "PASS")]
        passed = [r["functional"] for r in results] == expected
        save(args.output / "summary.json", {"pass":passed,"expected":expected,"observed":[r["functional"] for r in results]})
        return 0 if passed else 1
    assert 1 <= args.rounds <= 3
    with Lease(args.lease_dir.resolve(), args.physical_domain, "issue100-wg-" + args.output.name, args.generation) as lease:
        args.output.mkdir(exist_ok=False)
        protocol = {"kind":"functional-only","mtu":1420,"rounds":args.rounds,"order":"healthy, then base/fixed per round",
                    "physical_domain":args.physical_domain,"generation":args.generation,"runner_sha256":sha(__file__),
                    "rule":"Both endpoints full, allocation denied and OOO=7 at RTO before releasing network faults; budget blockers held until full ACK and read.",
                    "recovery_timeout_secs":7,"maximum_case_secs":15,"expected_negative":"STALL, never accepted as a positive pass"}
        save(args.output / "protocol.json", protocol)
        for arm, root in (("base", args.base_root), ("fixed", args.fixed_root)):
            lease.check()
            env = os.environ.copy()
            env["CARGO_TARGET_DIR"] = str(args.output / f"target-{arm}")
            env["CARGO_BUILD_JOBS"] = "2"
            with (args.output / f"build-{arm}.log").open("w") as log:
                command_args = ["cargo", "+1.94.1", "build", "--locked", "--release", "-p", "zfbench", "--example", "issue100_wg_duplex", "--features", "zfstack/test-peer"]
                subprocess.run(command_args, cwd=root, env=env, stdout=log, stderr=subprocess.STDOUT, check=True, timeout=600)
            binary = args.output / f"target-{arm}/release/examples/issue100_wg_duplex"
            protocol[f"{arm}_binary_sha256"] = sha(binary)
            save(args.output / "protocol.json", protocol)
        lease.check()
        cmd = ["bash", str(ROOT / "qa/isolate-linux.sh"), sys.executable, str(Path(__file__).resolve()), *sys.argv[1:], "--inside"]
        child = subprocess.Popen(cmd, start_new_session=True)
        deadline = time.monotonic() + 150
        try:
            while child.poll() is None:
                lease.check()
                if time.monotonic() > deadline:
                    raise TimeoutError("suite deadline")
                time.sleep(0.2)
        finally:
            if child.poll() is None:
                os.killpg(child.pid, signal.SIGTERM)
                child.wait(timeout=5)
        lease.check()
        return child.returncode


if __name__ == "__main__":
    sys.exit(main())
