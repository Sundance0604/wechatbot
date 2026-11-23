"""Windowless release launcher; double-click opens the local panel."""
import ctypes
import os
from pathlib import Path
import sys
import traceback


def main():
    home=os.environ.get('VIOLET_HOME')
    if '--data-dir' in sys.argv:
        index=sys.argv.index('--data-dir')
        if index+1<len(sys.argv):home=sys.argv[index+1]
    root=Path(home).expanduser().resolve() if home else Path(os.environ.get('LOCALAPPDATA',str(Path.home())))/'Violet'
    runtime=root/'runtime';runtime.mkdir(parents=True,exist_ok=True)
    log=runtime/'launcher.log'
    sys.stdout=log.open('a',encoding='utf-8',buffering=1)
    sys.stderr=sys.stdout
    if sys.stdin is None:sys.stdin=open(os.devnull,'r')
    if not any(flag in sys.argv for flag in ['--no-open','--headless','--stop','--quit','--self-test','--help','-h','--version']):
        if '--open' not in sys.argv:sys.argv.append('--open')
    try:
        from violet.cli import main as run
        run()
    except SystemExit as exc:
        if exc.code in (None,0):return
        traceback.print_exc()
        if '--no-open' not in sys.argv:
            ctypes.windll.user32.MessageBoxW(None,'Violet 未能启动。请检查微信版本、配置和端口。\n诊断日志：'+str(log),'Violet',0x10)
        raise
    except Exception:
        traceback.print_exc()
        if '--no-open' not in sys.argv:
            ctypes.windll.user32.MessageBoxW(None,'Violet 未能启动。诊断日志：'+str(log),'Violet',0x10)
        raise


if __name__=='__main__':main()
