"""Exclusive physical-domain ownership; stale/foreign leases never authorize cleanup."""
from __future__ import annotations

import os
import time
import uuid
from pathlib import Path

from .common import EvidenceError, canonical, digest, read_json


class Lease:
    def __init__(self, directory, domain, run_id, generation):
        self.directory = Path(directory)
        self.path = self.directory / (digest(domain) + ".json")
        self.value = {"domain": domain, "run_id": run_id, "generation": generation,
                      "owner": str(uuid.uuid4()), "pid": os.getpid(), "created_ns": time.time_ns()}
        self.owned = False

    def __enter__(self):
        self.directory.mkdir(parents=True, exist_ok=True)
        if self.path.with_suffix(".quarantine").exists():
            raise EvidenceError("node is quarantined")
        try:
            with self.path.open("xb") as stream:
                stream.write(canonical(self.value))
                stream.flush()
                os.fsync(stream.fileno())
        except FileExistsError as error:
            raise EvidenceError("physical domain already leased; stale leases require operator recovery") from error
        self.owned = True
        return self

    def check(self):
        if not self.owned or not self.path.is_file() or read_json(self.path) != self.value:
            raise EvidenceError("lease lost or generation changed")

    def quarantine(self, reason):
        self.path.with_suffix(".quarantine").write_text(str(reason) + "\n")

    def __exit__(self, kind, value, traceback):
        try:
            self.check()
        except EvidenceError:
            self.quarantine("lease ownership changed; inspect before reallocation")
            raise
        self.path.unlink()
        self.owned = False
