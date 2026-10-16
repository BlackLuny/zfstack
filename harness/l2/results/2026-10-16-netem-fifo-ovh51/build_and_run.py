"""Frozen native build, then exactly one C16 pair. Execute on assigned OVH physical host only."""
import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
E = ROOT / "evidence"
parser = argparse.ArgumentParser()
parser.add_argument("--attempt", type=int, default=1)
parser.add_argument("--input-archive", default="head-head-sources.tar.gz")
parser.add_argument("--transfer-manifest", default="build-input-transfer.json")
parser.add_argument("--cargo-quiet", action="store_true")
args = parser.parse_args()
assert args.attempt >= 1
suffix = "" if args.attempt == 1 else "-" + str(args.attempt)
build = ROOT / ("build" + suffix)
plan = json.loads((ROOT / "build-and-formal-plan.json").read_text())
sha = plan["source_refs"]["zfstack"]
for item in json.loads((ROOT / args.transfer_manifest).read_text()):
    path = ROOT / item["name"]
    assert path.stat().st_size == item["size_bytes"]
    assert hashlib.sha256(path.read_bytes()).hexdigest() == item["sha256"], path

env = os.environ.copy()
env.update(RUSTUP_HOME=str(ROOT / "rustup"), CARGO_HOME=str(ROOT / "cargo-home"),
           PYTHONDONTWRITEBYTECODE="1", PATH=str(ROOT / "qa-venv/bin") + ":/root/.cargo/bin:" + env["PATH"])
if args.cargo_quiet:
    env["CARGO_TERM_QUIET"] = "true"
for key in ("RUSTFLAGS", "CARGO_ENCODED_RUSTFLAGS", "RUSTDOCFLAGS", "CC", "CXX", "CARGO_BUILD_TARGET"):
    env.pop(key, None)
records = []


def run(label, argv, *, cwd=None, timeout=1200, required=True):
    started = time.time_ns()
    path = E / (label + suffix + ".log")
    with path.open("xb") as log:
        result = subprocess.run(argv, cwd=cwd or ROOT, env=env, stdout=log,
                                stderr=subprocess.STDOUT, timeout=timeout)
    rec = {"label": label, "argv": argv, "cwd": str(cwd or ROOT),
           "started_unix_ns": started, "finished_unix_ns": time.time_ns(), "returncode": result.returncode}
    records.append(rec)
    (E / ("build-formal-execution" + suffix + ".json")).write_text(json.dumps(records, indent=2)+"\n")
    print(label, "exit", result.returncode, flush=True)
    if required and result.returncode:
        raise RuntimeError(label + " failed; retained, no automatic repetition")
    return result.returncode


archive = ROOT / ("frozen-matrix" + suffix)
archive.mkdir()
run("extract-frozen-inputs", ["tar", "-xzf", str(ROOT / args.input_archive), "-C", str(archive)])
checkout = ROOT / "checkout"
assert subprocess.check_output(["git", "-C", str(checkout), "rev-parse", "HEAD"], text=True).strip() == sha
run("checkout-tested-commit", ["git", "-c", "core.hooksPath=/dev/null", "-C", str(checkout), "checkout", "--detach", sha])
python = str(ROOT / "qa-venv/bin/python")
node = {"kind": "resettable-test-node", "isolated": True,
        "physical_domain": "zfstack-qa-baremetal-51.75.146.55-2280",
        "generation": Path("/proc/sys/kernel/random/boot_id").read_text().strip(),
        "rig_lease_held": True, "rig_path": str(archive / "head-head/repos/zfc-rig"),
        "rig_build": str(ROOT / "fixtures/build"),
        "worker_binary": str(build / "head-head/zf-worker"),
        "worker_manifest": str(build / "head-head/worker-manifest.json"),
        "isolation_scope": "verified private network, mount and PID namespaces; reset by wrapper",
        "shared_KVM_VPS": False, "physical_CPU_exclusivity": "user-confirmed test-only host, no competing jobs",
        "performance_certified": False}
