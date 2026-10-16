"""Read L2 evidence without repairing missing observations or conflating scopes."""
import gzip
import hashlib
import io
import json
import math
from pathlib import Path

from netem import evidence_errors as qdisc_errors
from path_contract import inspect_path
from fifo_ledger import KIND as FIFO_KIND
from queue_control import DRAIN_GUARD_SEC, ledger_blob as queue_ledger, validate_evidence as queue_errors
from endpoints import validate_evidence as endpoint_errors
from open_loop import validate_supply
from load_contract import BOUNDARY_TOLERANCE_NS, arrivals, counters, worker_resources, worker_schedule
from queue_links import validate as link_errors


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                      allow_nan=False).encode()


def digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


MAX_JSON_BYTES = 64 * 1024 * 1024
MAX_COMPRESSED_BYTES = 32 * 1024 * 1024


def read_json(path):
    def reject(value):
        raise ValueError(f"nonfinite JSON number: {value}")
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"duplicate JSON key: {key}")
            result[key] = value
        return result
    path = Path(path)
    limit = MAX_COMPRESSED_BYTES if str(path).endswith(".gz") else MAX_JSON_BYTES
    if path.stat().st_size > limit:
        raise ValueError("evidence file exceeds input size limit")
    with path.open("rb") as stream:
        data = stream.read(limit + 1)
    if len(data) > limit:
        raise ValueError("evidence file grew beyond input size limit")
    if str(path).endswith(".gz"):
        with gzip.GzipFile(fileobj=io.BytesIO(data)) as stream:
            data = stream.read(MAX_JSON_BYTES + 1)
        if len(data) > MAX_JSON_BYTES:
            raise ValueError("decompressed evidence exceeds input size limit")
    try:
        value = json.loads(data, parse_constant=reject, object_pairs_hook=unique)
    except RecursionError as error:
        raise ValueError("evidence nesting exceeds JSON parser limit") from error
    check_finite(value)
    return value


def check_finite(value, depth=0):
    if depth > 64:
        raise ValueError("evidence nesting exceeds limit")
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError("nonfinite numeric observation")
    if isinstance(value, dict):
        for item in value.values():
            check_finite(item, depth + 1)
    elif isinstance(value, list):
        for item in value:
            check_finite(item, depth + 1)


def number(value, positive=False, integer=False):
    return (type(value) in ((int,) if integer else (int, float))
            and math.isfinite(value) and (value > 0 if positive else value >= 0))


def load_case(path):
    path = Path(path)
    result = read_json(path / ("result.json" if (path / "result.json").is_file() else "l2-result.json"))
    raw_path = path / "raw.json.gz"
    if raw_path.exists():
        raw = read_json(raw_path)
    else:
        raw = {}
        load = path / "load" if (path / "load").exists() else path
        for item in sorted(load.glob("client-*.json")) + sorted(load.glob("udp-*.json")):
            raw[item.name] = read_json(item)
        for name in ("endpoint-samples.json", "worker-samples.json"):
            if (load / name).is_file():
                raw[name] = read_json(load / name)
        if (path / "queue-link-samples.json").is_file():
            raw["queue-link-samples.json"] = read_json(path / "queue-link-samples.json")
        for name in ("worker.sha256", "forwarder.sha256", "light-capture.log", "target-capture.log"):
            if (path / name).exists():
                raw[name] = (path / name).read_text()
    analysis = read_json(path / "analysis.json") if (path / "analysis.json").exists() else {}
    return result, raw, analysis


def configuration(result, raw):
    """Full new manifest, or all *available* historical workload settings.

    Absence is retained as absence; a historical projection cannot certify an
    unrecorded runtime, MTU or offered-load setting.
    """
    if isinstance(result.get("effective_config"), dict):
        return result["effective_config"], True
    keys = ("memory_mb", "uplink_delay_ms", "phases_sec", "request_timeout_sec",
            "light_conns", "churn_requests", "udp_enabled", "udp_rate_hz",
            "udp_payload_bytes", "request_bytes", "request_rate_hz", "runtime",
            "mtu", "profile", "direction", "topology", "config", "manifest")
    config = {key: result[key] for key in keys if key in result}
    if isinstance(raw.get("client-0.json", {}).get("flows"), list):
        config["heavy_conns"] = len(raw["client-0.json"]["flows"])
    qdisc = result.get("qdisc")
    config["netem_limit_packets"] = qdisc.get("limit_packets") if isinstance(qdisc, dict) else None
    return config, False


