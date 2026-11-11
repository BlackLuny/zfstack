import json, os, subprocess, time
from pathlib import Path
T=Path('/root/zfstack-qa-pr75-20261017-canada192-02')
sha='ca9b597621614a371826866f515f6f5f77da4103'
record={'started_unix_ns':time.time_ns(),'tested_sha':sha}
passed=False
try:
    for unit in ['rc-worker.service','zfc-rig-watch.service']:
        assert subprocess.run(['systemctl','is-active','--quiet',unit]).returncode != 0, unit
    scopes=subprocess.check_output(['systemctl','list-units','--type=scope','--state=active','--no-legend','--plain'],text=True)
    assert 'rig-watch-' not in scopes
    for proc in ['cargo','rustc']:
        assert subprocess.run(['pgrep','-x',proc],stdout=subprocess.DEVNULL).returncode == 1, proc
    cmd=['python3',str(T/'source/harness/l2/results/2026-10-16-netem-fifo/remote_selftests.py'),'--expected-sha',sha,'--output',str(T/'gate'),'--domain','zfstack-qa-baremetal-192.99.148.123-24946','--run-id','canada-r2-gate-20261017']
    record['command']=cmd
    (T/'gate-launch.json').write_text(json.dumps(record,indent=2)+'\n')
    record['returncode']=subprocess.run(cmd,cwd=T/'source',env=dict(os.environ,PYTHONDONTWRITEBYTECODE='1')).returncode
    result=json.loads((T/'gate/gate.json').read_text())
    passed=record['returncode']==0 and result['gate']=='PASS'
    record['gate']=result['gate']
except BaseException as e:
    record['error']=repr(e)
finally:
    record['finished_unix_ns']=time.time_ns()
    if not passed:
        record['restore_returncode']=subprocess.run(['python3',str(T/'services.py'),'restore']).returncode
        if record['restore_returncode']==0:
            subprocess.run(['systemctl','stop','zfstack-pr75-canada02-restore-20261017.timer'])
    record['services_remain_paused_for_conditional_build']=passed
    (T/'gate-launch-result.json').write_text(json.dumps(record,indent=2)+'\n')
    print(json.dumps(record,indent=2),flush=True)
