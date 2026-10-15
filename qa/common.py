"""Strict JSON, stable identities, and bounded, non-executable artifacts."""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MAX_JSON_BYTES = 16 * 1024 * 1024
HASH = re.compile(r"^[a-f0-9]{64}$")


class EvidenceError(ValueError):
    pass


def _pairs(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise EvidenceError(f"duplicate JSON key: {key}")
        value[key] = item
    return value


def finite(value, path="$", depth=0):
    if depth > 64:
        raise EvidenceError(f"{path}: excessive nesting")
    if isinstance(value, float) and not math.isfinite(value):
        raise EvidenceError(f"{path}: non-finite number")
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise EvidenceError(f"{path}: non-string key")
            finite(item, f"{path}.{key}", depth + 1)
    elif isinstance(value, list):
        for index, item in enumerate(value):
            finite(item, f"{path}[{index}]", depth + 1)


def read_json(path):
    path = Path(path)
    if path.stat().st_size > MAX_JSON_BYTES:
        raise EvidenceError(f"oversized JSON: {path.name}")
    try:
        value = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=_pairs,
                           parse_constant=lambda x: (_ for _ in ()).throw(EvidenceError(f"invalid number: {x}")))
        finite(value)
        return value
    except (UnicodeError, json.JSONDecodeError, RecursionError) as error:
        raise EvidenceError(f"invalid JSON: {path.name}: {error}") from error


def canonical(value):
    """v1: UTF-8, sorted keys, compact Python JSON numbers, finite values only.

    Identity-bearing durations/counts use integers. 1 and 1.0 are deliberately
    distinct; consumers must not round/rewrite a signed or hashed document.
    """
    finite(value)
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                      allow_nan=False).encode("utf-8")


def digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def file_hash(path):
    h = hashlib.sha256()
    with open(path, "rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def write_json(path, value, *, exclusive=False):
    """Atomic replace; exclusive inputs/outputs cannot overwrite earlier evidence."""
    data = canonical(value) + b"\n"
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + f".{os.getpid()}.tmp")
    try:
        with temporary.open("xb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        if exclusive:
            os.link(temporary, path)
        else:
            os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def safe_path(root, relative):
    if not isinstance(relative, str) or not relative or "\\" in relative:
        raise EvidenceError("invalid artifact path")
    name = Path(relative)
    if name.is_absolute() or any(p in ("..", ".") for p in name.parts):
        raise EvidenceError(f"unsafe artifact path: {relative}")
    root = Path(root).resolve()
    path = root / name
    # Reject symlinks even when the target currently lies inside the archive.
    if any(parent.is_symlink() for parent in [path, *path.parents] if parent != root.parent):
        raise EvidenceError(f"symlink artifact: {relative}")
    if not path.resolve().is_relative_to(root):
        raise EvidenceError("artifact escapes root")
    return path


def case_identity(case):
    fields = ("case_id", "case_version", "layer", "platform", "profile", "toolchain", "features", "config", "runner", "integration_build", "service_contract")
    return digest({key: case[key] for key in fields if key in case})


def policy_hash(policy_dir):
    root = Path(policy_dir)
    files = sorted(root.glob("*.json"))
    if not files:
        raise EvidenceError("empty policy directory")
    return digest({p.name: read_json(p) for p in files})
