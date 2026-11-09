from __future__ import annotations

import argparse
import json
import logging
import multiprocessing
import os
from pathlib import Path
import socket
import threading
import time
import urllib.request
import webbrowser


def main():
    multiprocessing.freeze_support()
    parser = argparse.ArgumentParser(description="清简 · 微信记录清理")
    parser.add_argument("--data-dir", type=Path)
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--demo", action="store_true", help="Synthetic demo; never reads or modifies WeChat")
    parser.add_argument("--self-test", action="store_true", help="Offline executable diagnostics using synthetic data only")
    args = parser.parse_args()
    root = args.data_dir or Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "WeChatCleaner"
    if args.demo:
        root = root / "demo"
    root.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(filename=root / "application.log", level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s", encoding="utf-8")
    if args.self_test:
        from .selftest import run_selftest
        run_selftest(root)
        return
    from .files import save
    instance = root / "instance.json"
    with (root / "server.lock").open("a+b") as guard:
        try:
            if os.name == "nt":
                import msvcrt
                guard.seek(0)
                msvcrt.locking(guard.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(guard.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            if instance.exists() and not args.no_browser:
                port = json.loads(instance.read_text())["port"]
                if isinstance(port, int) and 0 < port < 65536:
                    webbrowser.open(f"http://127.0.0.1:{port}")
            return
        sock = socket.socket()
        sock.bind(("127.0.0.1", args.port))
        port = sock.getsockname()[1]
        save(instance, {"port": port, "pid": os.getpid()})
        import uvicorn
        from .web import create_app
        server = None

        def quit_server():
            if server:
                server.should_exit = True

        app = create_app(root, demo=args.demo, on_quit=quit_server)
        config = uvicorn.Config(app, host="127.0.0.1", port=port, access_log=False,
                                log_config=None, log_level="warning")
        server = uvicorn.Server(config)

        def open_when_ready():
            for _ in range(100):
                try:
                    with urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=1) as response:
                        if response.status == 200:
                            webbrowser.open(f"http://127.0.0.1:{port}")
                            return
                except OSError:
                    time.sleep(.15)
        if not args.no_browser:
            threading.Thread(target=open_when_ready, daemon=True).start()
        try:
            server.run(sockets=[sock])
        finally:
            sock.close()
            instance.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
