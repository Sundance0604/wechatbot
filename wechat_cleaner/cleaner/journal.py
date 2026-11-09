"""Verified file replacement with a durable journal and guarded recovery."""
from __future__ import annotations

from datetime import datetime, timezone
import os
from pathlib import Path
import re
import shutil

from .files import CleanupError, hashes, read, require_stopped, safe_path, save, sha

PENDING = {"applying", "restoring", "recovery_required"}
SIDECARS = ("-wal", "-shm", "-journal")


class Journal:
    def __init__(self, root, stopped=require_stopped, replace=os.replace):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.stopped = stopped
        self.replace = replace

    def load(self, identifier):
        if not re.fullmatch(r"[a-f0-9]{32}", identifier):
            raise CleanupError("备份编号无效。")
        run = safe_path(self.root, identifier)
        plan = read(run / "plan.json")
        if plan.get("id") != identifier:
            raise CleanupError("备份编号不一致。")
        source = Path(plan["source"]).resolve()
        if source.name != "db_storage" or source.parent.name != plan["account"]:
            raise CleanupError("备份账号路径不一致。")
        if source.is_relative_to(run) or run.is_relative_to(source):
            raise CleanupError("备份和微信数据目录不能重叠。")
        if hashes(run / "backup") != plan["baseline"]:
            raise CleanupError("备份校验未通过，拒绝修改微信数据。")
        if not plan.get("changes") or len({c["path"] for c in plan["changes"]}) != len(plan["changes"]):
            raise CleanupError("清理计划中的文件集合无效。")
        for change in plan["changes"]:
            rel = change["path"]
            if not re.fullmatch(r"message/(?:message_\d+|message_fts)\.db", rel):
                raise CleanupError("清理计划包含未支持的文件。")
            if change["before"] != plan["baseline"].get(rel) or sha(safe_path(run / "staged", rel)) != change["after"]:
                raise CleanupError("清理计划的文件校验未通过。")
        return run, plan, source

    def list(self):
        results = []
        for path in sorted(self.root.glob("*/plan.json"), reverse=True):
            try:
                plan = read(path)
                results.append({key: plan.get(key) for key in ("id", "created_at", "status", "account", "account_name", "count", "group_names", "conversation_names", "conversation_count", "keyword", "error")})
            except (OSError, ValueError):
                continue
        return results

    def pending(self):
        return [p for p in self.list() if p["status"] in PENDING]

    def _temporary(self, source, rel, identifier):
        target = safe_path(source, rel)
        return target.with_name(target.name + ".cleanup-" + identifier + ".tmp")

    def _copy(self, source_file, target, temporary):
        if temporary.exists():
            raise CleanupError("发现未完成的写入文件，请先恢复上次操作。")
        shutil.copy2(source_file, temporary)
        with temporary.open("rb+") as stream:
            stream.flush()
            os.fsync(stream.fileno())
        if sha(temporary) != sha(source_file):
            raise CleanupError("文件复制校验失败。")
        self.stopped()
        self.replace(temporary, target)

    def apply(self, identifier):
        self.stopped()
        run, plan, source = self.load(identifier)
        if plan["status"] != "prepared" or self.pending():
            raise CleanupError("当前计划不能执行，或存在需要先恢复的操作。")
        if hashes(source) != plan["baseline"]:
            raise CleanupError("微信数据在备份后发生了变化，请重新查询并准备清理。")
        expected = dict(plan["baseline"])
        plan.update(status="applying", completed_files=[])
        save(run / "plan.json", plan)
        try:
            for change in plan["changes"]:
                self.stopped()
                if hashes(source) != expected:
                    raise CleanupError("微信数据在写入过程中变化，已停止。")
                rel = change["path"]
                for suffix in SIDECARS:
                    sidecar = safe_path(source, rel + suffix)
                    if sidecar.exists():
                        self.stopped()
                        sidecar.unlink()
                    expected.pop(rel + suffix, None)
                target = safe_path(source, rel)
                self._copy(safe_path(run / "staged", rel), target, self._temporary(source, rel, identifier))
                expected[rel] = change["after"]
                plan["completed_files"].append(rel)
                save(run / "plan.json", plan)
            self.stopped()
            if hashes(source) != expected:
                raise CleanupError("写入后的文件校验未通过。")
            plan.update(status="installed", installed_hashes=expected,
                        installed_at=datetime.now(timezone.utc).isoformat(), client_acceptance_tested=False)
            save(run / "plan.json", plan)
        except BaseException as error:
            plan.update(status="recovery_required", error=type(error).__name__)
            save(run / "plan.json", plan)
            try:
                self.recover(identifier)
            except BaseException:
                pass
            raise
        return {"id": identifier, "status": "installed", "count": plan["count"],
                "backup": str(run / "backup"), "client_acceptance_tested": False}

    def recover(self, identifier, restore=False):
        self.stopped()
        run, plan, source = self.load(identifier)
        if restore:
            if plan["status"] != "installed" or hashes(source) != plan.get("installed_hashes"):
                raise CleanupError("微信已产生新的数据或当前状态不可恢复；为避免覆盖新记录，已停止。")
        elif plan["status"] not in PENDING:
            raise CleanupError("该操作不需要中断恢复。")
        current = hashes(source)
        allowed = {rel: {value} for rel, value in plan["baseline"].items()}
        optional = set()
        for change in plan["changes"]:
            rel = change["path"]
            allowed[rel].add(change["after"])
            optional.update(rel + suffix for suffix in SIDECARS)
            temporary = self._temporary(source, rel, identifier)
            temporary_rel = temporary.relative_to(source).as_posix()
            if temporary_rel in current:
                allowed[temporary_rel] = {current[temporary_rel]}
                optional.add(temporary_rel)
        if any(rel not in allowed or value not in allowed[rel] for rel, value in current.items()) or any(rel not in current for rel in allowed if rel not in optional):
            raise CleanupError("检测到清理计划以外的数据变化，请保留备份并人工处理。")
        plan["status"] = "restoring"
        save(run / "plan.json", plan)
        try:
            for change in plan["changes"]:
                self.stopped()
                rel = change["path"]
                temporary = self._temporary(source, rel, identifier)
                if temporary.exists():
                    temporary.unlink()
                self._copy(safe_path(run / "backup", rel), safe_path(source, rel), temporary)
                for suffix in SIDECARS:
                    saved = safe_path(run / "backup", rel + suffix)
                    sidecar = safe_path(source, rel + suffix)
                    self.stopped()
                    if saved.exists():
                        shutil.copy2(saved, sidecar)
                    elif sidecar.exists():
                        sidecar.unlink()
            self.stopped()
            if hashes(source) != plan["baseline"]:
                raise CleanupError("恢复后的文件校验未通过。")
            plan.update(status="restored" if restore else "rolled_back", error=None)
        except BaseException:
            plan["status"] = "recovery_required"
            raise
        finally:
            save(run / "plan.json", plan)
        return {"id": identifier, "status": plan["status"]}
