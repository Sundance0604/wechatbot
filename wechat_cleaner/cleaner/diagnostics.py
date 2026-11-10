"""Durable task diagnostics. Export records, never database files or keys."""
from contextvars import ContextVar
from datetime import datetime, timezone
from importlib.metadata import PackageNotFoundError, version
from io import BytesIO
from pathlib import Path
import hashlib
import json
import logging
import os
import platform
import re
import sqlite3
import struct
import sys
import threading
import traceback
import uuid
import zipfile

from . import __version__
from .files import CleanupError, read, save

_current = ContextVar("cleaner_diagnostic", default=None)
KINDS = {"connect", "search", "prepare", "execute", "restore", "recover", "api"}
STAGES = {"connect": {15: "connect_database"}, "search": {20: "query_messages"},
          "prepare": {10: "backup_files", 35: "decode_and_validate", 55: "update_messages_and_fts", 78: "encrypt_and_verify"},
          "execute": {20: "verify_and_install"}, "restore": {20: "verify_and_restore"},
          "recover": {20: "verify_and_recover"}}


def now():
    return datetime.now(timezone.utc).isoformat()


def event(name, **details):
    current = _current.get()
    if current:
        try:
            current[0].event(current[1], name, details)
        except OSError:
            # A diagnostics write must not interrupt an in-progress verified
            # replacement or prevent its durable recovery journal being saved.
            logging.getLogger("cleaner").error("Diagnostic event storage failed")


def scrub(value):
    # Binary key representations and 256-bit hexadecimal strings must not enter
    # diagnostics, including through an unexpected third-party exception.
    text = str(value)
    text = re.sub(r"(?i)(?<![a-f0-9])[a-f0-9]{64}(?![a-f0-9])", "[256-bit value omitted]", text)
    text = re.sub(r"b(['\"])(?:\\.|[^'\"\\])*\1", "[binary omitted]", text)
    return text[:4000]


def describe_error(error, kind="api", stage="request"):
    frames = []
    for frame in traceback.extract_tb(error.__traceback__):
        parts = Path(frame.filename).parts
        # Keep code locations, no source lines or frame local variables.
        if "cleaner" in parts:
            filename = "/".join(parts[parts.index("cleaner"):])
        else:
            filename = Path(frame.filename).name
        frames.append({"file": filename, "line": frame.lineno, "function": frame.name})
    code = getattr(error, "diagnostic_code", None)
    if not code:
        code = "SAFETY_CHECK_FAILED" if isinstance(error, CleanupError) else "UNEXPECTED_ERROR"
    location = frames[-1] if frames else {"file": "unknown", "line": 0, "function": "unknown"}
    signature = hashlib.sha256(json.dumps([__version__, kind, stage, code, type(error).__name__, location], sort_keys=True).encode()).hexdigest()[:16]
    causes = []
    cause = error.__cause__ or (None if error.__suppress_context__ else error.__context__)
    visited = {id(error)}
    while cause and id(cause) not in visited and len(causes) < 5:
        visited.add(id(cause))
        causes.append({"type": type(cause).__name__, "message": scrub(cause),
                       "frames": [{"file": Path(f.filename).name, "line": f.lineno, "function": f.name}
                                  for f in traceback.extract_tb(cause.__traceback__)]})
        cause = cause.__cause__ or (None if cause.__suppress_context__ else cause.__context__)
    return {"code": code, "type": type(error).__name__, "message": scrub(error), "causes": causes,
            "location": location, "frames": frames, "signature": signature,
            "errno": getattr(error, "errno", None), "winerror": getattr(error, "winerror", None)}


def environment(demo=False):
    packages = {}
    for name in ("fastapi", "starlette", "uvicorn", "apsw", "cryptography", "psutil", "wechatauto-replica"):
        try:
            packages[name] = version(name)
        except PackageNotFoundError:
            packages[name] = "not available"
    return {"app_version": __version__, "python": platform.python_version(),
            "os": platform.system(), "os_release": platform.release(), "os_version": platform.version(),
            "computer_name": platform.node(), "user_name": os.environ.get("USERNAME", os.environ.get("USER", "")),
            "executable": sys.executable, "frozen": bool(getattr(sys, "frozen", False)),
            "process_bits": struct.calcsize("P") * 8, "local_timezone": str(datetime.now().astimezone().tzinfo),
            "architecture": platform.machine(), "sqlite": sqlite3.sqlite_version,
            "packages": packages, "demo": demo}


