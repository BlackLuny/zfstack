# OVH physical-host gate plan — registered before measurement

Target: root@51.75.146.55:2280. Read-only checks report Debian kernel
6.12.57+deb13-amd64, systemd-detect-virt=none, Xeon E3-1270 v6 (4 cores,
8 threads), 32 GiB RAM. The user confirmed the running services are test-only,
no competing tasks will be run during testing, and tracing is permitted.
Existing services, host networking, CPU affinity, priorities and power settings
will not be changed. No system packages will be installed or upgraded.

Freeze the commit containing this plan and the receiver-order gate fix; record
its full SHA and bundle SHA-256 before execution. Clone an explicit local branch
from a verified bundle into a new remote task directory. Output stays outside
that clean checkout. No previous VPS measurement is repeated.

Run remote_selftests.py exactly once at that SHA, with public lease directory
/var/lock/zfstack-qa and domain zfstack-qa-baremetal-51.75.146.55-2280.
All privileged networking stays inside qa/isolate-linux.sh. Order:
capabilities, full, cancel, overflow, one each. If ANY mode has overtaken UDP,
run exactly three historical plain-netem controls. The driver records its plan
before execution and /proc/stat and /proc/softirqs before and after each step.
No tracing in this qualification gate. Record before/after host state.

PASS additionally requires every mode's raw receiver sequence to be strictly
increasing without duplicates. Overflow may omit expected dropped sequences;
its existing loss accounting remains mandatory. Missing/malformed receiver
samples fail closed. The diagnostic timing mapper requires complete sequences
and must not index overflow gaps as if they were contiguous.

Keep 20 ms, strict order and all workload/queue parameters unchanged. Retain
all outcomes. A FAIL stops case registration, new gate runs and formal C16;
collect, hash and independently recompute the raw evidence first. Any later
trace/ablation is a separately pre-registered diagnostic, never a replacement
qualification run. If PASS, follow PR #75 steps: archive stage1, register only
case v3/netem-fifo plus the BLOCKED note, gate the new SHA, then frozen worker
build and one held-out formal C16 with the same other four repository versions.
Even a formal PASS keeps known-BLOCKED pending maintainer review.

Preparation failures: initial local QA invocations used the system Python
without jsonschema (two suite attempts and one audit import failure). No remote
measurement had started. Reuse the existing /tmp/zfstack-pr75-remote23-qa-venv
for QA; do not install dependencies on the host. The complete second failed
suite log is retained as local-qa-missing-dependency.log.
