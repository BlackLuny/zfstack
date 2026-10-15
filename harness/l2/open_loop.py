"""Frozen request and connection arrivals; one bounded event loop per lane.

An epoch owns N consecutive planned requests, independent of their responses.
Slow old connections can overlap later epochs but can never postpone them.
"""
import errno
from collections import deque
import hashlib
import json
import math
import os
import selectors
import socket
import struct
import time

from load_contract import arrivals, counters

CONNECT_LEAD_SEC = .25
MAX_OPEN_EPOCHS = 64
MAX_PENDING_WRITES = 32
MAX_ARRIVALS = 200_000


def frozen_schedule(a, index=0, *, rate, churn):
    duration = a.baseline + a.loaded + a.recovery
    if (type(churn) is not int or churn < 0 or type(index) is not int or not 0 <= index < 128
            or not math.isfinite(rate) or rate <= 0 or not math.isfinite(duration) or duration <= 0
            or not math.isfinite(a.request_timeout) or a.request_timeout <= 0
            or math.ceil(duration * rate) > MAX_ARRIVALS):
        raise ValueError("open-loop schedule exceeds the fixed arrival/connection bounds")
    table = arrivals(a.start, duration, rate)
    size = churn or len(table)
    samples, epochs = [], []
    for ordinal, at in enumerate(table):
        epoch = ordinal // size
        phase = "baseline" if at < a.start + a.baseline else "loaded" if at < a.start + a.baseline + a.loaded else "recovery"
        samples.append({"seq": (index << 24) + ordinal, "epoch": epoch, "phase": phase,
                        "scheduled_at": at, "started_at": None, "status": "unfinished"})
        if ordinal % size == 0:
            last = min(ordinal + size, len(table)) - 1
            epochs.append({"epoch": epoch, "scheduled_at": at - CONNECT_LEAD_SEC,
                           "first_seq": (index << 24) + ordinal, "sample_count": last - ordinal + 1,
                           "last_arrival_at": table[last], "deadline_at": table[last] + a.request_timeout})
    plan = {"schema_version": 1, "connection": index, "offered_rate_hz": rate, "churn_requests": churn,
            "connect_lead_sec": CONNECT_LEAD_SEC, "max_open_epochs": MAX_OPEN_EPOCHS, "max_pending_writes": MAX_PENDING_WRITES,
            "arrivals": [{key: sample[key] for key in ("seq", "epoch", "phase", "scheduled_at")} for sample in samples],
            "epochs": epochs}
    plan_hash = hashlib.sha256(json.dumps(plan, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()
    return plan, plan_hash, samples


def run_connection(a, index=0, churn=None):
    from load import pattern
    rate = float(os.environ.get("L2_LIGHT_RATE_HZ", "100"))
    max_lag = float(os.environ.get("L2_MAX_GENERATOR_LAG_SECS", ".02"))
    churn = int(os.environ.get("L2_CHURN_REQUESTS", "0")) if churn is None else churn
    if not math.isfinite(max_lag) or max_lag <= 0:
        raise ValueError("invalid generator lag bound")
    plan, plan_hash, samples = frozen_schedule(a, index, rate=rate, churn=churn)
    connects = [{**epoch, "started_at": None, "connected_at": None, "ready_at": None,
                 "closed_at": None, "status": "unfinished"} for epoch in plan["epochs"]]
    by_seq = {sample["seq"]: sample for sample in samples}
    selector, live, errors = selectors.DefaultSelector(), {}, []
    next_epoch = cursor = peak = 0

    def finish_sample(sample, state, why, now):
        sample.update(status=state, ended_at=now, outcome=why)
        prefix = "missed arrival" if state == "missed" else "request " + state
        errors.append(f"{prefix} seq={sample['seq']} epoch={sample['epoch']}: {why}")

    def close_epoch(epoch, now, error=None, timed_out=False):
        item = live.pop(epoch, None)
        if item:
            try:
                selector.unregister(item["socket"])
            except (KeyError, ValueError, OSError) as exc:
                error = error or "selector cleanup failed: " + str(exc)
            finally:
                item["socket"].close()
        record = connects[epoch]
        record["closed_at"] = now
        if error:
            record.update(status="timed_out" if timed_out else "failed", error=str(error))
            errors.append(f"connection {record['status']} epoch={epoch}: {error}")
            for sample in samples[epoch * (churn or len(samples)):(epoch + 1) * (churn or len(samples))]:
                if sample["status"] == "unfinished" and (sample["started_at"] is not None or sample["scheduled_at"] <= now):
                    finish_sample(sample, "timed_out" if timed_out else "failed", "connection ended: " + str(error), now)
        elif record["status"] == "connected":
            record["status"] = "completed"

    def events(item):
        mask = selectors.EVENT_READ
        if item["connecting"] or item["writes"]:
            mask |= selectors.EVENT_WRITE
        selector.modify(item["socket"], mask, item)

    try:
        while cursor < len(samples) or next_epoch < len(connects) or live:
            now = time.monotonic()
            # Capacity exhaustion is a supply miss, never a deferred attempt.
            while next_epoch < len(connects) and connects[next_epoch]["scheduled_at"] <= now:
                epoch, next_epoch = next_epoch, next_epoch + 1
                record = connects[epoch]
                record["generator_lag_ms"] = (now - record["scheduled_at"]) * 1000
                if now - record["scheduled_at"] > max_lag or len(live) >= MAX_OPEN_EPOCHS:
                    record.update(status="missed", closed_at=now,
                                  error="connection arrival late" if now - record["scheduled_at"] > max_lag else "bounded epoch capacity exhausted")
                    errors.append(f"missed arrival connection epoch={epoch}: {record['error']}")
                    continue
                record["started_at"] = now
                sock = None
                try:
                    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                    sock.setblocking(False)
                    sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                    code = sock.connect_ex((a.host, a.port))
                    if code not in (0, errno.EINPROGRESS, errno.EWOULDBLOCK, errno.EALREADY):
                        raise OSError(code, os.strerror(code))
                    item = {"epoch": epoch, "socket": sock, "connecting": True,
                            "writes": deque([[None, b"E" + struct.pack("!I", a.flow), 0]]),
                            "received": bytearray(), "pending": {}}
                    selector.register(sock, selectors.EVENT_READ | selectors.EVENT_WRITE, item)
                    live[epoch] = item
                    peak = max(peak, len(live))
                except OSError as exc:
                    if sock:
                        try:
                            selector.unregister(sock)
                        except (KeyError, ValueError, OSError):
                            pass
                        sock.close()
                    live.pop(epoch, None)
                    record.update(status="failed", closed_at=time.monotonic(), error=str(exc))
                    errors.append(f"connection failed epoch={epoch}: {exc}")

            while cursor < len(samples) and samples[cursor]["scheduled_at"] <= now:
                sample, cursor = samples[cursor], cursor + 1
                sample["generator_lag_ms"] = (now - sample["scheduled_at"]) * 1000
                record, item = connects[sample["epoch"]], live.get(sample["epoch"])
                if now - sample["scheduled_at"] > max_lag:
                    finish_sample(sample, "missed", "request arrival late", now)
                elif record["status"] == "missed":
                    finish_sample(sample, "missed", record["error"], now)
                elif not item or record["ready_at"] is None:
                    state = "timed_out" if record["status"] == "timed_out" else "failed"
                    finish_sample(sample, state, "scheduled connection unavailable", now)
                elif len(item["writes"]) >= MAX_PENDING_WRITES:
                    finish_sample(sample, "missed", "bounded generator write queue full", now)
                else:
                    frame = struct.pack("!II", a.flow, sample["seq"]) + pattern(a.flow)[:248]
                    item["writes"].append([sample, frame, 0])
                    item["pending"][sample["seq"]] = sample
                    events(item)

            for epoch, item in list(live.items()):
                record = connects[epoch]
                if item["connecting"] and now >= record["scheduled_at"] + a.request_timeout:
                    close_epoch(epoch, now, "connection deadline", True)
                    continue
                for seq, sample in list(item["pending"].items()):
                    if now >= sample["scheduled_at"] + a.request_timeout:
                        item["pending"].pop(seq)
                        finish_sample(sample, "timed_out", "request deadline", now)
                        queued = next((write for write in item["writes"] if write[0] is sample), None)
                        if queued:
                            if queued[2]:
                                close_epoch(epoch, now, "partially transmitted request deadline", True)
                                break
                            item["writes"].remove(queued)
                            events(item)
                if epoch not in live:
                    continue
                if now >= record["last_arrival_at"] and not item["pending"] and not item["writes"]:
                    close_epoch(epoch, now)
                elif now >= record["deadline_at"]:
                    close_epoch(epoch, now, "epoch deadline", True)

            for key, mask in selector.select(.002):
                item = key.data
                epoch, sock = item["epoch"], item["socket"]
                if epoch not in live:
                    continue
                record = connects[epoch]
                try:
                    if item["connecting"] and mask & selectors.EVENT_WRITE:
                        code = sock.getsockopt(socket.SOL_SOCKET, socket.SO_ERROR)
                        if code:
                            raise OSError(code, os.strerror(code))
                        item["connecting"] = False
                        record.update(status="connected", connected_at=time.monotonic())
                        record["latency_ms"] = (record["connected_at"] - record["started_at"]) * 1000
                    if not item["connecting"] and mask & selectors.EVENT_WRITE:
                        # Coalesced wakeups may expose several still-on-time
                        # arrivals. Drain their bounded FIFO until EAGAIN, with
                        # each request's original first-send deadline intact.
                        while item["writes"]:
                            sample, data, offset = item["writes"][0]
                            sent_at = time.monotonic()
                            if sample and sample["started_at"] is None and sent_at - sample["scheduled_at"] > max_lag:
                                item["pending"].pop(sample["seq"])
                                finish_sample(sample, "missed", "generator write deadline", sent_at)
                                item["writes"].popleft()
                                continue
                            if sample and sample["started_at"] is not None and sent_at >= sample["scheduled_at"] + a.request_timeout:
                                close_epoch(epoch, sent_at, "partially transmitted request deadline", True)
                                break
                            try:
                                count = sock.send(data[offset:])
                            except BlockingIOError:
                                break
                            if not count:
                                break
                            if sample and sample["started_at"] is None:
                                sample.update(started_at=sent_at, at=sent_at,
                                              generator_lag_ms=(sent_at - sample["scheduled_at"]) * 1000)
                            item["writes"][0][2] += count
                            if item["writes"][0][2] == len(data):
                                item["writes"].popleft()
                                if sample is None:
                                    record["ready_at"] = time.monotonic()
                        if epoch not in live:
                            continue
                        events(item)
                    if not item["connecting"] and mask & selectors.EVENT_READ:
                        try:
                            data = sock.recv(65536)
                        except BlockingIOError:
                            continue
                        if not data:
                            raise EOFError("early EOF")
                        item["received"].extend(data)
                        while len(item["received"]) >= 256:
                            frame = bytes(item["received"][:256]); del item["received"][:256]
                            flow, seq = struct.unpack("!II", frame[:8])
                            sample = by_seq.get(seq)
                            if (flow != a.flow or sample is None or sample["epoch"] != epoch
                                    or frame != struct.pack("!II", flow, seq) + pattern(a.flow)[:248]):
                                raise ValueError("corrupt/cross-flow response")
                            end = time.monotonic()
                            if sample["status"] == "timed_out":
                                sample["late_completed_at"] = end
                            elif sample["status"] != "unfinished" or sample["started_at"] is None:
                                raise ValueError("duplicate/unscheduled response")
                            else:
                                item["pending"].pop(seq)
                                sample.update(status="completed", outcome="verified", ended_at=end,
                                              latency_ms=(end - sample["started_at"]) * 1000,
                                              arrival_to_completion_ms=(end - sample["scheduled_at"]) * 1000)
                except (OSError, EOFError, ValueError) as exc:
                    close_epoch(epoch, time.monotonic(), exc)
    except Exception as exc:
        errors.append("test rig: open-loop generator aborted: " + str(exc))
        now = time.monotonic()
        for sample in samples:
            if sample["status"] == "unfinished":
                sample.update(outcome="generator aborted before completion", ended_at=now)
    finally:
        for epoch in list(live):
            close_epoch(epoch, time.monotonic(), "generator interrupted")
        selector.close()
    return {"connection": index, "samples": samples, "connects": connects, "errors": errors,
            "counters": counters(samples), "arrival_mode": "open-loop", "offered_rate_hz": rate,
            "churn_requests": churn, "schedule": plan, "schedule_hash": plan_hash, "peak_open_epochs": peak}


def validate_supply(peer, config, start):
    """Rebuild planned epochs/arrivals rather than trust offered-rate labels."""
    from types import SimpleNamespace
    errors = []
    try:
        phases = config["phases_sec"]
        a = SimpleNamespace(start=start, baseline=phases["baseline"], loaded=phases["loaded"], recovery=phases["recovery"],
                            request_timeout=config["request_timeout_sec"])
        count, churn, rate = config["light_conns"], config["churn_requests"], config["request_rate_hz"]
        if type(count) is not int or not 1 <= count <= 128 or not isinstance(peer, dict):
            raise ValueError("invalid open-loop lane population")
        rows = peer.get("connections", [peer])
        if (peer.get("arrival_mode") != "open-loop" or not isinstance(rows, list) or len(rows) != count
                or [row.get("connection") for row in rows] != list(range(count))):
            raise ValueError("missing fixed open-loop lane/epoch evidence")
        merged = []
        for index, row in enumerate(rows):
            plan, plan_hash, expected = frozen_schedule(a, index, rate=rate, churn=churn)
            if (row.get("schedule") != plan or row.get("schedule_hash") != plan_hash or row.get("churn_requests") != churn
                    or row.get("arrival_mode") != "open-loop" or row.get("offered_rate_hz") != rate):
                errors.append("open-loop frozen connection/request schedule differs")
            samples, connects = row.get("samples"), row.get("connects")
            if not isinstance(samples, list) or len(samples) != len(expected) or row.get("counters") != counters(samples):
                errors.append("open-loop scheduled request population/counters are incomplete")
                continue
            merged.extend(samples)
            for sample, scheduled in zip(samples, expected):
                if any(sample.get(key) != scheduled[key] for key in ("seq", "epoch", "phase", "scheduled_at")):
                    errors.append("open-loop arrival/epoch assignment changed")
                state, began, ended = sample.get("status"), sample.get("started_at"), sample.get("ended_at")
                if (state not in ("completed", "failed", "timed_out", "missed", "unfinished")
                        or not isinstance(sample.get("outcome"), str)
                        or type(ended) not in (int, float) or not math.isfinite(ended) or ended < sample["scheduled_at"]):
                    errors.append("open-loop terminal request outcome is incomplete")
                if state in ("missed", "unfinished"):
                    errors.append("open-loop generator did not supply every frozen arrival")
                if began is not None and (type(began) not in (int, float) or not math.isfinite(began)
                                          or not 0 <= began - sample["scheduled_at"] <= config["max_generator_lag_sec"]):
                    errors.append("open-loop actual send missed its fixed arrival bound")
                if state == "completed" and began is None:
                    errors.append("open-loop completion lacks an actual send")
            if (not isinstance(connects, list) or len(connects) != len(plan["epochs"])
                    or type(row.get("peak_open_epochs")) is not int or not 0 <= row["peak_open_epochs"] <= MAX_OPEN_EPOCHS):
                errors.append("open-loop connection population or concurrency evidence is missing")
                continue
            for record, epoch in zip(connects, plan["epochs"]):
                if any(record.get(key) != value for key, value in epoch.items()):
                    errors.append("open-loop connection arrival schedule changed")
                if record.get("status") == "missed":
                    errors.append("open-loop generator missed a fixed connection arrival")
                elif record.get("status") not in ("completed", "failed", "timed_out"):
                    errors.append("open-loop connection outcome is incomplete")
                actual = record.get("started_at")
                if actual is not None and (type(actual) not in (int, float) or not math.isfinite(actual)
                                           or not 0 <= actual - epoch["scheduled_at"] <= config["max_generator_lag_sec"]):
                    errors.append("open-loop connection attempt missed the frozen time")
                if record.get("status") == "completed":
                    times = [record.get(key) for key in ("started_at", "connected_at", "ready_at", "closed_at")]
                    if any(type(value) not in (int, float) or not math.isfinite(value) for value in times) or times != sorted(times):
                        errors.append("open-loop connection lifecycle is incomplete")
        merged.sort(key=lambda sample: sample["scheduled_at"])
        if merged != peer.get("samples") or peer.get("counters") != counters(merged):
            errors.append("open-loop aggregate removed or changed lane observations")
    except (AttributeError, KeyError, IndexError, TypeError, ValueError, OverflowError) as error:
        errors.append("open-loop supply evidence: " + str(error))
    return sorted(set(errors))
