"""Use a real Windows file lock to exercise repeat launches without starting a server or WeChat."""
import os
from pathlib import Path
import subprocess
import sys
import time

import pytest

from violet.settings import ROOT


@pytest.mark.skipif(sys.platform!='win32',reason='Windows byte-range locking regression')
def test_repeat_launch_opens_existing_panel_and_stop_still_works(tmp_path):
    env={**os.environ,'VIOLET_HOME':str(tmp_path),'PYTHONPATH':str(ROOT)}
    holder=r'''
import sys,types
from pathlib import Path
from violet.settings import RUNTIME
from violet.cli import main
def hold(app,**kwargs):
    (RUNTIME/'ready').write_text('ready')
    sys.stdin.readline()
sys.modules['uvicorn']=types.SimpleNamespace(run=hold)
sys.modules['violet.web']=types.SimpleNamespace(create_app=lambda **kwargs:None)
sys.argv=['violet']
main()
'''
    process=subprocess.Popen([sys.executable,'-c',holder],env=env,stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
    runtime=tmp_path/'runtime'
    try:
        deadline=time.monotonic()+10
        while not (runtime/'ready').exists() and process.poll() is None and time.monotonic()<deadline:
            time.sleep(.05)
        assert (runtime/'ready').exists(), 'holder did not acquire the initially empty lock'
        opener=r'''
import sys,webbrowser
from violet.cli import main
webbrowser.open=lambda url: print('OPENED '+url) or True
sys.argv=['violet','--open']
main()
'''
        repeated=subprocess.run([sys.executable,'-c',opener],env=env,capture_output=True,text=True,timeout=10)
        assert repeated.returncode==0, repeated.stderr
        assert 'already running' in repeated.stdout and 'OPENED http://127.0.0.1:8766' in repeated.stdout
        assert 'could not start' not in repeated.stdout
        stopped=subprocess.run([sys.executable,'-m','violet','--stop'],env=env,capture_output=True,text=True,timeout=10)
        assert stopped.returncode==0 and (runtime/'STOP').exists() and (runtime/'AGENT_STOP').exists()
        assert process.poll() is None
    finally:
        if process.poll() is None:
            process.communicate('\n',timeout=10)
        else:
            process.communicate()
    assert process.returncode==0
    assert (runtime/'server.lock').read_bytes()==b'0'
