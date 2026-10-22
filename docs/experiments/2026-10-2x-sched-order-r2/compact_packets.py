#!/usr/bin/env python3
"""Lossless gzip-header normalization and hard-link dedup; every run path remains."""
import hashlib,json,os,pathlib
out=pathlib.Path(__file__).resolve().parent
seen={};rows=[]
for p in sorted((out/'packets').glob('*.bin.gz')):
 data=p.read_bytes();assert data[:3]==b'\x1f\x8b\x08'
 assert data[3] in (0,8),data[3]
 end=data.index(b'\0',10)+1 if data[3]&8 else 10
 canonical=data[:3]+b'\0'+b'\0'*4+data[8:10]+data[end:]
 old=hashlib.sha256(data).hexdigest();new=hashlib.sha256(canonical).hexdigest()
 if new in seen:
  tmp=p.with_suffix('.dedup');os.link(seen[new],tmp);os.replace(tmp,p)
 else:
  if canonical!=data:p.write_bytes(canonical)
  seen[new]=p
 rows.append({'path':str(p.relative_to(out)),'original_compressed_sha256':old,'canonical_compressed_sha256':new,'canonical_size':len(canonical),'same_content_as':str(seen[new].relative_to(out))})
(out/'packet-storage.json').write_text(json.dumps({'note':'Only gzip filename/mtime headers normalized; DEFLATE bytes and CRC/size trailer retained. Hard links deduplicate identical archive bytes while preserving every execution path. Uncompressed stream hashes in raw JSONL remain valid.','paths':len(rows),'unique_archives':len(seen),'files':rows},indent=2)+'\n')
print(json.dumps({'paths':len(rows),'unique_archives':len(seen)}))
