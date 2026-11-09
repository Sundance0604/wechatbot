"""Explicit, synthetic interface demo. Never constructs a database reader."""
from datetime import datetime, timezone
from types import SimpleNamespace

from .files import CleanupError


class DemoJournal:
    def __init__(self):
        self.items = []

    def list(self):
        return self.items

    def pending(self):
        return []


class DemoService:
    def __init__(self):
        self.reader = None
        self.journal = DemoJournal()
        self.selected = []
        self.deleted_ids = set()
        self.messages = {}

    def connect(self, account, directory=None, progress=lambda *args: None):
        self.reader = self
        return self.info()

    def info(self):
        return {"account": "demo_account", "name": "演示账号", "version": "4.2.0.13", "supported": True, "group_count": 3, "contact_count": 2}

    def groups(self):
        return [{"id": "demo_design@chatroom", "name": "设计灵感交流"},
                {"id": "demo_reading@chatroom", "name": "周末读书会"},
                {"id": "demo_friends@chatroom", "name": "朋友们的小客厅"}]

    def contacts(self):
        return [{"id": "wxid_demo_lin", "name": "林小夏"}, {"id": "wxid_demo_chen", "name": "陈同学"}]

    def conversations(self):
        return [{**item, "type": kind} for kind, items in
                (("contact", self.contacts()), ("group", self.groups())) for item in items]

    def search(self, request, progress=lambda *args: None):
        now = int(datetime.now(timezone.utc).timestamp())
        chosen = request.get("conversation_ids", request.get("group_ids", []))
        conversations = [c for c in self.conversations() if c["id"] in chosen]
        if not conversations:
            raise CleanupError("请先选择联系人或群聊。")
        keyword = request["keyword"]
        messages = [{"id": f'{conversation["id"]}-{i}', "text": text, "sender": sender,
                     "create_time": now - i * 3600, "group_name": conversation["name"],
                     "conversation_name": conversation["name"], "conversation_type": conversation["type"],
                     "user": conversation["id"], "blocked_reason": reason}
                    for conversation in conversations
                    for i, (text, sender, reason) in enumerate([
                        (f"这条{keyword}信息已经不需要了。", "小林", None),
                        (f"分享一个关于{keyword}的记录。", "阿远", None),
                        (f"今天的{keyword}讨论先到这里。", "小周", "会话最后一条消息，暂不支持清理")], 1)
                    if f'{conversation["id"]}-{i}' not in self.deleted_ids]
        if request.get("match") == "exact":
            messages = [m for m in messages if m["text"] == keyword]
        self.messages = {m["id"]: m for m in messages}
        return {"scan_id": "demo", "messages": messages, "total": len(messages), "truncated": False,
                "eligible": sum(not m["blocked_reason"] for m in messages)}

    def prepare(self, scan_id, ids, progress=lambda *args: None):
        if not ids or any(identifier not in self.messages or self.messages[identifier]["blocked_reason"] for identifier in ids):
            raise CleanupError("请选择当前预览中可清理的消息。")
        self.selected = ids
        return self.plan_info()

    def plan_info(self):
        selected = [self.messages[identifier] for identifier in self.selected]
        conversations = list({m["user"]: {"id": m["user"], "name": m["conversation_name"], "type": m["conversation_type"]} for m in selected}.values())
        names = [c["name"] for c in conversations]
        return {"id": "demo_plan", "count": len(self.selected), "group_names": names,
                "conversation_names": names, "conversations": conversations, "conversation_count": len(conversations),
                "files": 2, "backup": "演示模式：没有创建真实备份", "status": "prepared"}

    def execute(self, identifier, progress=lambda *args: None):
        self.deleted_ids.update(self.selected)
        info = self.plan_info()
        self.journal.items = [{"id": "demo_plan", "status": "installed", "created_at": datetime.now(timezone.utc).isoformat(),
                               "count": len(self.selected), "account_name": "演示账号", "group_names": info["group_names"],
                               "conversation_names": info["conversation_names"], "conversation_count": info["conversation_count"], "keyword": "演示"}]
        return {"id": identifier, "count": len(self.selected), "status": "installed", "backup": "演示模式：未修改任何微信数据"}

    def restore(self, identifier, recovery=False, progress=lambda *args: None):
        self.deleted_ids.difference_update(self.selected)
        self.journal.items[0]["status"] = "restored"
        return {"id": identifier, "status": "restored"}
