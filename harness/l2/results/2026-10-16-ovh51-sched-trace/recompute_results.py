"""Offline checks of retained raw UDP samples, execution identity, and isolation decisions.

Full traces over 50 MB stay on the host as preregistered. This recomputes sample
metrics/statistics and validates recorded trace coverage; it does not claim to
reconstruct scheduler intervals from hashes.
"""
import hashlib
import json
from pathlib import Path
import isolation
from retained_evidence import evidence_root

HERE = Path(__file__).resolve().parent
E = evidence_root()
SHA = '85ac164a2b762a87f00ceac4f56727c5cbc91128'
WORKER = 'd921130672b975fb48f646e7ff159301ea5089d51a8fdd06b16cd0a08477a551'


def read(path):
    return json.loads(path.read_text())


def samples(path):
    flows = {}
    for p in path.rglob('udp-*.json'):
        flow = p.stem.split('-')[1]
        if flow.isdigit():
            assert int(flow) not in flows, p
            flows[int(flow)] = read(p)['samples']
    assert set(flows) == {0, 1, 2, 3}, (path, flows.keys())
    for flow, rows in flows.items():
        assert len(rows) == 3200, (path, flow, len(rows))
        assert [r['seq'] for r in rows] == list(range(3200)), (path, flow)
        assert all(isinstance(r.get('generator_lag_ms'), (int, float)) for r in rows)
    return flows


def check_arm(path):
    p, e = read(path/'plan.json'), read(path/'execution.json')
    assert p['tested_sha'] == SHA and p['worker_sha256'] == WORKER
    assert p['env_l2']['L2_MAX_GENERATOR_LAG_SECS'] == '0.02'
    assert e['record']['cleanup_status'] == 'PASS' and e['instance_removed']
    rows = [row for values in samples(path).values() for row in values]
    return {'max_ms': max(r['generator_lag_ms'] for r in rows),
            'misses': sum(r['generator_lag_ms'] > 20 for r in rows),
            'statuses': {status: sum(r['status'] == status for r in rows) for status in sorted({r['status'] for r in rows})},
            'baseline_exit_code': e['record']['exit_code'], 'cleanup': e['record']['cleanup_status']}


def main():
    index = read(HERE/'evidence-index.json')
    for row in index['files']:
        p = E/row['path']
        assert p.stat().st_size == row['size_bytes']
        assert hashlib.sha256(p.read_bytes()).hexdigest() == row['sha256'], p
    traces = {}
    for n in range(1, 4):
        p = E/f'trace-{n}'
        result = check_arm(p)
        a = read(p/'analysis.json')
        assert set(a['trace_drops']) == {f'cpu{i}' for i in range(8)}
        assert not any(v for r in a['trace_drops'].values() for v in r.values())
        assert a['valid'] and not a['invalid_reasons']
        assert set(a['generator_pids']) == {'0', '1', '2', '3'}
        assert len(set(a['generator_pids'].values())) == 4
        assert all(r['uncovered_ns'] == 0 for r in a['late_samples'])
        assert sum(r['miss'] for r in a['late_samples']) == result['misses']
        result['irq130_supporting_misses'] = sum(r['miss'] and r['class'] == 'IRQ/softirq on generator CPU'
            and r['generator_on_core0'] and (r['core0_irq130_ms'] > 0 or r['core0_net_rx_ms'] > 0) for r in a['late_samples'])
        result['miss_class_counts'] = a['miss_class_counts']
        traces[p.name] = result
    I = E/'isolation'; plan = read(I/'plan.json'); execution = read(I/'execution.json')
    assert plan['arms'] == ['A', 'B', 'C']
    assert [tuple(v) for v in plan['order']] == isolation.schedule(tuple(plan['arms']))
    assert len(execution) == 15
    metrics = {a: [] for a in plan['arms']}; controls = {}
    for record, (round_number, arm) in zip(execution, plan['order']):
        label = f'r{round_number}-{arm}'
        assert record['label'] == label and record['returncode'] == 0
        p = I/label; controls[label] = check_arm(p)
        arm_plan = read(p/'plan.json')
        assert arm_plan['cpus'] == isolation.CPUS[arm] and not arm_plan['trace']
        m = isolation.run_metric(p)
        assert m == record['metric']; metrics[arm].append(m)
        if arm == 'C':
            assert not record['restore_errors'] and record['irq_restored'] == plan['nic_irqs']
            assert all(isolation.cpu_set(v['effective_affinity_list']) <= {0, 4} for v in record['irq_during'].values())
        else:
            snapshot = read(p/'host-before.json')['irq_affinity']
            assert {irq: snapshot[irq] for irq in plan['nic_irqs']} == plan['nic_irqs']
    decision = isolation.decide(metrics, tuple(plan['arms']))
    assert decision == read(I/'summary.json')
    final = read(E/'host-final.json')
    assert not final['lease_entries'] and not final['trace_instances'] and not final['namespaces']['stdout'].strip()
    assert {irq: final['irq_affinity'][irq] for irq in plan['nic_irqs']} == plan['nic_irqs']
    report = {'artifact_hashes_verified': len(index['files']), 'traces': traces, 'controls': controls,
              'isolation_decision': decision, 'irq_restoration_verified': True,
              'formal': 'unchanged NOT_RUN / UNKNOWN; no new formal invocation', 'known_blocked': True}
    (HERE/'independent-recomputed.json').write_text(json.dumps(report, indent=2)+'\n')
    print(json.dumps(report, indent=2))


if __name__ == '__main__':
    main()
