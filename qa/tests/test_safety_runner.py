from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from qa.archive import stage
from qa.common import EvidenceError, ROOT, file_hash, read_json, write_json
from qa.plan import make_plan
from qa.run import (_corpus_snapshot, _functional_evidence, commands, finish_fuzz,
                    prepare_fuzz, run_case, run_child, safety_capability)


class SafetyRunnerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        shutil.copytree(ROOT / 'fuzz/corpus', self.root / 'fuzz/corpus')
        self.output = self.root / 'result'
        self.output.mkdir()
        self.case = {'runner': 'fuzz', 'toolchain': 'nightly-2026-07-11', 'profile': 'dev',
                     'platform': 'darwin-aarch64', 'config': {'target': 'wire_parser', 'seconds': 30}}
        self.capability = {'host': 'aarch64-apple-darwin', 'target': 'aarch64-apple-darwin', 'errors': []}
        write_json(self.output / 'safety-capability.json', self.capability)

    def prepare(self):
        return prepare_fuzz(self.case, self.root, self.output)

    def test_real_child_mutates_only_writable_copy_and_keeps_source(self):
        before = _corpus_snapshot(self.root)
        seeds = self.prepare()
        record = run_child([sys.executable, '-c',
            'import pathlib,sys; p=pathlib.Path(sys.argv[1]); (p/"empty").write_bytes(b"mutated"); '
            '(p/"new-input").write_bytes(b"generated"); print("Done 3 runs in 1 second(s)")', seeds['corpus_path']],
            self.root, self.output / 'command-0.log', 10, os.environ.copy())
        self.assertEqual(record['cleanup_status'], 'PASS')
        finish_fuzz(self.root, self.output, seeds)
        self.assertEqual(_corpus_snapshot(self.root), before)
        self.assertEqual(seeds['initial_seeds'][0]['sha256'], file_hash(self.root / 'fuzz/corpus/wire_parser/empty'))
        self.assertTrue(read_json(self.output / 'fuzz-artifacts.json')['source_unchanged'])

    def test_command_binds_copy_artifacts_native_target_and_budget(self):
        seeds = self.prepare()
        argv = commands(self.case, self.case['toolchain'], self.root, self.output)[0]
        self.assertIn(seeds['corpus_path'], argv)
        self.assertNotIn(str(self.root / 'fuzz/corpus/wire_parser'), argv)
        self.assertEqual(argv[argv.index('--target') + 1], self.capability['target'])
        for item in ('--dev', '-max_total_time=30', '-seed=1', '-artifact_prefix=' + seeds['artifact_path'] + os.sep):
            self.assertIn(item, argv)

    def test_changed_reviewed_seed_is_rejected(self):
        (self.root / 'fuzz/corpus/wire_parser/empty').write_bytes(b'changed')
        with self.assertRaisesRegex(EvidenceError, 'seed changed'):
            self.prepare()

    def test_source_mutation_fails_and_preserves_index(self):
        seeds = self.prepare()
        (self.root / 'fuzz/corpus/wire_parser/empty').write_bytes(b'changed')
        with self.assertRaisesRegex(EvidenceError, 'source fuzz corpus changed'):
            finish_fuzz(self.root, self.output, seeds)
        self.assertFalse(read_json(self.output / 'fuzz-artifacts.json')['source_unchanged'])

    def test_failure_index_archives_without_raw_crash_or_corpus(self):
        seeds = self.prepare()
        (self.output / 'fuzz-artifacts/crash-one').write_bytes(b'failure')
        finish_fuzz(self.root, self.output, seeds)
        index = read_json(self.output / 'fuzz-artifacts.json')
        self.assertEqual(index['artifacts'][0]['sha256'], file_hash(self.output / 'fuzz-artifacts/crash-one'))
        destination = self.root / 'upload'
        stage(self.output, destination)
        self.assertTrue((destination / 'fuzz-artifacts.json').is_file())
        self.assertFalse((destination / 'fuzz-artifacts').exists())
        self.assertFalse((destination / 'fuzz-corpus').exists())
        with self.assertRaises(FileExistsError):
            finish_fuzz(self.root, self.output, seeds)

    def test_independent_validator_requires_copy_identity_and_completed_runs(self):
        seeds = self.prepare()
        finish_fuzz(self.root, self.output, seeds)
        records = [{'status': 'completed', 'exit_code': 0,
                    'argv': commands(self.case, self.case['toolchain'], self.root, self.output)[0]}]
        (self.output / 'command-0.log').write_text('Done 3 runs in 1 second(s)\n')
        self.assertEqual(_functional_evidence(self.case, self.output, records)[0], 'PASS')
        # Upload staging changes the directory, but the recorded argv remains the actual original command.
        destination = self.root / 'upload'
        stage(self.output, destination)
        self.assertEqual(_functional_evidence(self.case, destination, records)[0], 'PASS')
        records[0]['argv'][-1] = '-artifact_prefix=/source/'
        self.assertEqual(_functional_evidence(self.case, self.output, records)[0], 'NOT_RUN')
        records[0]['exit_code'] = 77
        self.assertEqual(_functional_evidence(self.case, self.output, records)[0], 'FAIL')

    def test_exit_zero_without_fuzz_iterations_is_not_run(self):
        seeds = self.prepare()
        finish_fuzz(self.root, self.output, seeds)
        records = [{'status': 'completed', 'exit_code': 0,
                    'argv': commands(self.case, self.case['toolchain'], self.root, self.output)[0]}]
        for log in ('build successful', 'Done 0 runs in 1 second(s)'):
            with self.subTest(log=log):
                (self.output / 'command-0.log').write_text(log)
                self.assertEqual(_functional_evidence(self.case, self.output, records)[0], 'NOT_RUN')

    def test_missing_indexes_or_changed_source_cannot_pass_but_nonzero_retains_failure(self):
        seeds = self.prepare()
        finish_fuzz(self.root, self.output, seeds)
        records = [{'status': 'completed', 'exit_code': 0,
                    'argv': commands(self.case, self.case['toolchain'], self.root, self.output)[0]}]
        (self.output / 'command-0.log').write_text('Done 3 runs in 1 second(s)')
        failures = read_json(self.output / 'fuzz-artifacts.json')
        failures['source_unchanged'] = False
        write_json(self.output / 'fuzz-artifacts.json', failures)
        self.assertEqual(_functional_evidence(self.case, self.output, records)[0], 'NOT_RUN')
        for name in ('fuzz-seeds.json', 'fuzz-artifacts.json', 'safety-capability.json'):
            with self.subTest(name=name):
                path = self.output / name
                saved = path.read_bytes()
                path.unlink()
                with self.assertRaises(OSError):
                    _functional_evidence(self.case, self.output, records)
                records[0]['exit_code'] = 77
                self.assertEqual(_functional_evidence(self.case, self.output, records)[0], 'FAIL')
                records[0]['exit_code'] = 0
                path.write_bytes(saved)

    def probe(self, runner='fuzz', supported=('address',)):
        case = dict(self.case, runner=runner)
        values = ['rustc 1.99.0-nightly\nhost: aarch64-apple-darwin',
                  json.dumps({'supported-sanitizers': list(supported)}), 'cargo-fuzz 0.13.2']
        with patch('qa.run.command', side_effect=values), patch('qa.run.platform_id', return_value='darwin-aarch64'):
            return safety_capability(case, self.root, {})

    def test_darwin_native_asan_fuzz_and_explicit_leak_limitation(self):
        capability = self.probe()
        self.assertEqual(capability['target'], 'aarch64-apple-darwin')
        self.assertFalse(capability['errors'])
        self.assertIn('no LeakSanitizer guarantee', capability['scope'])
        self.assertIn('lacks LeakSanitizer', self.probe('sanitizer')['errors'][0])
        self.assertFalse(self.probe('sanitizer', ('address', 'leak'))['errors'])

    def test_unsupported_asan_is_capability_missing(self):
        self.assertIn('does not support AddressSanitizer', self.probe(supported=())['errors'][0])

    def test_unsupported_entrypoint_records_not_run_without_executing_tests(self):
        plan = make_plan(ROOT / 'qa/policy', 'safety', '1' * 40, platform='darwin-aarch64')
        case = next(case for case in plan['cases'] if case['runner'] == 'sanitizer')
        values = ['rustc\nhost: aarch64-apple-darwin', json.dumps({'supported-sanitizers': ['address']})]
        with patch('qa.run.command', side_effect=values), patch('qa.run.platform_id', return_value='darwin-aarch64'), \
                patch('qa.run.source_identity', return_value={'sha': '1' * 40}), \
                patch('qa.run.build_manifest', return_value={}), patch('qa.run.run_child') as child:
            result = run_case(plan, case, self.root, self.root / 'unsupported')
        child.assert_not_called()
        self.assertEqual(result['functional']['status'], 'NOT_RUN')
        self.assertEqual(result['execution']['status'], 'capability_missing')
        self.assertIn('lacks LeakSanitizer', result['validity']['reasons'][0])

    def test_inherited_flags_cannot_disable_sanitizer(self):
        env = {'RUSTFLAGS': '-Zsanitizer=none', 'CARGO_ENCODED_RUSTFLAGS': '-Zsanitizer=none'}
        case = dict(self.case, runner='sanitizer')
        with patch('qa.run.command', side_effect=['rustc\nhost: aarch64-apple-darwin',
                   json.dumps({'supported-sanitizers': ['address', 'leak']})]), patch('qa.run.platform_id', return_value='darwin-aarch64'):
            capability = safety_capability(case, self.root, env)
        self.assertFalse(capability['errors'])
        self.assertNotIn('CARGO_ENCODED_RUSTFLAGS', env)
        self.assertEqual(env['RUSTFLAGS'], '-Zsanitizer=address')

    def test_cross_compiler_host_and_inherited_target_are_rejected(self):
        with patch('qa.run.command', return_value='rustc\nhost: x86_64-unknown-linux-gnu'):
            with self.assertRaisesRegex(EvidenceError, 'host differs'):
                safety_capability(self.case, self.root, {})
        with patch('qa.run.command', return_value='rustc\nhost: aarch64-apple-darwin'), patch('qa.run.platform_id', return_value='darwin-aarch64'):
            with self.assertRaisesRegex(EvidenceError, 'cross target'):
                safety_capability(self.case, self.root, {'CARGO_BUILD_TARGET': 'x86_64-unknown-linux-gnu'})

    def test_sanitizer_command_uses_probed_native_target(self):
        case = dict(self.case, runner='sanitizer', config={'test_filter': 'buf::tests::'})
        argv = commands(case, case['toolchain'], self.root, self.output)[0]
        self.assertEqual(argv[argv.index('--target') + 1], 'aarch64-apple-darwin')


if __name__ == '__main__':
    unittest.main()
