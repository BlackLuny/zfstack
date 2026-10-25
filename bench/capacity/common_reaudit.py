#!/usr/bin/env python3
"""Independent actual-mask audit; retain the frozen strict pipeline unchanged."""
import argparse
import copy
import json
from pathlib import Path
import re

import common_cases as rig
import common_collect as collector
import common_sign
from cubic50_env import ROLES
from run import ROOT, save


def rr_transition_error(error, row, observer):
    match = re.fullmatch(r'actual mask differs (\d+) (.+)', error)
    if not match or row['kind'] != 'native' or row['placement'] != 'fixed' or not row.get('lights'):
        return False
    tid, name = match.groups()
    thread = observer['threads'].get(tid)
    if not thread or name in ROLES or thread['name'] != name or thread['last']['affinity'] != [4]:
        return False
    states = [(thread['first_seen_sec'], thread['first']['affinity'])]
    states += [(s['elapsed_sec'], t['affinity']) for s in observer['samples'] for t in s['threads'] if str(t['tid']) == tid]
    states.append((thread['last_seen_sec'], thread['last']['affinity']))
    masks = [mask for _, mask in sorted(states, key=lambda item:item[0])]
    if any(mask not in [[3], [4]] for mask in masks):
        return False
    reached_four = False
    for mask in masks:
        if reached_four and mask != [4]:
            return False
        reached_four |= mask == [4]
    return reached_four


def independent_masks(a, row):
    errors = []
    required = set(ROLES) if row['kind'] == 'native' else {'zfc-driver', 'zfc-tun-rx'}
    if a['observer_affinity'] != [4] or set(a['required_roles']) != required:
        errors.append('observer/role identity differs')
    if a['placement'] != row['placement'] or a['kind'] != row['kind'] or a['interactive'] != bool(row.get('lights')):
        errors.append('observer mode differs')
    seen = {role:set() for role in required}
    for sample in a['samples']:
        if sample['elapsed_sec'] <= 2:
            errors.append('sample before assignment deadline')
        for t in sample['threads']:
            if row['placement'] == 'original':
                allowed = [[0, 1, 2, 3]] + ([[4]] if row.get('lights') else [])
            elif t['name'] in ROLES:
                allowed = [[ROLES[t['name']]]]
            else:
                allowed = [[3]] + ([[4]] if row.get('lights') else [])
            if t['affinity'] not in allowed:
                errors.append('independent sampled mask differs')
            if t['name'] in seen:
                seen[t['name']].add(t['tid'])
    if not a['samples'] or any(v['first_seen_sec'] > 2 for v in a['required_roles'].values()):
        errors.append('missing samples or late required role')
    for role, tids in seen.items():
        if role not in a['required_roles'] or tids != {a['required_roles'][role]['tid']}:
            errors.append('missing/ambiguous role: '+role)
    if row['kind'] == 'native' and row['placement'] == 'fixed' and 'zfb-stack' in a['required_roles']:
        tid = a['required_roles']['zfb-stack']['tid']
        states = [t for s in a['samples'] for t in s['threads'] if t['tid'] == tid]
        if len(states) < 2 or states[-1]['scheduler_counts'].get('se.nr_migrations', -1)-states[0]['scheduler_counts'].get('se.nr_migrations', -2) != 0:
            errors.append('driver migration count invalid')
    return dict(valid=not errors, errors=sorted(set(errors)))


def certify(out, row, container):
    dest = out/rig.name(row)
    original = rig.inspect(out, row, container)
    a = json.loads((dest/'environment.json').read_text())
    raw = json.loads((dest/'stdout.json').read_text())
    end = json.loads((dest/'end.json').read_text())
    masks = independent_masks(a, row)
    rr_valid = True
    if row.get('lights'):
        streams = json.loads((dest/'arrivals.json').read_text())
        rr_valid = len(streams) == 6 and all(s.get(k) == 4 for s in streams for k in ['requested_cpu', 'observer_cpu', 'sender_cpu'])
    race = (bool(a['errors']) and a['returncode'] == 0 and end['returncode'] == 1 and raw['ok'] and not raw['errors']
        and original['errors'] == ['process or identity failed'] and original['audit_errors'] == sorted(set(a['errors']))
        and masks['valid'] and rr_valid and all(rr_transition_error(e, row, a) for e in a['errors']))
    return dict(name=rig.name(row), original_outer_returncode=end['returncode'], child_returncode=a['returncode'],
        original_strict_valid=original['strict_valid'], original_observation_valid=original['observation_valid'],
        original_errors=original['errors'], original_audit_errors=original['audit_errors'],
        independent_masks=masks, rr_ledgers_valid=rr_valid, rr_self_assignment_race_certified=bool(race),
        independently_observed_valid=masks['valid'] and rr_valid and (original['observation_valid'] or race),
        independently_service_valid=masks['valid'] and rr_valid and (original['strict_valid'] or race))