def aligned_metrics(result):
    """Never divide a sampled CPU window by whole-load delivered bytes."""
    errors = []
    loaded = result.get("worker", {}).get("loaded", {})
    window = loaded.get("measurement_window")
    metrics = {"heavy_mbps": None, "worker_cpu_cores": None,
               "worker_cpu_sec_per_gib": None, "worker_peak_rss_mib": None}
    rss = loaded.get("rss_peak_kib")
    if number(rss):
        metrics["worker_peak_rss_mib"] = rss / 1024
    else:
        errors.append("missing or invalid loaded RSS observation")
    if not isinstance(window, dict):
        return metrics, errors + ["missing same-window CPU / verified-byte boundary samples"]
    start, end = window.get("start_ns"), window.get("end_ns")
    cpu, delivered = window.get("cpu_sec"), window.get("verified_bytes")
    if not number(start, integer=True) or not number(end, integer=True) or end <= start:
        errors.append("invalid monotonic measurement interval")
    if window.get("clock") != "CLOCK_MONOTONIC":
        errors.append("unknown measurement clock")
    if not number(cpu):
        errors.append("missing, negative or invalid CPU delta")
    if not number(delivered, integer=True):
        errors.append("missing, negative or invalid verified-byte delta")
    elif delivered == 0:
        errors.append("zero verified bytes; CPU/GiB is undefined")
    if not number(window.get("sample_count"), positive=True, integer=True) or window.get("sample_count", 0) < 2:
        errors.append("insufficient boundary samples")
    skew, tolerance = window.get("max_boundary_skew_ns"), window.get("boundary_tolerance_ns")
    if not number(skew, integer=True) or not number(tolerance, positive=True, integer=True) or skew > tolerance:
        errors.append("measurement boundary precision insufficient")
    planned_start, planned_end = window.get("planned_start_ns"), window.get("planned_end_ns")
    if (not number(planned_start, integer=True) or not number(planned_end, integer=True)
            or planned_end <= planned_start or not number(start, integer=True) or not number(end, integer=True)
            or not number(tolerance, positive=True, integer=True)
            or abs(start - planned_start) > tolerance or abs(end - planned_end) > tolerance):
        errors.append("missing/late planned phase boundaries; load-onset cannot be omitted")
    if window.get("errors"):
        errors.extend(str(x) for x in window["errors"])
    if not errors:
        seconds = (end - start) / 1e9
        metrics.update(heavy_mbps=delivered * 8 / seconds / 1e6,
                       worker_cpu_cores=cpu / seconds,
                       worker_cpu_sec_per_gib=cpu * 1024 ** 3 / delivered)
    return metrics, errors


