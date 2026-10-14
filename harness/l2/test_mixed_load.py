import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
import unittest

HERE = Path(__file__).resolve().parent


class MixedLoadTest(unittest.TestCase):
    def test_churn_and_pipelined_udp_validate_replies_and_unique_sequences(self):
        with socket.socket() as s:
            s.bind(('127.0.0.1', 0))
            port = s.getsockname()[1]
        server = subprocess.Popen([sys.executable, str(HERE / 'load.py'), 'serve', '--host', '127.0.0.1', '--port', str(port)], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        children = []
        try:
            self.assertEqual(server.stdout.readline().strip(), 'ready')
            with tempfile.TemporaryDirectory() as directory:
                common = ['--host', '127.0.0.1', '--port', str(port), '--flow', '1', '--start', str(time.monotonic() + .2), '--baseline', '.2', '--loaded', '.3', '--recovery', '.2', '--request-timeout', '1']
                for script, mode in [('mixed_load.py', ['light']), ('udp_probe.py', [])]:
                    out = Path(directory) / (script + '.json')
                    child = subprocess.Popen([sys.executable, str(HERE / script), *mode, *common, '--out', str(out)], env={**os.environ, 'L2_LIGHT_CONNS': '2', 'L2_CHURN_REQUESTS': '2'}, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                    children.append(child)
                for child in children:
                    stdout, stderr = child.communicate(timeout=5)
                    self.assertEqual(child.returncode, 0, (stdout, stderr))
                mixed = json.loads((Path(directory) / 'mixed_load.py.json').read_text())
                self.assertFalse(mixed['errors'])
                self.assertEqual(len(mixed['connections']), 2)
                self.assertTrue(all(len(row['connects']) > 1 for row in mixed['connections']))
                sequences = [s['seq'] for s in mixed['samples']]
                self.assertEqual(len(sequences), len(set(sequences)))
                self.assertEqual({s['phase'] for s in mixed['samples']}, {'baseline', 'loaded', 'recovery'})
                udp = json.loads((Path(directory) / 'udp_probe.py.json').read_text())
                self.assertFalse(udp['errors'])
                self.assertEqual(udp['sent'], udp['received'])
                self.assertGreater(udp['received'], 10)
        finally:
            for child in children:
                if child.poll() is None: child.kill()
                child.communicate()
            server.terminate()
            server.communicate(timeout=5)


if __name__ == '__main__':
    unittest.main()
