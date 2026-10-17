"""Extract and verify retained evidence; run only offline analyses, no traffic."""
import hashlib,json,runpy,sys,tarfile
from pathlib import Path
P=Path(__file__).resolve().parent
archive=P/'collected-artifacts.tar.gz'
assert hashlib.sha256(archive.read_bytes()).hexdigest()=='6e7d404d77cc00ed5a5aee754ee4eb14fb81cf8d74d6b5a89d671fd50fabd9eb'
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
runpy.run_path(str(P/'analyze-build.py'),run_name='__main__')
state=json.loads((P/'collected/service-control.json').read_text())
for unit,original in state['original'].items():
    for key in ['ActiveState','UnitFileState']:
        assert state['restored'][unit][key]==original[key]
print(f'Verified {len(index)} archived files, gate and build diagnosis, and service restoration.')
