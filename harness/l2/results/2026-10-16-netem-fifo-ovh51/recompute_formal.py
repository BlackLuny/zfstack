"""Independently verify retained C16 evidence, including an aborted baseline."""
import gzip
import json
import sys
from pathlib import Path

P = Path(__file__).resolve().parent
ROOT = P.parents[3]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(P.parents[1]))
import netem_fifo_ledger as ledger
from qa.common import read_json, write_json, EvidenceError
from qa.evaluate import evaluate, verify_artifacts, verify_execution_evidence
from qa.build_manifest import verify_manifest
from qa.integration import queue_negative_evidence, verify_integration

plan = read_json(P / 'collected/evidence/plan.json')
paths = [p for p in (P / 'formal-artifacts').rglob('result.json')
         if read_json(p).get('case_id') == 'C16-l2-queue-control']
assert len(paths) == 1
directory = paths[0].parent
result = read_json(paths[0])
case = next(c for c in plan['cases'] if c['case_id'] == 'C16-l2-queue-control')
worker = read_json(directory / 'worker-manifest.json')
status, details, observations = queue_negative_evidence(case, directory / 'baseline', directory / 'fault', worker)
assert status == result['functional']['status']
checks = {}
for label, action in (
    ('artifact_manifest', lambda: verify_artifacts(result, directory)),
    ('build_identity', lambda: verify_manifest(verify_artifacts(result, directory), case, plan, read_json(directory / 'environment.json'))),
    ('command_identity', lambda: verify_execution_evidence(result, case, directory, ROOT / 'qa/policy')),
    ('integration', lambda: verify_integration(result, case, directory)),
):
    try:
        action()
        checks[label] = {'ok': True}
    except EvidenceError as error:
        checks[label] = {'ok': False, 'error': str(error)}
evaluation = evaluate(plan, paths, ROOT / 'qa/policy', expected_sha=plan['tested_sha'])
write_json(P / 'local-evaluation.json', evaluation)
value = {'tested_sha': plan['tested_sha'], 'case_version': case['case_version'],
         'functional': status, 'functional_validity': result['validity']['functional'],
         'aggregate': evaluation['status'], 'details': details, 'verification': checks,
         'execution': result['execution'], 'worker_sha256': worker['binary_sha256'],
         'arms': {}, 'queue_ledger_present': False}
for arm in ('baseline', 'fault'):
    rig = directory / arm / 'rig'
    if not (rig / 'raw.json.gz').is_file():
        value['arms'][arm] = {'raw_workload_present': False}
        continue
    raw = json.loads(gzip.decompress((rig / 'raw.json.gz').read_bytes()))
    udp = {}
    for index in range(4):
        data = raw[f'udp-{index}.json']
        udp[str(index)] = {'counters': data['counters'],
                          'max_generator_lag_ms': max(s.get('generator_lag_ms', 0) for s in data['samples']),
                          'non_completed': [s for s in data['samples'] if s['status'] != 'completed']}
    info = observations.get(arm, {})
    value['arms'][arm] = {'raw_workload_present': True, 'udp': udp,
                         'service_status': info.get('service_result', {}).get('status'),
                         'raw_functional_status': info.get('raw_functional_status'),
                         'performance_errors': info.get('performance_errors'),
                         'queue_control': info.get('queue_control')}
    if (rig / 'queue-ledger.bin.gz').is_file():
        report = read_json(rig / 'queue-control.json')
        blob = (rig / 'queue-ledger.bin.gz').read_bytes()
        ins, outs = ledger.decode(blob)
        pairs, lost, extra = ledger.match(ins, outs)
        value['queue_ledger_present'] = True
        value['arms'][arm]['queue'] = {'errors': ledger.validate(report, case['config']['queue_control'], blob),
                                      'in_frames': len(ins), 'out_frames': len(outs),
                                      'lost': len(lost), 'extra': len(extra)}
write_json(P / 'formal-recomputed.json', value)
print(json.dumps({'functional': status, 'validity': value['functional_validity'],
                  'aggregate': evaluation['status'], 'verification': checks,
                  'fault_present': value['arms']['fault']['raw_workload_present']}, indent=2))
