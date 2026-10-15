#!/usr/bin/env python3
"""Small supervised exec helper: immutable argv, affinity, PID and file budget.

This runs outside the measured probe's timed loop. exec preserves its PID and
cgroup. RLIMIT_FSIZE is inherited by all descendants and bounds their shared
regular output file even if they write faster than the supervisor can poll.
"""
import json
import os
from pathlib import Path
import resource
import sys
import time


def main():
    config = json.loads(Path(sys.argv[1]).read_text())
    if config["cpu"] is not None:
        os.sched_setaffinity(0, {config["cpu"]})
    limit = config["output_limit"]
    resource.setrlimit(resource.RLIMIT_FSIZE, (limit, limit))
    identity = Path(config["pid_path"])
    temporary = identity.with_suffix(".tmp")
    temporary.write_text(json.dumps({"pid": os.getpid()}))
    temporary.replace(identity)
    deadline = time.monotonic() + 5
    while not Path(config["go_path"]).exists():
        if time.monotonic() > deadline:
            raise RuntimeError("environment observer did not acknowledge DUT PID")
        time.sleep(.002)
    os.execv(config["argv"][0], config["argv"])


if __name__ == "__main__":
    main()
