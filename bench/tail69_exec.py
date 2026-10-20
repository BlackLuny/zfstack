#!/usr/bin/env python3
"""Linux wrapper: record child process CPU/RSS after a benchmark exits."""
import json
from pathlib import Path
import resource
import subprocess
import sys
import time

start=time.monotonic()
p=subprocess.run(sys.argv[2:])
r=resource.getrusage(resource.RUSAGE_CHILDREN)
Path(sys.argv[1]).write_text(json.dumps({"returncode":p.returncode,"wall_seconds":time.monotonic()-start,
    "child_user_seconds":r.ru_utime,"child_system_seconds":r.ru_stime,"max_rss_kib":r.ru_maxrss},indent=2)+"\n")
raise SystemExit(p.returncode)
