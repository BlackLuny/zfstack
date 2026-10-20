#!/usr/bin/env python3
"""Verify published bytes against both packaging and original evidence indexes."""
import argparse
import gzip
import hashlib
import json
from pathlib import Path


def sha(data):
    return hashlib.sha256(data).hexdigest()


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument("path",nargs="?",type=Path,default=Path(__file__).resolve().parents[1]/"docs/experiments/2026-10-20-issue69-tail")
    out=parser.parse_args().path
    manifest=json.loads((out/"publication-manifest.json").read_text())
    original_bytes=(out/"artifact-index.json").read_bytes()
    if sha(original_bytes)!=manifest["original_index_sha256"]:
        raise ValueError("original index identity mismatch")
    originals=json.loads(original_bytes)["files"]
    seen=set();published=0;omitted=0
    for item in manifest["files"]:
        name=item["original"]
        if name in seen:raise ValueError("duplicate original: "+name)
        seen.add(name)
        if name in originals and any(item[k]!=originals[name][k] for k in ("bytes","sha256")):
            raise ValueError("original index entry mismatch: "+name)
        if item["storage"]=="local-only-executable":
            if not name.startswith("bin/"):raise ValueError("non-executable omission: "+name)
            omitted+=1;continue
        path=(out/item["path"]).resolve()
        if not path.is_relative_to(out.resolve()):raise ValueError("path outside evidence directory")
        data=path.read_bytes()
        if sha(data)!=item["stored_sha256"]:raise ValueError("stored bytes mismatch: "+name)
        if item["storage"]=="gzip":data=gzip.decompress(data)
        elif item["storage"]!="original":raise ValueError("unknown storage: "+name)
        if len(data)!=item["bytes"] or sha(data)!=item["sha256"]:
            raise ValueError("uncompressed original mismatch: "+name)
        published+=1
    if not set(originals)<=seen:raise ValueError("original evidence entry missing from publication manifest")
    print(f"verified {published} archived original files; {omitted} local-only executables explicitly omitted")


if __name__=="__main__":main()
