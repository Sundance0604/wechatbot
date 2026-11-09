from __future__ import annotations

from datetime import datetime
import hashlib

from .files import CleanupError
from .text import content, row_hash, table_name


def eligibility(item, session, resource):
    if item.get("local_type") != 1:
        return "第一版仅支持普通纯文本消息"
    row = session.execute("SELECT last_msg_locald_id,unread_count,unread_first_msg_srv_id,unread_first_pat_msg_local_id FROM SessionTable WHERE username=?", (item["user"],)).fetchone()
    if row is None:
        return "没有可核验的会话状态"
    if row[0] == item["local_id"]:
        return "会话最后一条消息，暂不支持清理"
    if row[1] or row[2] == item["server_id"] or row[3] == item["local_id"]:
        return "会话涉及未读状态，请在微信中读完后重新查询"
    if session.execute("SELECT 1 FROM SessionUnreadListTable_1 WHERE local_id=? AND server_id=?", (item["local_id"], item["server_id"])).fetchone():
        return "消息仍被未读列表引用"
    if resource.execute("SELECT 1 FROM MessageResourceInfo WHERE message_svr_id=? AND message_local_id=? AND message_create_time=?", (item["server_id"], item["local_id"], item["create_time"])).fetchone():
        return "消息存在媒体资源引用，暂不支持清理"
    return None


def search(reader, group_ids=None, keyword="", match="contains", start=None, end=None, limit=1000, conversation_ids=None):
    keyword = keyword.strip()
    if not keyword:
        raise CleanupError("请输入删除词。")
    available = {item["id"]: {**item, "type": kind} for kind, items in
                 (("group", reader.groups()), ("contact", reader.people())) for item in items}
    if conversation_ids is not None and group_ids is not None:
        raise CleanupError("请使用一组明确的会话选择。")
    selected_ids = conversation_ids if conversation_ids is not None else group_ids
    if not selected_ids or any(user not in available for user in selected_ids):
        raise CleanupError("请从当前账号的联系人或群聊列表中选择会话。")
    if group_ids is not None and any(available[user]["type"] != "group" for user in group_ids):
        raise CleanupError("群聊选择中包含其他会话类型，请重新查询。")
    if start is not None and end is not None and start > end:
        raise CleanupError("开始时间不能晚于结束时间。")
    session = reader.open("session/session.db")
    resource = reader.open("message/message_resource.db")
    results, total = [], 0
    try:
        for shard in reader.shards():
            conn = reader.open(shard)
            try:
                senders = {r[0]: r[1] for r in conn.execute("SELECT rowid,user_name FROM Name2Id")}
                for user in dict.fromkeys(selected_ids):
                    table = table_name(user)
                    if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone():
                        continue
                    sql, args = f'SELECT * FROM "{table}"', []
                    conditions = []
                    if start is not None:
                        conditions.append("create_time>=?")
                        args.append(start)
                    if end is not None:
                        conditions.append("create_time<=?")
                        args.append(end)
                    if conditions:
                        sql += " WHERE " + " AND ".join(conditions)
                    for row in conn.execute(sql + " ORDER BY sort_seq,local_id", args):
                        data = dict(row)
                        text = content(data, group=available[user]["type"] == "group", known_ids=set(senders.values()))
                        if (text != keyword if match == "exact" else keyword not in text):
                            continue
                        total += 1
                        if len(results) >= limit:
                            continue
                        sender = senders.get(data.get("real_sender_id"), "")
                        item = {key: data.get(key) for key in ("local_id", "sort_seq", "server_id", "create_time", "local_type")}
                        item.update(user=user, group_name=available[user]["name"],
                                    conversation_name=available[user]["name"], conversation_type=available[user]["type"],
                                    shard=shard, table=table,
                                    text=text, sender=reader.names.get(sender, sender or "未知发送者"),
                                    row_hash=row_hash(row))
                        identity = f'{reader.account}|{shard}|{table}|{item["local_id"]}|{item["sort_seq"]}|{item["row_hash"]}'
                        item["id"] = hashlib.sha256(identity.encode()).hexdigest()
                        item["blocked_reason"] = eligibility(item, session, resource)
                        results.append(item)
            finally:
                conn.close()
    finally:
        session.close()
        resource.close()
    results.sort(key=lambda item: (item["create_time"], item["shard"], item["local_id"]), reverse=True)
    return {"messages": results, "total": total, "truncated": total > len(results),
            "eligible": sum(not item["blocked_reason"] for item in results)}


def validate_targets(reader, targets):
    session = reader.open("session/session.db")
    resource = reader.open("message/message_resource.db")
    try:
        for item in targets:
            conn = reader.open(item["shard"])
            try:
                rows = list(conn.execute(f'SELECT * FROM "{item["table"]}" WHERE local_id=? AND sort_seq=? AND server_id=?',
                                         (item["local_id"], item["sort_seq"], item["server_id"])))
                if len(rows) != 1 or row_hash(rows[0]) != item["row_hash"]:
                    raise CleanupError("候选消息已经变化，请重新查询后再清理。")
                reason = eligibility({**dict(rows[0]), "user": item["user"]}, session, resource)
                if reason:
                    raise CleanupError(reason)
            finally:
                conn.close()
    finally:
        session.close()
        resource.close()