node_file = ROOT / ("node" + suffix + ".json")
node_file.write_text(json.dumps(node, indent=2)+"\n")
shutil.copyfile(node_file, E / node_file.name)
run("native-build", [python, "-m", "qa.crossrepo", "build", "--archive", str(archive),
    "--output", str(build), "--node", str(node_file),
    "--lease-dir", "/var/lock/zfstack-qa", "--cell", "head-head", "--toolchain", "1.98.1",
    "--profile", "fast-release", "--jobs", "2", "--cache-dir", str(ROOT / "build-cache")],
    cwd=checkout, timeout=12000)

fixtures = ROOT / "fixtures/build/tools"
fixtures.mkdir(parents=True)
shutil.copyfile(build / "head-head/fakectl", fixtures / "fakectl")
(fixtures / "fakectl").chmod(0o755)
run("make-frozen-qa-plan", [python, "-m", "qa", "make-plan", "--suite", "nightly",
    "--platform", "linux-x86_64", "--tested-sha", sha, "--output", str(E / "plan.json")], cwd=checkout)
worker = json.loads((build / "head-head/worker-manifest.json").read_text())
assert worker["status"] == "BUILT" and not worker["errors"]
assert worker["zfstack_sha"] == sha
prereg = {"created_unix_ns": time.time_ns(), "case": "C16-l2-queue-control", "runs": 1,
          "arms": ["baseline", "fault"], "settle_before_measurement_sec": 5,
          "worker_binary_sha256": worker["binary_sha256"],
          "worker_manifest_sha256": hashlib.sha256((build / "head-head/worker-manifest.json").read_bytes()).hexdigest(),
          "plan_sha256": hashlib.sha256((E / "plan.json").read_bytes()).hexdigest(),
          "changes_to_policy_or_workload": [], "retries": 0}
(E / "formal-preregistration.json").write_text(json.dumps(prereg, indent=2)+"\n")
time.sleep(5)
run("formal-c16", [python, "-m", "qa", "run-plan", "--plan", str(E / "plan.json"),
    "--case", "C16-l2-queue-control", "--node", str(node_file),
    "--lease-dir", "/var/lock/zfstack-qa", "--output", str(ROOT / "formal")],
    cwd=checkout, timeout=1500, required=False)

code = '''import json,sys
from pathlib import Path
from qa.evaluate import evaluate
from qa.archive import archive
from qa.common import read_json,write_json
from qa.integration import verify_integration,queue_negative_evidence
from qa.common import EvidenceError
from qa.render_report import render
root=Path(sys.argv[1]); checkout=root/'checkout'; e=root/'evidence'
plan=read_json(e/'plan.json')
envelopes=[]
for path in (root/'formal').rglob('result.json'):
 value=read_json(path)
 if value.get('schema_version')==2 and 'case_instance_id' in value: envelopes.append(path)
assert len(envelopes)==1,envelopes
result=read_json(envelopes[0]); case=next(c for c in plan['cases'] if c['case_id']=='C16-l2-queue-control')
directory=envelopes[0].parent
worker=read_json(directory/'worker-manifest.json')
status,details,observations=queue_negative_evidence(case,directory/'baseline',directory/'fault',worker)
verification_error=None
try: verify_integration(result,case,directory)
except EvidenceError as error: verification_error=str(error)
write_json(e/'independent-c16-verification.json',{'functional':status,'details':details,'observations':observations,'verification_error':verification_error,'result_file':str(envelopes[0])},exclusive=True)
report=evaluate(plan,envelopes,checkout/'qa/policy',expected_sha=plan['tested_sha'])
write_json(e/'evaluation.json',report,exclusive=True)
(e/'evaluation.md').write_text(render(report))
archive(root/'formal',e/'formal-artifacts.tar.gz')
print(json.dumps({'case_functional':status,'aggregate':report['status'],'case_details':details}))
'''
run("independent-evaluation-and-archive", [python, "-c", code, str(ROOT)], cwd=checkout)
for label, command in [("host-after-services", ["docker", "ps", "--format", "{{.Names}} {{.Status}}"]),
                       ("host-after-links", ["ip", "-j", "link", "show"]),
                       ("host-after-namespaces", ["ip", "netns", "list"])]:
    run(label, command)
print("native build and single formal attempt finished", flush=True)