def worker_evidence(timeline, result, config):
    """Recompute summaries from raw observations, including every missing sample.

    The preparatory observation also belongs to contamination accounting. A
    derived summary cannot substitute for an omitted raw sample or compiler scan.
    """
    errors, recomputed = [], {}
    try:
        start, phases = result.get("start_at"), config.get("phases_sec")
        tolerance = config.get("boundary_tolerance_ns")
        if (not number(start) or not isinstance(phases, dict) or set(phases) != {"baseline", "loaded", "recovery"}
                or any(not number(value, positive=True) for value in phases.values())
                or type(tolerance) is not int or tolerance != BOUNDARY_TOLERANCE_NS):
            raise ValueError("invalid frozen worker phase schedule / boundary tolerance")
        # Bound schedule expansion before allocating from an untrusted manifest.
        if sum(phases.values()) > 50_000:
            raise ValueError("worker phase schedule exceeds bounded evidence size")
        if not isinstance(timeline, list) or not timeline or len(timeline) > 100_005:
            raise ValueError("missing/invalid raw worker-samples.json timeline")
        schedule = [round(value * 1e9) for value in worker_schedule(start, phases)]
        previous, last_bytes, contaminated = None, None, False
        for index, row in enumerate(timeline):
            if not isinstance(row, dict):
                raise ValueError(f"worker sample {index}: invalid observation object")
            for key in ("scheduled_ns", "at_ns", "boundary_skew_ns", "rss_kib", "peak_rss_kib", "cpu_resolution_ns"):
                if not number(row.get(key), integer=True, positive=key == "cpu_resolution_ns"):
                    raise ValueError(f"worker sample {index}: missing/invalid {key} counter")
            if (not number(row.get("at")) or row["at"] != row["at_ns"] / 1e9
                    or not number(row.get("cpu_sec")) or row["peak_rss_kib"] < row["rss_kib"]):
                raise ValueError(f"worker sample {index}: invalid clock / CPU / RSS observation")
            amount = row.get("verified_bytes")
            if not number(amount, integer=True):
                errors.append(f"worker sample {index}: missing/invalid verified-byte counter")
            else:
                if last_bytes is not None and amount < last_bytes:
                    errors.append(f"worker sample {index}: verified-byte counter reset")
                last_bytes = amount
            compilers = row.get("compilers")
            if (not isinstance(compilers, list) or any(not isinstance(item, dict)
                    or set(item) != {"pid", "name"} or not number(item.get("pid"), positive=True, integer=True)
                    or not isinstance(item.get("name"), str) or not item["name"] for item in compilers)):
                raise ValueError(f"worker sample {index}: missing/invalid compiler contamination observation")
            contaminated |= bool(compilers)
            threads = row.get("threads")
            if (not isinstance(row.get("host_cpu_pressure"), str) or not row["host_cpu_pressure"]
                    or not isinstance(threads, dict) or any(not isinstance(pid, str) or not pid.isdigit()
                        or not isinstance(thread, dict) or set(thread) != {"name", "cpu_sec"}
                        or not isinstance(thread.get("name"), str) or not thread["name"]
                        or not number(thread.get("cpu_sec")) for pid, thread in threads.items())):
                raise ValueError(f"worker sample {index}: missing/invalid CPU diagnostic observations")
            if row["boundary_skew_ns"] > tolerance or row["at_ns"] < row["boundary_skew_ns"]:
                errors.append(f"worker sample {index}: measurement boundary precision insufficient")
            if index and abs(row["at_ns"] - row["scheduled_ns"]) > tolerance:
                errors.append(f"worker sample {index}: scheduled sampling precision insufficient")
            if previous and (row["at_ns"] <= previous["at_ns"] or row["scheduled_ns"] <= previous["scheduled_ns"]
                    or row["cpu_sec"] < previous["cpu_sec"] or row["cpu_resolution_ns"] != previous["cpu_resolution_ns"]):
                errors.append(f"worker sample {index}: CPU / clock counter reset or resolution changed")
            previous = row
        observed_schedule = [row["scheduled_ns"] for row in timeline]
        if (len(observed_schedule) != len(schedule) + 1 or observed_schedule[0] >= schedule[0]
                or observed_schedule[1:] != schedule):
            errors.append("worker samples omit or change the frozen schedule / preparatory observation")
        if result.get("performance_contaminated") is not contaminated:
            errors.append("derived performance contamination flag differs from raw worker observations")
        if contaminated:
            errors.append("performance contaminated by concurrent compiler / external work in raw worker observations")
        recomputed = worker_resources(timeline, start, phases, tolerance)
        for phase, values in recomputed.items():
            errors.extend(f"worker {phase}: {error}" for error in values["measurement_window"]["errors"])
        if canonical(result.get("worker")) != canonical(recomputed):
            errors.append("derived worker phase CPU / bytes / RSS windows differ from raw worker observations")
    except (AttributeError, KeyError, IndexError, TypeError, ValueError, OverflowError) as error:
        errors.append("worker evidence: " + str(error))
    return recomputed, sorted(set(errors))


