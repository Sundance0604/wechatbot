"""Test an executable with Conda/Python removed from PATH. Uses synthetic data."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import time
import urllib.request

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("exe", type=Path)
parser.add_argument("--output", type=Path, required=True)
parser.add_argument("--serve-demo", action="store_true")
parser.add_argument("--port", type=int, default=8878)
args = parser.parse_args()
exe = args.exe.resolve()
output = args.output.resolve()
output.mkdir(parents=True, exist_ok=True)
environment = dict(os.environ)
for key in ("PYTHONPATH", "PYTHONHOME", "CONDA_PREFIX", "CONDA_DEFAULT_ENV"):
    environment.pop(key, None)
environment["PATH"] = str(Path(environment.get("SystemRoot", "C:/Windows")) / "System32")
flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
result = subprocess.run([str(exe), "--self-test", "--data-dir", str(output / "diagnostics")],
                        env=environment, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                        creationflags=flags, timeout=45)
report = json.loads((output / "diagnostics/selftest.json").read_text())
assert result.returncode == 0 and report["status"] == "passed" and report["frozen"]
if args.serve_demo:
    process = subprocess.Popen([str(exe), "--demo", "--no-browser", "--port", str(args.port),
                                "--data-dir", str(output / "ui")], env=environment,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=flags)
    try:
        for _ in range(100):
            try:
                request = urllib.request.Request(f"http://127.0.0.1:{args.port}/api/session", headers={"X-Cleaner-Client":"1"})
                with urllib.request.urlopen(request, timeout=.5) as response:
                    session = json.load(response)
                assert session["demo"] is True
                report.update(demo_pid=process.pid, demo_port=args.port, demo_ready=True)
                break
            except OSError:
                if process.poll() is not None:
                    raise RuntimeError("Packaged server exited before becoming ready")
                time.sleep(.2)
        else:
            raise RuntimeError("Packaged server did not become ready")
    except BaseException:
        process.terminate()
        process.wait(timeout=10)
        raise
(output / "smoke.json").write_text(json.dumps(report,indent=2),encoding="utf-8")
print(json.dumps(report))
