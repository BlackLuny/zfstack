"""Archive only gate evidence and read-only snapshots, never source/config/keys."""
import hashlib,json,tarfile
from pathlib import Path
P=Path(__file__).resolve().parent
files=[P/'gate-driver.log',P/'snapshot.py']
for folder in ('stage1','preflight'):
    for path in sorted((P/folder).rglob('*')):
        if path.is_dir(): continue
        assert not path.is_symlink(),path
        assert path.name.endswith(('.json','.log','.bin.gz')),path
        assert not any(word in path.name.lower() for word in ('private','secret','wireguard','wg-key')),path
        files.append(path)
archive=P/'evidence.tar.gz'
assert not archive.exists()
rows=[]
with tarfile.open(archive,'w:gz') as tar:
    for p in files:
        data=p.read_bytes(); rel=p.relative_to(P).as_posix()
        rows.append({'path':rel,'size_bytes':len(data),'sha256':hashlib.sha256(data).hexdigest()})
        tar.add(p,arcname=rel,recursive=False)
index={'files':rows,'archive_sha256':hashlib.sha256(archive.read_bytes()).hexdigest()}
(P/'evidence-index.json').write_text(json.dumps(index,indent=2)+'\n')
print(json.dumps({'files':len(rows),'bytes':archive.stat().st_size,'sha256':index['archive_sha256']}))
