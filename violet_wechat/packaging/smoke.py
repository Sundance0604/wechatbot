"""Run the EXE with no Python/Conda on PATH; all writes stay in an isolated test home."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import time

import httpx

parser=argparse.ArgumentParser(description=__doc__)
parser.add_argument('exe',type=Path)
parser.add_argument('--output',type=Path,required=True)
parser.add_argument('--port',type=int,default=8878)
parser.add_argument('--check-wechat-read',action='store_true')
args=parser.parse_args()
exe=args.exe.resolve();output=args.output.resolve();output.mkdir(parents=True,exist_ok=True)
home=output/'home';environment=dict(os.environ)
for key in ['PYTHONPATH','PYTHONHOME','CONDA_PREFIX','CONDA_DEFAULT_ENV','VIRTUAL_ENV','VIOLET_HOME',
            'VIOLET_DEEPSEEK_API_KEY','SSL_CERT_FILE','REQUESTS_CA_BUNDLE']:
    environment.pop(key,None)
windows=Path(environment.get('SystemRoot','C:/Windows'))
environment['PATH']=str(windows/'System32')+os.pathsep+str(windows)
for key in ['TEMP','TMP']:
    directory=output/'temp';directory.mkdir(exist_ok=True);environment[key]=str(directory)
if not args.check_wechat_read:
    for key in ['LOCALAPPDATA','APPDATA']:
        directory=output/key.lower();directory.mkdir(exist_ok=True);environment[key]=str(directory)
flags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0
common=[str(exe),'--no-open','--data-dir',str(home)]
result=subprocess.run([*common,'--self-test'],env=environment,creationflags=flags,
                      stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,timeout=90)
report=json.loads((home/'runtime/selftest.json').read_text(encoding='utf-8'))
assert result.returncode==0 and report['status']=='passed' and report['frozen'],report
with exe.open('rb') as stream:report['exe_sha256']=hashlib.file_digest(stream,'sha256').hexdigest()
process=subprocess.Popen([*common,'--port',str(args.port)],env=environment,creationflags=flags,
                         stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
base=f'http://127.0.0.1:{args.port}'
try:
    deadline=time.monotonic()+45
    while time.monotonic()<deadline:
        try:
            with httpx.Client(base_url=base,timeout=1) as client:
                token=client.get('/api/session').raise_for_status().json()['token']
            break
        except httpx.HTTPError:
            if process.poll() is not None:raise RuntimeError('EXE exited before its panel started')
            time.sleep(.25)
    else:raise RuntimeError('Packaged panel did not start')
    with httpx.Client(base_url=base,timeout=45) as client:
        assert 'Violet' in client.get('/').raise_for_status().text
        for name in ['app.js','agents.js','styles.css']:
            assert len(client.get('/assets/'+name).raise_for_status().content)>100
        clean=client.get('/api/settings').raise_for_status().json()
        assert not clean['api_key_configured'] and clean['groups']==[] and clean['workspaces']==[]
        assert clean['admin_accounts']==clean['secondary_admin_accounts']==[]
        assert clean['self_id']==clean['wechat_account']==clean['wechat_data_dir']==''
        assert clean['bot_name']=='VioletBot' and clean['mentions']==['VioletBot'] and clean['persona']==''
        assert client.get('/api/profile').raise_for_status().json()['content']==''
        assert client.put('/api/settings',json={'poll_seconds':12}).status_code==403
        headers={'x-violet-local':token}
        updated=client.put('/api/settings',headers=headers,json={'agent':{
            'deepseek_enabled':True,'deepseek_path':'C:/distribution-test/harness.exe'}}).raise_for_status().json()
        assert client.put('/api/settings',headers=headers,json=clean).status_code==409
        assert client.get('/api/settings').json()['agent']['deepseek_path']==updated['agent']['deepseek_path']
        client.put('/api/settings',headers=headers,json={'agent':{'deepseek_enabled':False,'deepseek_path':''}}).raise_for_status()
        if args.check_wechat_read:
            connected=client.post('/api/connect',headers=headers).raise_for_status().json()
            report['wechat_read_connected']=bool(connected.get('account'))
            recovered=client.post('/api/native/recover',headers=headers).raise_for_status().json()
            report['native_recovery_connected']=bool(recovered.get('connected'))
            report['detected_wechat_version']=recovered.get('version')
        report.update(panel_started=True,assets_loaded=True,private_defaults_clean=True,
                      unauthorized_write_blocked=True,stale_settings_blocked=True)
    repeated=subprocess.run([*common,'--port',str(args.port)],env=environment,creationflags=flags,
                            stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,timeout=30)
    assert repeated.returncode==0 and process.poll() is None
    stopped=subprocess.run([*common,'--quit'],env=environment,creationflags=flags,
                          stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,timeout=30)
    assert stopped.returncode==0
    process.wait(timeout=20)
    assert process.returncode==0
    report.update(repeated_launch_ok=True,graceful_quit_ok=True)
finally:
    # A one-file EXE has a bootloader parent and server child. Always request
    # graceful shutdown; killing only the parent would orphan the server.
    subprocess.run([*common,'--quit'],env=environment,creationflags=flags,
                   stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,timeout=30)
    if process.poll() is None:
        try:process.wait(timeout=15)
        except subprocess.TimeoutExpired:
            import psutil
            parent=psutil.Process(process.pid)
            children=parent.children(recursive=True)
            for child in children:
                if Path(child.exe()).resolve()==exe:child.terminate()
            process.terminate();process.wait(timeout=10)
(output/'smoke.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
print(json.dumps(report,ensure_ascii=False))