class Diagnostics:
    def __init__(self, data_dir, demo=False):
        self.root = Path(data_dir) / "diagnostics"
        self.root.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        identity = self.root / "installation.json"
        if not identity.exists():
            save(identity, {"installation_id": uuid.uuid4().hex})
        self.installation_id = read(identity)["installation_id"]
        self.environment = environment(demo)
        # Persisted running records indicate a previous process exited before
        # it could record completion. Never infer that an installation succeeded.
        for path in self.root.glob("task-*.json"):
            try:
                item = read(path)
                if item.get("state") == "running":
                    item.update(state="interrupted", ended_at=now())
                    item.setdefault("events", []).append({"at": now(), "name": "previous_process_interrupted", "details": {}})
                    save(path, item)
            except (OSError, ValueError, KeyError):
                continue

    def path(self, identifier):
        if not re.fullmatch(r"[a-f0-9]{32}", identifier):
            raise CleanupError("诊断任务编号无效。")
        return self.root / f"task-{identifier}.json"

    def start(self, identifier, kind, context=None):
        with self.lock:
            save(self.path(identifier), {"id": identifier, "kind": kind if kind in KINDS else "api",
                 "state": "running", "stage": "starting", "started_at": now(), "ended_at": None,
                 "environment": self.environment, "context": context or {}, "events": [], "error": None})

    def event(self, identifier, name, details=None):
        with self.lock:
            path = self.path(identifier)
            item = read(path)
            item["events"].append({"at": now(), "name": name, "details": details or {}})
            save(path, item)

    def progress(self, identifier, percent):
        with self.lock:
            path = self.path(identifier)
            item = read(path)
            stage = STAGES.get(item["kind"], {}).get(percent, "working")
            item["stage"] = stage
            item["events"].append({"at": now(), "name": "stage", "details": {"stage": stage, "progress": percent}})
            save(path, item)

    def finish(self, identifier, error=None, result=None):
        with self.lock:
            path = self.path(identifier)
            item = read(path)
            item.update(state="failed" if error else "completed", ended_at=now())
            if error:
                item["error"] = describe_error(error, item["kind"], item["stage"])
            # Explicit projection: never persist API result message bodies, keys,
            # contact lists, or complete plans.
            if isinstance(result, dict):
                item["summary"] = {key: result[key] for key in
                    ("id", "count", "total", "eligible", "files", "version", "supported", "conversation_names", "backup", "status") if key in result}
            save(path, item)
            return item["error"]

    def records(self):
        with self.lock:
            items, unreadable = [], 0
            for path in self.root.glob("task-*.json"):
                try:
                    items.append(read(path))
                except (OSError, ValueError):
                    unreadable += 1
            return {"tasks": sorted(items, key=lambda item: item["started_at"], reverse=True), "unreadable_records": unreadable}

    def export(self):
        report = {"schema_version": 1, "exported_at": now(), "installation_id": self.installation_id,
                  "data_directory": str(self.root.parent.resolve()), "environment": self.environment, **self.records()}
        lines = ["清简任务诊断报告", f"版本：{__version__}", f"导出时间：{report['exported_at']}",
                 f"安装编号：{self.installation_id}", f"任务数：{len(report['tasks'])}",
                 "包含真实任务上下文、路径和错误信息，请仅分享给协助排查的人。", "不包含数据库文件、聊天正文结果或加密密钥。", ""]
        for item in report["tasks"]:
            lines.append(f"{item['started_at']}  {item['kind']}  {item['state']}  {item['id']}")
            lines.append(f"阶段：{item['stage']}")
            if item.get("error"):
                error = item["error"]
                loc = error["location"]
                lines += [f"错误：{error['code']}  聚类编号：{error['signature']}", error["message"],
                          f"位置：{loc['file']}:{loc['line']} ({loc['function']})"]
            lines.append("")
        output = BytesIO()
        with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("report.json", json.dumps(report, ensure_ascii=False, indent=2))
            archive.writestr("summary.txt", "\ufeff" + "\n".join(lines))
        return output.getvalue()


def inspect_fts_schema(path):
    """Only sqlite_schema definitions/PRAGMAs; never query indexed documents."""
    connection = sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True)
    try:
        rows = connection.execute("SELECT name, sql FROM sqlite_master WHERE type='table' ORDER BY name").fetchall()
        virtuals = []
        for name, sql in rows:
            if sql and re.match(r"CREATE\s+VIRTUAL\s+TABLE", sql, re.I):
                # FTS table definitions are needed to compare schema versions.
                virtuals.append({"name": name, "definition": scrub(sql)})
        details = {"page_size": connection.execute("PRAGMA page_size").fetchone()[0],
                   "user_version": connection.execute("PRAGMA user_version").fetchone()[0],
                   "table_names": [row[0] for row in rows], "virtual_tables": virtuals,
                   "expected_virtual_tables": [f"message_fts_v4_{i}" for i in range(4)]}
        event("fts_schema", **details)
        return details
    finally:
        connection.close()