def inspect_case(path):
    """Preserve observed functional failures even when performance is invalid."""
    result, raw, analysis = load_case(path)
    functional, invalid, functional_evidence = [], [], []
    for error in result.get("errors", []):
        (invalid if str(error).startswith(("test rig:", "missed arrival", "missed datagram arrival")) else functional).append(str(error))
    if result.get("ok") is False and not result.get("errors"):
        functional.append("run marked unsuccessful")
    if result.get("ok") not in (True, False):
        invalid.append("missing functional completion marker")
    if result.get("performance_contaminated") is True:
        invalid.append("performance contaminated by concurrent compiler / external work")
    elif result.get("performance_contaminated") is not False:
        invalid.append("missing performance contamination observation")
    heavy = raw.get("client-0.json", {}).get("flows")
    if not isinstance(heavy, list) or not heavy:
        invalid.append("missing heavy flow evidence")
    else:
        for index, flow in enumerate(heavy):
            if not isinstance(flow, dict):
                invalid.append(f"heavy flow {index}: malformed evidence")
                continue
            functional.extend(str(e) for e in flow.get("errors", []))
            if not number(flow.get("bytes"), integer=True):
                invalid.append(f"heavy flow {index}: missing/invalid verified bytes")
            elif flow["bytes"] == 0:
                functional.append(f"heavy flow {index}: no progress")
    for key, peer in raw.items():
        if not (key.startswith(("client-", "udp-")) and isinstance(peer, dict)):
            continue
        for error in peer.get("errors", []):
            (invalid if str(error).startswith(("missed arrival", "missed datagram arrival")) else functional).append(str(error))
        for sample in peer.get("samples", []):
            latency = sample.get("latency_ms")
            if latency is not None and not number(latency):
                invalid.append(f"{key}: invalid request latency")
            if sample.get("status") == "missed":
                invalid.append(f"{key}: generator missed request {sample.get('seq')}")
            if sample.get("status") in ("failed", "timed_out", "unfinished"):
                functional.append(f"{key}: request {sample.get('seq')} {sample['status']}")
    delay = result.get("uplink_delay_ms")
    if not number(delay):
        invalid.append("missing or invalid configured uplink delay")
    else:
        invalid.extend(qdisc_errors(result.get("qdisc"), delay))
    config, complete = configuration(result, raw)
    endpoint_invalid, endpoint_diagnostics = endpoint_errors(raw.get("endpoint-samples.json"), result, config)
    invalid.extend(endpoint_invalid)
    if result.get("endpoint_observer_error"):
        invalid.append("endpoint observer failed: " + str(result["endpoint_observer_error"]))
    if complete:
        for key in ("runtime", "profile", "mtu", "request_bytes", "heavy_conns", "light_conns",
                    "churn_requests", "udp_enabled", "udp_rate_hz", "phases_sec", "request_timeout_sec"):
            if key not in config or config[key] is None:
                invalid.append(f"effective configuration missing {key}")
        for key in ("mtu", "request_bytes", "heavy_conns", "light_conns", "memory_mb", "request_rate_hz", "udp_rate_hz", "request_timeout_sec"):
            if not number(config.get(key), positive=True):
                invalid.append(f"effective configuration missing/invalid positive {key}")
        if not number(config.get("churn_requests"), integer=True):
            invalid.append("effective configuration missing/invalid churn_requests")
        for key in ("runtime", "profile"):
            if not isinstance(config.get(key), str) or not config[key]:
                invalid.append(f"effective configuration missing/invalid {key}")
        if config.get("capture") is True:
            invalid.append("diagnostic packet capture cannot enter the clean performance track")
    path_errors, _ = inspect_path(result)
    invalid.extend(path_errors)
    functional_evidence.extend(path_errors)
    if config.get("outbound") == "relay":
        words = raw.get("forwarder.sha256", "").split()
        if not words or len(words[0]) != 64 or any(c not in "0123456789abcdef" for c in words[0]):
            functional_evidence.append("missing actual relay forwarder binary hash")
    if config.get("queue_control") is not None:
        control_report = result.get("queue_control")
        links = raw.get("queue-link-samples.json")
        if not isinstance(control_report, dict):
            functional_evidence.append("missing/invalid typed queue control report")
            control_report = {}
        if links != control_report.get("link_samples"):
            functional_evidence.append("raw queue link samples differ from the queue control report")
        phase_times = config.get("phases_sec", {})
        try:
            functional_evidence.extend(link_errors(links, before_ns=round(result["start_at"] * 1e9),
                after_ns=round((result["start_at"] + sum(phase_times.values())) * 1e9)))
        except (TypeError, ValueError, KeyError, OverflowError):
            functional_evidence.append("queue link samples lack the full actual load interval")
        fifo = isinstance(config["queue_control"], dict) and config["queue_control"].get("kind") == FIFO_KIND
        try:
            ledger = queue_ledger(path) if fifo else None
        except (OSError, ValueError) as error:
            functional_evidence.append(str(error))
            ledger = None
        control_errors = queue_errors(control_report, config["queue_control"], ledger)
        functional_evidence.extend(control_errors)
        start = result.get("start_at")
        phases = config.get("phases_sec", {})
        if not control_errors and number(start) and isinstance(phases, dict) and all(number(phases.get(key), positive=True) for key in ("baseline", "loaded", "recovery")):
            expected = {"start_ns": round((start + phases["baseline"]) * 1e9),
                        "end_ns": round((start + phases["baseline"] + phases["loaded"] - config["queue_control"]["delay_ms"] / 1000 - DRAIN_GUARD_SEC) * 1e9),
                        "recovery_start_ns": round((start + phases["baseline"] + phases["loaded"]) * 1e9)}
            if fifo:
                expected.update(measurement_start_ns=round(start * 1e9),
                                measurement_end_ns=round((start + phases["baseline"] + phases["loaded"] + phases["recovery"]) * 1e9))
            if result.get("queue_control", {}).get("planned") != expected:
                functional_evidence.append("queue control interval differs from the actual loaded phase")
        else:
            functional_evidence.append("queue control lacks the actual loaded phase interval")
        invalid.append("intentional test-side egress queue control; not clean performance evidence")
    elif result.get("queue_control") is not None:
        functional_evidence.append("unexpected queue control outside the frozen configuration")
    if complete:
        for key in ("memory_mb", "uplink_delay_ms", "phases_sec", "request_timeout_sec", "light_conns", "churn_requests", "topology"):
            if key in result and key in config and result[key] != config[key]:
                functional_evidence.append(f"observed {key} differs from effective configuration")
        if isinstance(heavy, list) and len(heavy) != config.get("heavy_conns"):
            functional_evidence.append("heavy flow count differs from effective configuration")
    # Require the intended light and UDP load, not just fast successful samples.
    light_peers = config.get("topology", {}).get("light_peers", [1, 2, 3])
    for peer_id in light_peers:
        peer = raw.get(f"client-{peer_id}.json")
        if not isinstance(peer, dict) or not isinstance(peer.get("samples"), list) or not peer["samples"]:
            functional_evidence.append(f"missing light peer {peer_id} request evidence")
            continue
        for phase in ("baseline", "loaded", "recovery"):
            if not any(s.get("phase") == phase for s in peer["samples"]):
                functional_evidence.append(f"light peer {peer_id} missing {phase} observations")
        for sample in peer["samples"]:
            if number(sample.get("latency_ms")) and number(result.get("request_timeout_sec"), positive=True) and sample["latency_ms"] >= result["request_timeout_sec"] * 1000:
                functional.append(f"light peer {peer_id} exceeded request deadline")
        if complete:
            try:
                if peer.get("counters") != counters(peer["samples"]):
                    functional_evidence.append(f"light peer {peer_id} counters do not cover all requests")
            except ValueError as error:
                functional_evidence.append(str(error))
            if config.get("light_mode") == "open-loop":
                invalid.extend(f"light peer {peer_id}: {error}" for error in validate_supply(peer, config, result.get("start_at")))
    if complete:
        expected_clients = {"client-0.json", *(f"client-{i}.json" for i in light_peers)}
        actual_clients = {key for key in raw if key.startswith("client-") and key.endswith(".json")}
        if actual_clients != expected_clients:
            functional_evidence.append("observed client identity set differs from the configured topology")
        if config.get("udp_enabled") is False and any(key.startswith("udp-") for key in raw):
            functional_evidence.append("UDP observations exist while effective configuration disables UDP")
    if config.get("udp_enabled"):
        for peer_id in range(4):
            peer = raw.get(f"udp-{peer_id}.json")
            if not isinstance(peer, dict) or not isinstance(peer.get("counters"), dict):
                functional_evidence.append(f"missing UDP peer {peer_id} scheduled/failed counters")
            elif complete:
                try:
                    table = arrivals(result.get("start_at"), sum(config["phases_sec"].values()), config["udp_rate_hz"])
                    samples = peer.get("samples")
                    if (not isinstance(samples, list) or [sample.get("scheduled_at") for sample in samples] != table
                            or [sample.get("seq") for sample in samples] != list(range(len(table)))
                            or peer["counters"] != counters(samples) or peer.get("scheduled") != len(table)):
                        invalid.append(f"UDP peer {peer_id}: frozen arrival supply/counters are incomplete")
                except (ValueError, TypeError, KeyError) as error:
                    invalid.append(f"UDP peer {peer_id}: invalid frozen supply: {error}")
    functional_evidence.extend(error for error in invalid if any(token in error for token in
                               ("path", "WG", "startup", "heavy flow", "missing heavy", "configuration missing")))
    invalid.extend(functional_evidence)
    recomputed_worker, worker_errors = worker_evidence(raw.get("worker-samples.json"), result, config)
    invalid.extend(worker_errors)
    metrics, metric_errors = aligned_metrics({"worker": recomputed_worker})
    invalid.extend(metric_errors)
    if worker_errors:
        metrics = {key: None for key in metrics}
    if not complete:
        invalid.append("historical record has no full effective configuration manifest")
    # A candidate with no verified work cannot look cheaper. These metrics are
    # unusable whenever a functional failure changes the completed workload.
    if functional:
        invalid.append("functional failure changes completed workload; performance is descriptive only")
    binary = raw.get("worker.sha256", "").split()
    binary_hash = binary[0] if binary else None
    if not isinstance(binary_hash, str) or len(binary_hash) != 64 or any(c not in "0123456789abcdef" for c in binary_hash):
        invalid.append("missing or malformed worker binary hash")
    return {"result": result, "raw": raw, "analysis": analysis,
            "config": config, "config_complete": complete,
            "functional_errors": sorted(set(functional)), "functional_evidence_errors": sorted(set(functional_evidence)), "performance_errors": sorted(set(invalid)),
            "metrics": metrics, "endpoint_diagnostics": endpoint_diagnostics, "binary_sha256": binary_hash}
