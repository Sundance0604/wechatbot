from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import shutil
import threading
import uuid

from .changes import modify_copies, stage_encrypted
from .files import CleanupError, hashes, read, require_stopped, safe_path, save, remove_workspace_tree
from .journal import Journal
from .reader import LocalReader, SnapshotReader, SUPPORTED_VERSIONS, accounts
from .selection import search, validate_targets
from .diagnostics import event


class CleanerService:
    def __init__(self, data_dir, reader_factory=LocalReader, stopped=require_stopped):
        self.data_dir = Path(data_dir).resolve()
        self.data_dir.mkdir(parents=True, exist_ok=True)
        remove_workspace_tree(self.data_dir, "cache")
        self.journal = Journal(self.data_dir / "runs", stopped=stopped)
        for run in self.journal.root.iterdir():
            if run.is_dir() and len(run.name) == 32 and all(c in "0123456789abcdef" for c in run.name):
                for name in ("plain", "modified"):
                    remove_workspace_tree(run, name)
        self.reader_factory = reader_factory
        self.stopped = stopped
        self.reader = None
        self.scans = {}
        self.lock = threading.Lock()

    def connect(self, account, directory=None, progress=lambda *args: None):
        with self.lock:
            progress(15, "正在连接本机微信数据库")
            reader = self.reader_factory(account, directory, self.data_dir / "cache")
            self.reader = reader
            self.scans.clear()
            return reader.info()

    def groups(self):
        if self.reader is None:
            raise CleanupError("请先连接微信账号。")
        return self.reader.groups()

    def contacts(self):
        if self.reader is None:
            raise CleanupError("请先连接微信账号。")
        return self.reader.people()

    def conversations(self):
        return [{**item, "type": kind} for kind, items in
                (("contact", self.contacts()), ("group", self.groups())) for item in items]

    def search(self, request, progress=lambda *args: None):
        with self.lock:
            if self.reader is None:
                raise CleanupError("请先连接微信账号。")
            progress(20, "正在查询所选联系人与群聊的本地消息")
            scope = request.get("conversation_ids", request.get("group_ids", []))
            event("query_context", account=self.reader.account, version=self.reader.version,
                  source=str(self.reader.source), keyword=request.get("keyword"),
                  conversations=[{"id": user, "name": self.reader.names.get(user, user)} for user in scope])
            result = search(self.reader, **request)
            identifier = uuid.uuid4().hex
            self.scans = {identifier: {"account": self.reader.account, "messages": result["messages"], "request": request}}
            return {"scan_id": identifier, **result}

    def prepare(self, scan_id, ids, progress=lambda *args: None):
        with self.lock:
            self.stopped()
            if self.reader is None or scan_id not in self.scans:
                raise CleanupError("查询已失效，请重新连接并查询。")
            if self.journal.pending():
                raise CleanupError("请先在备份页面恢复上次中断的操作。")
            if self.reader.version not in SUPPORTED_VERSIONS:
                raise CleanupError("尚未确认受支持的微信版本。请先登录微信并重新连接；当前支持 4.2.0.11 / 4.2.0.13 的历史纯文本。")
            scan = self.scans[scan_id]
            if scan["account"] != self.reader.account:
                raise CleanupError("当前账号与查询账号不一致。")
            selected = set(ids)
            known = {item["id"]: item for item in scan["messages"]}
            if not selected or len(selected) != len(ids) or any(i not in known for i in selected):
                raise CleanupError("请选择本次预览中有效且不重复的消息。")
            targets = [known[i] for i in ids]
            if any(item["blocked_reason"] for item in targets):
                raise CleanupError("选中消息包含暂不支持的类型，请重新选择。")
            identifier = uuid.uuid4().hex
            run = self.journal.root / identifier
            run.mkdir()
            source = self.reader.source.resolve()
            if run.is_relative_to(source) or source.is_relative_to(run):
                raise CleanupError("备份目录不能位于微信数据目录中。")
            conversations = list({item["user"]: {"id": item["user"], "name": item["conversation_name"],
                                                  "type": item["conversation_type"]} for item in targets}.values())
            plan = {"id": identifier, "status": "building", "created_at": datetime.now(timezone.utc).isoformat(),
                    "account": self.reader.account, "account_name": self.reader.info()["name"],
                    "source": str(source), "version": self.reader.version, "count": len(targets),
                    "group_names": sorted({m["group_name"] for m in targets}),
                    "conversation_names": [item["name"] for item in conversations],
                    "conversation_count": len(conversations), "conversations": conversations,
                    "keyword": scan["request"]["keyword"], "changes": []}
            save(run / "plan.json", plan)
            event("plan_context", run_id=identifier, source=str(source), version=self.reader.version,
                  account=self.reader.account, account_name=plan["account_name"], keyword=plan["keyword"],
                  conversations=conversations, selected_count=len(targets))
            try:
                progress(10, "正在备份当前账号的数据库和日志")
                baseline = hashes(source)
                event("backup_inventory", file_count=len(baseline), source=str(source), backup=str(run / "backup"))
                shutil.copytree(source, run / "backup")
                self.stopped()
                if hashes(run / "backup") != baseline or hashes(source) != baseline:
                    raise CleanupError("备份期间微信数据发生变化，请确认微信已退出后重新操作。")
                plan["baseline"] = baseline
                save(run / "plan.json", plan)
                progress(35, "备份校验完成，正在重新核对候选消息")
                relatives = sorted({"session/session.db", "message/message_resource.db", "message/message_fts.db"}
                                   | {item["shard"] for item in targets})
                plan["wal_checks"] = self.reader.decode_backup(run / "backup", run / "plain", relatives)
                event("wal_checks_completed", checks=plan["wal_checks"])
                validate_targets(SnapshotReader(run / "plain", self.reader), targets)
                progress(55, "正在副本中更新消息正文和全文索引")
                changed = modify_copies(run / "plain", run / "modified", targets)
                progress(78, "正在重新加密并逐页验证写回文件")
                changes = stage_encrypted(run / "backup", run / "modified", run / "staged", changed, self.reader.keys)
                self.stopped()
                if hashes(source) != baseline or hashes(run / "backup") != baseline:
                    raise CleanupError("微信数据在准备期间发生变化，已停止。")
                plan.update(status="prepared", changes=changes, backup_verified=True,
                            retained_rows_verified=True, fts_tokens_verified=True, encryption_verified=True)
                save(run / "plan.json", plan)
                self.scans.clear()
                return {"id": identifier, "count": len(targets), "group_names": plan["group_names"],
                        "conversation_names": plan["conversation_names"], "conversations": conversations,
                        "conversation_count": len(conversations),
                        "files": len(changes), "backup": str(run / "backup"), "status": "prepared"}
            except BaseException as error:
                plan.update(status="failed", error=str(error) if isinstance(error, CleanupError) else type(error).__name__)
                save(run / "plan.json", plan)
                raise
            finally:
                for name in ("plain", "modified"):
                    remove_workspace_tree(run, name)

    def execute(self, identifier, progress=lambda *args: None):
        with self.lock:
            progress(20, "正在核对备份并写入已验证的文件")
            return self.journal.apply(identifier)

    def restore(self, identifier, recovery=False, progress=lambda *args: None):
        with self.lock:
            progress(20, "正在核对当前文件并恢复备份")
            return self.journal.recover(identifier, restore=not recovery)

    def close(self):
        self.reader = None
        self.scans.clear()
        remove_workspace_tree(self.data_dir, "cache")
