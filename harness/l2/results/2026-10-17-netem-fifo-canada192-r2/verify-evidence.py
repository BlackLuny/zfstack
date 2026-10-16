"""Extract and verify retained evidence; run only offline analyses, no traffic."""
import hashlib,json,runpy,sys,tarfile
from pathlib import Path
P=Path(__file__).resolve().parent
archive=P/'collected-artifacts.tar.gz'
assert hashlib.sha256(archive.read_bytes()).hexdigest()=='4a6388ca8cb3980215b3019a02aa88d5cf07daa7cd533e3c7276137f653cba6e'
index=json.loads((P/'collected-artifacts.index.json').read_text())['files']
with tarfile.open(archive) as tar:
    assert {m.name for m in tar.getmembers()}=={r['path'] for r in index}
    for member in tar.getmembers():
        assert member.isfile() and not Path(member.name).is_absolute() and '..' not in Path(member.name).parts
        content=tar.extractfile(member).read()
        expected=next(r for r in index if r['path']==member.name)
        assert len(content)==expected['size_bytes'] and hashlib.sha256(content).hexdigest()==expected['sha256']
        target=P/'collected'/member.name
        target.parent.mkdir(parents=True,exist_ok=True)
        target.write_bytes(content)
sys.argv=[str(P/'recompute-gate.py')]
runpy.run_path(str(P/'recompute-gate.py'),run_name='__main__')
runpy.run_path(str(P/'analyze-order.py'),run_name='__main__')
state=json.loads((P/'collected/service-control.json').read_text())
for unit,original in state['original'].items():
    for key in ['ActiveState','UnitFileState']:
        assert state['restored'][unit][key]==original[key]
print(f'Verified {len(index)} archived files, gate and order diagnosis, and service restoration.')
