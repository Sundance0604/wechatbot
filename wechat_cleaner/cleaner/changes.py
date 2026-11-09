from __future__ import annotations

from collections import defaultdict
from contextlib import closing
from pathlib import Path
import re
import shutil
import sqlite3

from .database import (authenticated_decrypt, encoded_row, encrypt_snapshot,
                       fts_inventory, inventories, open_fts, open_readonly, require_integrity)
from .files import CleanupError, safe_path, sha
from .upstream import get_upstream


def modify_copies(plain, modified, targets):
    changed = []
    by_shard = defaultdict(list)
    for item in targets:
        if not re.fullmatch(r"Msg_[a-f0-9]{32}", item["table"]):
            raise CleanupError("消息表结构不受支持。")
        by_shard[item["shard"]].append(item)
    for rel, items in by_shard.items():
        before = open_readonly(safe_path(plain, rel))
        try:
            require_integrity(before)
            expected = inventories(before)
            for item in items:
                row = before.execute(f'SELECT * FROM "{item["table"]}" WHERE local_id=? AND sort_seq=? AND server_id=?',
                                     (item["local_id"], item["sort_seq"], item["server_id"])).fetchone()
                if row is None:
                    raise CleanupError("备份中没有找到目标消息。")
                expected[item["table"]][encoded_row(row)] -= 1
        finally:
            before.close()
        path = safe_path(modified, rel)
        path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(safe_path(plain, rel), path)
        with closing(sqlite3.connect(path)) as writer, writer:
            writer.execute("PRAGMA journal_mode=DELETE")
            writer.execute("PRAGMA secure_delete=ON")
            writer.execute("BEGIN IMMEDIATE")
            for item in items:
                count = writer.execute(f'DELETE FROM "{item["table"]}" WHERE local_id=? AND sort_seq=? AND server_id=?',
                                       (item["local_id"], item["sort_seq"], item["server_id"])).rowcount
                if count != 1:
                    raise CleanupError("无法唯一定位消息，已停止清理。")
        after = open_readonly(path)
        try:
            require_integrity(after)
            if inventories(after) != {name: +values for name, values in expected.items()}:
                raise CleanupError("检测到非目标记录变化，已停止清理。")
        finally:
            after.close()
        changed.append(rel)

    rel = "message/message_fts.db"
    path = safe_path(modified, rel)
    path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(safe_path(plain, rel), path)
    fts, virtuals, vocabulary = open_fts(path)
    if set(virtuals) != {f"message_fts_v4_{i}" for i in range(4)}:
        fts.close()
        raise CleanupError("全文索引结构不在当前支持范围内。")
    ordinary = [r[0] for r in fts.execute("SELECT name FROM sqlite_master WHERE type='table' AND sql NOT LIKE 'CREATE VIRTUAL TABLE%'")]
    expected = fts_inventory(fts, ordinary)
    removed = defaultdict(set)
    segments = set()
    try:
        fts.execute("PRAGMA journal_mode=DELETE")
        fts.execute("PRAGMA secure_delete=ON")
        fts.execute("BEGIN IMMEDIATE")
        for item in targets:
            sid_row = fts.execute("SELECT rowid FROM name2id WHERE username=?", (item["user"],)).fetchone()
            if sid_row is None:
                raise CleanupError("目标群组没有可核验的搜索索引映射。")
            sid = sid_row[0]
            found = 0
            for i in range(4):
                table = f"message_fts_v4_{i}"
                docs = list(fts.execute(f'SELECT rowid FROM "{table}" WHERE message_local_id=? AND session_id=? AND sort_seq=? AND create_time=?',
                                       (item["local_id"], sid, item["sort_seq"], item["create_time"])))
                for (doc,) in docs:
                    for suffix in ("content", "docsize"):
                        physical = table + "_" + suffix
                        row = fts.execute(f'SELECT * FROM "{physical}" WHERE id=?', (doc,)).fetchone()
                        if row:
                            expected[physical][encoded_row(row)] -= 1
                    fts.execute(f'DELETE FROM "{table}" WHERE rowid=?', (doc,))
                    removed[table].add(doc)
                    segments.update((table + "_data", table + "_idx"))
                    found += 1
                aux = f"message_fts_v4_aux_{i}"
                args = (item["local_id"], sid, item["sort_seq"])
                rows = list(fts.execute(f'SELECT * FROM "{aux}" WHERE message_local_id=? AND session_id=? AND sort_seq=?', args))
                for row in rows:
                    expected[aux][encoded_row(row)] -= 1
                fts.execute(f'DELETE FROM "{aux}" WHERE message_local_id=? AND session_id=? AND sort_seq=?', args)
            if found != 1:
                raise CleanupError("消息没有唯一对应的搜索文档，已停止清理。")
        for index, table in enumerate(virtuals):
            fts.execute(f'INSERT INTO "{table}"("{table}",rank) VALUES(\'integrity-check\',1)')
            actual = list(fts.execute(f'SELECT term,doc,col,offset FROM temp.v{index} ORDER BY doc,offset,term'))
            if actual != [row for row in vocabulary[table] if row[1] not in removed[table]]:
                raise CleanupError("搜索词元校验未通过，已停止清理。")
        if list(fts.execute("PRAGMA integrity_check")) != [("ok",)]:
            raise CleanupError("搜索索引完整性校验未通过。")
        actual = fts_inventory(fts, ordinary)
        if any(actual[name] != +expected[name] for name in ordinary if name not in segments):
            raise CleanupError("搜索辅助数据发生意外变化。")
        fts.execute("COMMIT")
    except BaseException:
        if not fts.get_autocommit():
            fts.execute("ROLLBACK")
        raise
    finally:
        fts.close()
    changed.append(rel)
    return changed


def stage_encrypted(raw, modified, staged, changed, keys, upstream=None):
    if upstream is None:
        upstream = get_upstream()
    changes = []
    for rel in changed:
        key = keys.get(str(Path(rel)))
        if key is None or len(key) != 32:
            raise CleanupError("该数据库的加密密钥格式不在当前写回支持范围内。")
        source = safe_path(raw, rel)
        plain = safe_path(modified, rel).read_bytes()
        encrypted, mac_key = encrypt_snapshot(plain, source.read_bytes()[:upstream.PAGE_SZ], key, upstream)
        if not upstream._verify_enc_key(key, encrypted[:upstream.PAGE_SZ]):
            raise CleanupError("写回文件的加密头校验未通过。")
        recovered = authenticated_decrypt(encrypted, key, mac_key, upstream)
        if recovered != plain:
            raise CleanupError("重新加密回读不一致，已停止清理。")
        output = safe_path(staged, rel)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(encrypted)
        changes.append({"path": rel, "before": sha(source), "after": sha(output)})
    return changes
