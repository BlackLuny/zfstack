"""Read-only host snapshot; never read cmdline, environments or credentials."""
import json, subprocess, sys, time
from pathlib import Path
commands = [['uname','-a'], ['cat','/proc/modules'], ['free','-h'], ['df','-h','/'], ['ip','netns','list'], ['ls','-la','/var/lock/zfstack-qa'], ['docker','ps','--format','{{.Names}} {{.Status}}'], ['ps','-eo','pid,ppid,pcpu,pmem,comm','--sort=-pcpu'], ['tc','-V'], ['python3','--version']]
result = {'unix_ns': time.time_ns(), 'commands': []}
for argv in commands:
    p=subprocess.run(argv, capture_output=True, text=True, timeout=30)
    result['commands'].append({'argv':argv,'returncode':p.returncode,'stdout':p.stdout,'stderr':p.stderr})
result['meminfo']=Path('/proc/meminfo').read_text()
Path(sys.argv[1]).write_text(json.dumps(result,indent=2)+'\n')
