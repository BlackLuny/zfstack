"""Offline diagnosis only. Never regrade the failed build or invoke Cargo."""
import json
from pathlib import Path
P=Path(__file__).resolve().parent
D=P/'collected/build/head-head'
manifest=json.loads((D/'worker-manifest.json').read_text())
commands=manifest['commands']
metadata=next(row for row in commands if 'metadata' in row['argv'])
raw=(D/metadata['log']).read_text()
try:
    json.loads(raw)
except json.JSONDecodeError as error:
    parse_error=str(error)
else:
    raise AssertionError('retained failure does not reproduce')
# Diagnostic extraction proves the payload exists, not that ignoring stderr is
# an acceptable production parser. No manifest, log or gate verdict is changed.
lines=raw.splitlines()
payloads=[(n,line) for n,line in enumerate(lines,1) if line.startswith('{"packages":')]
assert len(payloads)==1
line_number,payload=payloads[0]
value=json.loads(payload)
package=[r for r in value['packages'] if r['name']=='zfstack']
assert len(package)==1 and package[0]['source'] is None
assert package[0]['manifest_path'].endswith('/build/head-head/repos/zfstack/Cargo.toml')
result={'build_verdict_unchanged':manifest['status'],'build_errors':manifest['errors'],
        'cargo_metadata_exit_code':metadata['exit_code'],'cargo_metadata_execution_status':metadata['status'],
        'cargo_metadata_cleanup':metadata['cleanup'],'combined_log_parse_error':parse_error,
        'diagnostic_json_line':line_number,'diagnostic_json_packages':len(value['packages']),
        'non_json_prefix_lines':line_number-1,'prefix_excerpt':lines[:5],
        'frozen_local_zfstack_resolves':True,
        'worker_compile_invoked':any('build' in c['argv'] for c in commands),
        'formal_invocations':0,
        'cause':'qa.process merges stderr into stdout; qa.crossrepo parses the entire cargo metadata log as JSON. Cold Cargo cache emitted download progress on stderr although metadata succeeded.',
        'fix_direction':'Capture structured-command stdout and stderr separately; retain both and enforce combined output budgets/containment. Parse stdout only; add cold-cache progress and warning regression coverage. Do not strip lines or suppress diagnostics to regrade this failure.'}
(P/'build-diagnosis.json').write_text(json.dumps(result,indent=2)+'\n')
print(json.dumps(result,indent=2))
