"""Validate the final fixed-N bulk confirmation and retain rejected diagnostics."""
from pathlib import Path
import hashlib
import json
import subprocess
import sys

r = Path(__file__).resolve().parent
original = json.loads((r / 'network-meta.json').read_text())
plan = json.loads((r / 'network-unrestricted-plan.json').read_text())
proof = json.loads((r / 'bulk-final-proof.json').read_text())
assert proof['binaries'] == original['binaries']
assert proof['source_sha'] == plan['source_sha'] and proof['baseline_sha'] == plan['baseline_sha']
assert proof['host_tun_exists'] is False and proof['host_test_netns_exists'] is False
assert plan['limits'] == {'cpu_upper_pct': 2, 'goodput_lower_pct': -5}
assert plan['source_sha'] == '2694c1406b7ebb5d1eef381a0c1a69e27f1ea743'
assert plan['baseline_sha'] == '88a2e3f7a2e86fb6948223054a377e8a7f831af1'
assert (plan['secs'], plan['warmup'], plan['window_secs']) == (30, 5, 25)
cohorts = {}
checks = []
for tag, n in [('network-unrestricted-aa', 12), ('network-unrestricted-tu', 64)]:
    assert plan['cohorts'][tag]['pairs_per_direction'] == n
    assert hashlib.sha256((r / (tag + '.py')).read_bytes()).hexdigest() == plan['runner_sha256'][tag]
    meta = json.loads((r / (tag + '-meta.json')).read_text())
    assert meta['finished'] > meta['started'] >= plan['registered_at']
    assert proof['at'] > meta['finished']
    assert proof['complete'][tag] == {'runs': (n + 1) * 4, 'started': meta['started'], 'finished': meta['finished']}
    assert meta['affinity'] == '0-3' and 'thread_placement' not in meta
    assert meta['common'] == original['common'] and meta['mtu'] == original['mtu'] == 1420
    assert [(case, count) for case, count, _ in meta['cases']] == [('down8', n), ('up8', n)]
    for case, _, args in meta['cases']:
        wanted = next(list(a) for c, _, a in original['cases'] if c == case)
        wanted[wanted.index('--secs') + 1] = '30'
        wanted[wanted.index('--warmup') + 1] = '5'
        assert args == wanted
    for label in ['T', 'U']:
        source = 'T' if tag.endswith('-aa') else label
        assert meta['binaries'][label] == original['binaries'][source]
    # Checks full pairs, exact commands, success/patterns, namespaces, real
    # TUN/qdisc/softnet drops, and the frozen paired bootstrap estimator.
    subprocess.run([sys.executable, str(r / 'network_current_quota_analyze.py'), tag], check=True, stdout=subprocess.DEVNULL)
    cohort = json.loads((r / (tag + '-summary.json')).read_text())
    cohorts[tag] = cohort
    if tag.endswith('-aa'):
        continue  # A/A is a noise diagnostic, never a correction of T/U ratios.
    for case in ['down8', 'up8']:
        for field, limit in [('cpu.cpu_sec_per_effective_GB', 2), ('results.goodput_mbps_window', -5)]:
            result = cohort[case][field]
            assert result['n'] == n
            lo, hi = result['ci95_pct']
            if field.startswith('cpu.'):
                status = 'PASS' if hi <= limit else 'REGRESSION' if lo > limit else 'INCONCLUSIVE'
                kind = 'network_cpu_guard'
            else:
                status = 'PASS' if lo >= limit else 'REGRESSION' if hi < limit else 'INCONCLUSIVE'
                kind = 'throughput_guard'
            checks.append({'name': tag + '/' + case + '/' + field, 'kind': kind, 'limit_pct': limit, 'median_pct': result['median_pct'], 'ci95_pct': [lo, hi], 'status': status})

# The per-role placement experiment failed its own A/A precision gate. Keep
# that failure and its cancelled T/U prefix; never turn the prefix into a score.
pin_plan = json.loads((r / 'network-placement-plan.json').read_text())
pin_meta = json.loads((r / 'network-placement-aa-meta.json').read_text())
assert pin_meta['finished'] > pin_meta['started'] >= pin_plan['registered_at']
placement = {'zfb-stack': 0, 'zfb-link-down': 1, 'zfb-link-up': 2, 'others': 3}
assert pin_meta['thread_placement'] == pin_plan['placement'] == placement
for line in (r / 'network-placement-aa.jsonl').read_text().splitlines():
    audit = json.loads(line)['placement']
    assert not audit.get('error')
    assert all(t['cpu'] == placement.get(t['name'], 3) for t in audit['threads'])
    for name, cpu in placement.items():
        if name != 'others':
            assert any(t['name'] == name and t['cpu'] == cpu and t['first_sec'] < 2 for t in audit['threads'])
for label in ['T', 'U']:
    assert pin_meta['binaries'][label] == original['binaries']['T']
for tag in ['network-placement-aa', 'network-placement-tu']:
    assert hashlib.sha256((r / (tag + '.py')).read_bytes()).hexdigest() == pin_plan['runner_sha256'][tag]
subprocess.run([sys.executable, str(r / 'network_current_quota_analyze.py'), 'network-placement-aa'], check=True, stdout=subprocess.DEVNULL)
pin_summary = json.loads((r / 'network-placement-aa-summary.json').read_text())
pin_gate = all(-limit <= entry[field]['ci95_pct'][0] <= entry[field]['ci95_pct'][1] <= limit
               for entry in pin_summary.values()
               for field, limit in [('cpu.cpu_sec_per_effective_GB', 2), ('results.goodput_mbps_window', 5)])
assert not pin_gate, 'Do not silently reinterpret the rejected placement experiment'
cancel = json.loads((r / 'network-placement-cancelled.json').read_text())
partial_meta = json.loads((r / 'network-placement-tu-meta.json').read_text())
partial = (r / 'network-placement-tu.jsonl').read_text().splitlines()
assert 'finished' not in partial_meta and pin_meta['finished'] < cancel['at'] < plan['registered_at']
assert len(partial) < sum((n + 1) * 2 for _, n, _ in partial_meta['cases'])
result = {'plan': plan, 'cohorts': cohorts, 'checks': checks,
          'rejected_placement': {'aa': pin_summary, 'precision_gate': 'FAIL', 'tu_status': 'CANCELLED_NO_INFERENCE', 'partial_lines': len(partial), 'cancellation': cancel},
          'performance_verdict': 'PASS' if all(c['status'] == 'PASS' for c in checks) else 'REGRESSION' if any(c['status'] == 'REGRESSION' for c in checks) else 'INCONCLUSIVE',
          'scope': 'Original MTU1420 unlimited 8-flow socket/TUN protocol, CPU0-3; fixed 64 pairs per direction; no sample pooling or A/A bias correction.'}
(r / 'bulk-final-summary.json').write_text(json.dumps(result, indent=2) + '\n')
print(json.dumps({'checks': checks, 'performance_verdict': result['performance_verdict']}, indent=2))
