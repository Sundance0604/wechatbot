"""ExampleOwnernch independently: python run.py [--port 8766] [--open]."""
import argparse
import os
from pathlib import Path
import threading
import webbrowser


def _run():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port',type=int,default=8766)
    parser.add_argument('--open',action='store_true')
    parser.add_argument('--data-dir',type=Path,help='Independent configuration and runtime directory')
    parser.add_argument('--config',type=Path,help='Optional configuration JSON')
    parser.add_argument('--stop',action='store_true',help='Stop monitoring and all Agent jobs in this data directory')
    parser.add_argument('--headless',action='store_true',help='Start monitoring without opening a browser')
    args = parser.parse_args()
    if args.data_dir:
        os.environ['VIOLET_HOME']=str(args.data_dir.expanduser().resolve())
    from violet.settings import RUNTIME
    RUNTIME.mkdir(parents=True,exist_ok=True)
    if args.stop:
        (RUNTIME/'STOP').touch()
        (RUNTIME/'AGENT_STOP').touch()
        print('Stop requested for monitoring and Agent jobs.')
        return
    import msvcrt
    with (RUNTIME/'server.lock').open('a+b') as guard:
        guard.seek(0)
        try:
            # Windows denies reads of another process's locked byte. Acquire first, including on an empty file.
            msvcrt.locking(guard.fileno(),msvcrt.LK_NBLCK,1)
        except OSError:
            print('Violet is already running. Open http://127.0.0.1:'+str(args.port))
            if args.open:
                webbrowser.open(f'http://127.0.0.1:{args.port}')
            return
        if os.fstat(guard.fileno()).st_size==0:
            guard.write(b'0');guard.flush()
        if args.headless:
            import asyncio
            from violet.engine import Engine
            from violet.settings import ConfigStore
            from violet.store import Store
            async def monitor():
                store = Store(RUNTIME/'state.sqlite3')
                config = ConfigStore(args.config) if args.config else ConfigStore()
                engine = Engine(config,store,RUNTIME)
                from violet.agent import AgentManager
                agents = AgentManager(config,store,RUNTIME,engine.agent_send_message,engine.agent_find_contacts,engine.agent_finished,on_cancel=engine.agent_cancelled)
                engine.agent_manager=agents
                try:
                    await engine.start()
                    if engine.task:await engine.task
                finally:
                    await engine.stop()
                    await agents.close()
                    store.close()
            asyncio.run(monitor())
        else:
            import uvicorn
            from violet.web import create_app
            if args.open:
                threading.Timer(1.5,lambda:webbrowser.open(f'http://127.0.0.1:{args.port}')).start()
            uvicorn.run(create_app(config_path=args.config),host='127.0.0.1',port=args.port,access_log=False,log_level='warning')


def main():
    try:
        return _run()
    except KeyboardInterrupt:
        print('Violet stopped.')
    except (ValueError,RuntimeError,OSError):
        print('Violet could not start. Check configuration, port, dependencies, and account connection.')
        raise SystemExit(1) from None


if __name__=='__main__':
    main()
