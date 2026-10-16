import json,subprocess,time
from pathlib import Path
p=Path(__file__).resolve().parent
r=subprocess.run(['python3',str(p/'build_and_run.py'),'--cargo-quiet'],cwd=p)
(p/'build-driver-exit.json').write_text(json.dumps(dict(returncode=r.returncode,finished_unix_ns=time.time_ns()))+'\n')
