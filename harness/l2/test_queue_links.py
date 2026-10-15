"""Queue-link evidence cannot substitute qdisc zero drops for link health."""
import copy
import gzip
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from queue_links import ROLES, check_nodes, snapshot, validate


def link_fixture():
    report = {"schema_version": 1, "clock": "CLOCK_MONOTONIC"}
    suffixes = {"worker": "worker", "target": "target", "client0": "client", "client1": "fwd1", "client2": "fwd2", "client3": "fwd3"}
    for phase, at in (("before", 500_000_000), ("after", 34_000_000_000)):
        nodes = {}
        for index, role in enumerate(sorted(ROLES)):
            links = []
            for ordinal, name in enumerate(("lo", "eth0", "wg0") if role.startswith("client") else ("lo", "eth0")):
                stats = {direction: {"bytes": 100, "packets": 1, "dropped": 4, "errors": 2, "over_errors": 0}
                         for direction in ("rx", "tx")}
                links.append({"ifindex": ordinal + 1, "ifname": name, "link_type": "none" if name == "wg0" else "ether",
                              **({} if name == "wg0" else {"address": f"02:00:00:00:{index:02x}:{ordinal:02x}"}), "stats64": stats})
            nodes[role] = {"namespace": "rig-abcdef-" + suffixes[role], "namespace_inode": index + 100,
                           "begin_ns": at + 1, "end_ns": at + 2, "links": links}
        report[phase] = {"schema_version": 1, "clock": "CLOCK_MONOTONIC", "begin_ns": at, "end_ns": at + 10,
                         "nodes": nodes, "errors": []}
    return report


class QueueLinkTests(unittest.TestCase):
    def test_zero_deltas_with_existing_nonzero_counters_are_valid(self):
        self.assertEqual(validate(link_fixture(), before_ns=1_000_000_000, after_ns=33_000_000_000), [])

    def test_missing_malformed_scope_interface_and_resets_are_not_zero(self):
        changes = [lambda r: r.update(schema_version=True), lambda r: r['after'].update(schema_version=True),
                   lambda r: r.pop("after"), lambda r: r['after']['nodes'].pop('target'),
                   lambda r: r['after']['nodes']['worker'].update(namespace='host'),
                   lambda r: r['after']['nodes']['worker'].update(namespace='rig-123456-worker'),
                   lambda r: r['after']['nodes']['worker'].update(namespace_inode=101),
                   lambda r: r['after']['nodes']['worker'].update(namespace_inode=999),
                   lambda r: r['after']['nodes']['worker']['links'].pop(),
                   lambda r: r['after']['nodes']['worker']['links'][0].update(ifindex=99),
                   lambda r: r['after']['nodes']['worker']['links'][0]['stats64']['rx'].pop('dropped'),
                   lambda r: r['after']['nodes']['worker']['links'][0]['stats64']['rx'].update(dropped=False),
                   lambda r: r['after']['nodes']['worker']['links'][0]['stats64']['rx'].update(bytes=0),
                   lambda r: r['after'].update(errors=['ip failed']), lambda r: r['after'].update(begin_ns=1),
                   lambda r: r.update(ok=True)]
        for index, change in enumerate(changes):
            with self.subTest(index=index):
                report = link_fixture(); change(report)
                self.assertTrue(validate(report))
        self.assertTrue(validate(link_fixture(), before_ns=1, after_ns=35_000_000_000))
        with self.assertRaises(ValueError):
            check_nodes({'worker':'rig-abcdef-worker'})

    def test_every_endpoint_direction_drop_or_error_delta_invalidates(self):
        for role in ROLES:
            for direction in ('rx','tx'):
                for field in ('dropped','errors','over_errors'):
                    with self.subTest(role=role,direction=direction,field=field):
                        report = link_fixture()
                        row = next(row for row in report['after']['nodes'][role]['links'] if row['ifname']=='eth0')
                        row['stats64'][direction][field] += 1
                        self.assertIn('increased by 1',str(validate(report)))

    def test_sampler_is_owned_bounded_and_retains_command_failure(self):
        nodes = {role: row['namespace'] for role,row in link_fixture()['before']['nodes'].items()}
        with patch('isolation.verify',return_value=[]), patch('queue_links.os.stat') as stat, \
                patch('queue_links.subprocess.run') as run:
            stat.return_value.st_ino=99
            run.return_value.returncode=1; run.return_value.stdout=b'';run.return_value.stderr=b'failed'
            observed=snapshot(nodes)
        self.assertEqual(len(observed['errors']),6)
        for call in run.call_args_list:
            self.assertEqual(call.kwargs['timeout'],3)
            self.assertEqual(call.args[0][-6:],['ip','-s','-s','-j','link','show'])
        with patch('isolation.verify',return_value=['not isolated']):
            with self.assertRaises(ValueError): snapshot(nodes)

    def test_queue_raw_closure_scope_failure_and_payload_failure_are_independent(self):
        from test_evidence import fixture, identity
        from export_case import export_case
        from test_path_queue import QUEUE, queue_report
        from evidence import inspect_case, load_case
        from qa.integration import freeze_l2_raw
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory); rig=root/'rig';rig.mkdir()
            result,raw=fixture(rig)
            result['effective_config']['queue_control']=copy.deepcopy(QUEUE)
            result['queue_control']=queue_report()
            links=result['queue_control']['link_samples']
            raw['queue-link-samples.json']=copy.deepcopy(links)
            (rig/'queue-link-samples.json').write_text(json.dumps(links))
            # Unsealed actual-source file is imported, then retained in the
            # same allowlisted raw envelope; no independent archive allowlist.
            (rig/'raw.json.gz').unlink()
            load=rig/'load';load.mkdir()
            for name,value in raw.items():
                if name=='queue-link-samples.json':continue
                if name.endswith('.json'): (load/name).write_text(json.dumps(value))
                else: (rig/name).write_text(value)
            (rig/'result.json').write_text(json.dumps(result))
            freeze_l2_raw(root)
            (rig/'queue-link-samples.json').unlink()
            self.assertEqual(load_case(rig)[1]['queue-link-samples.json'],links)
            self.assertFalse(inspect_case(rig)['functional_evidence_errors'])
            # Keep qdisc drops zero while an actual link counter increases.
            raw=load_case(rig)[1]
            raw['queue-link-samples.json']['after']['nodes']['worker']['links'][1]['stats64']['tx']['dropped']+=1
            result['queue_control']['link_samples']=copy.deepcopy(raw['queue-link-samples.json'])
            def save():
                (rig/'result.json').write_text(json.dumps(result))
                (rig/'raw.json.gz').write_bytes(gzip.compress(json.dumps(raw).encode()))
            save(); observed=inspect_case(rig)
            self.assertFalse(observed['functional_errors'])
            self.assertIn('increased by 1',str(observed['functional_evidence_errors']))
            raw['client-1.json']['errors'].append('corrupt payload');save()
            self.assertIn('corrupt payload',inspect_case(rig)['functional_errors'])
            raw.pop('queue-link-samples.json');save()
            self.assertIn('raw queue link samples differ',str(inspect_case(rig)['functional_evidence_errors']))
            self.assertEqual(export_case(rig,identity(result['effective_config']))['functional']['status'],'FAIL')
            for malformed in (None, [], "not-a-report"):
                result['queue_control']=malformed;save()
                self.assertIn('invalid typed queue control',str(inspect_case(rig)['functional_evidence_errors']))
                self.assertEqual(export_case(rig,identity(result['effective_config']))['functional']['status'],'FAIL')


if __name__=='__main__':unittest.main()
