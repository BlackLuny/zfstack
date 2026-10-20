"""Install only task-local, hashed tool inputs. No system packages or services."""
import hashlib,json,os,subprocess,time
from pathlib import Path
P=Path(__file__).resolve().parent
E=P/'evidence';E.mkdir(exist_ok=True)
rows=[]
env=dict(os.environ,RUSTUP_HOME=str(P/'rustup'),CARGO_HOME=str(P/'cargo-home'),PYTHONDONTWRITEBYTECODE='1')

def run(name,argv,extra=None):
    start=time.time_ns()
    with (E/(name+'.log')).open('xb') as log:
        r=subprocess.run(argv,env=dict(env,**(extra or {})),stdout=log,stderr=subprocess.STDOUT,timeout=300)
    rows.append(dict(name=name,argv=argv,returncode=r.returncode,start_unix_ns=start,end_unix_ns=time.time_ns()))
    (E/'tools-execution.json').write_text(json.dumps(rows,indent=2)+'\n')
    if r.returncode:raise RuntimeError(name+' failed; retained')

for r in json.loads((P/'tool-inputs.json').read_text()):
    f=P/r['path'];assert hashlib.sha256(f.read_bytes()).hexdigest()==r['sha256']
T=P/'tool-unpack';T.mkdir()
for name in ('rustc','cargo','rust-std'):
    package=P/(name+'-1.98.1-linux.tar.xz')
    run('unpack-'+name,['tar','-xJf',str(package),'-C',str(T)])
    installer=T/(name+'-1.98.1-x86_64-unknown-linux-gnu')/'install.sh'
    run('install-'+name,['bash',str(installer),'--prefix='+str(P/'rustup/toolchains/1.98.1-x86_64-unknown-linux-gnu'),'--disable-ldconfig'])
run('create-qa-venv',['python3','-m','venv','--without-pip',str(P/'qa-venv')])
run('install-qa-wheels',[str(P/'qa-venv/bin/python'),'-m','pip','install','--no-index','--find-links',str(P/'wheels'),'-r',str(P/'source/qa/requirements.lock'),'pip==25.2','cmake==3.31.6'],{'PYTHONPATH':str(P/'wheels/pip-25.2-py3-none-any.whl')})
run('rustc-version',['/root/.cargo/bin/rustc','+1.98.1','-Vv'])
run('cargo-version',['/root/.cargo/bin/cargo','+1.98.1','-Vv'])
print('task-local tools ready',flush=True)