def reaudit(directory, destination):
    if not (rig.PREP/'done.json').exists():
        raise RuntimeError('finish every measurement before comparative analysis')
    destination.mkdir(parents=True, exist_ok=False)
    certificates, normalized = {}, {}
    for phase in ['preflight', 'aa', 'ordinary', 'peak']:
        original = json.loads((directory/(phase+'-summary.json')).read_text())
        result = copy.deepcopy(original)
        out = ROOT/'qa/results'/('common-20261025-'+phase)
        plan = json.loads((out/'plan.json').read_text())
        by_name = {rig.name(r):r for r in plan['rows']}
        certs = [certify(out, by_name[r['name']], plan['container']) for r in original['rows']]
        for row, cert in zip(result['rows'], certs):
            row['original_strict_valid'] = row['strict_valid']
            row['original_observation_valid'] = row['observation_valid']
            # Compatibility projection for the unchanged decision functions.
            # Export the mapping, original flags and certificates explicitly.
            row['strict_valid'] = cert['independently_service_valid']
            row['observation_valid'] = cert['independently_observed_valid']
        result['strict_valid'] = sum(r['strict_valid'] for r in result['rows'])
        result['observed_valid'] = sum(r['observation_valid'] for r in result['rows'])
        result['validity_projection_note'] = 'In this separate view only, strict_valid projects independently_service_valid; original flags and every certificate are retained. The frozen original files are unchanged.'
        certificates[phase] = certs
        normalized[phase] = result
        save(destination/(phase+'-summary.json'), result)
    ordinary = collector.comparison(normalized['ordinary'])
    peak = collector.comparison(normalized['peak'], peak=True)
    save(destination/'ordinary-comparisons.json', ordinary)
    save(destination/'peak-comparisons.json', peak)
    numerical_equal = {}
    for name, current in [('ordinary-comparisons', ordinary), ('peak-comparisons', peak)]:
        previous = json.loads((directory/(name+'.json')).read_text())
        numerical_equal[name] = (previous['aggregates'] == current['aggregates'] and previous['statistics'] == current['statistics'] and
            all(previous['cells'][k]['changes'] == v['changes'] for k, v in current['cells'].items()))
    if not all(numerical_equal.values()):
        raise RuntimeError('independent validity must not change numerical estimates')
    original_verdict = json.loads((directory/'verdict.json').read_text())
    revised = collector.decisions(normalized['ordinary'], ordinary)
    save(destination/'majority-median.json', common_sign.analyze(destination))
    report = dict(original_primary_qualification=original_verdict['representative_scope_qualified'],
        independently_audited_primary_qualification=revised['representative_scope_qualified'],
        unchanged_numerical_estimates=numerical_equal, qualification_after_independent_audit=revised,
        original_strict_counts={p:sum(c['original_strict_valid'] for c in cs) for p, cs in certificates.items()},
        independently_observed_counts={p:sum(c['independently_observed_valid'] for c in cs) for p, cs in certificates.items()},
        independently_service_counts={p:sum(c['independently_service_valid'] for c in cs) for p, cs in certificates.items()},
        certified_races=[c for cs in certificates.values() for c in cs if c['rr_self_assignment_race_certified']],
        certificates=certificates, original_flags_overwritten=False,
        scope='Second read-only actual-mask audit, unchanged workloads/pairs/estimators/margins. Original automatic gate is retained separately; no retry or omission.')
    save(destination/'independent-audit.json', report)
    return {k:v for k, v in report.items() if k not in ['certificates', 'qualification_after_independent_audit']}


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('original_directory', type=Path)
    p.add_argument('destination', type=Path)
    a = p.parse_args()
    print(json.dumps(reaudit(a.original_directory.resolve(), a.destination.resolve()), indent=2))
