import os
import shutil
import sqlite3

import pytest

from cleaner.files import CleanupError, hashes, read, save
from cleaner.journal import Journal
from cleaner.text import table_name


def scan(environment):
    service, source, group, other = environment
    result = service.search({"group_ids": [group], "keyword": "草", "match": "contains"})
    ids = [m["id"] for m in result["messages"] if not m["blocked_reason"]]
    return result, ids


def prepared(environment):
    service = environment[0]
    result, ids = scan(environment)
    return service.prepare(result["scan_id"], ids)


def test_encrypted_group_cleanup_and_exact_restore(environment):
    service, source, group, other = environment
    baseline = hashes(source)
    result, ids = scan(environment)
    assert result["total"] == 3 and len(ids) == 1
    assert {m["user"] for m in result["messages"]} == {group}
    assert any("最后一条" in (m["blocked_reason"] or "") for m in result["messages"])
    plan = service.prepare(result["scan_id"], ids)
    assert hashes(source) == baseline
    assert not (service.journal.root / plan["id"] / "plain").exists()
    assert not (service.journal.root / plan["id"] / "modified").exists()
    assert plan["files"] == 2
    service.execute(plan["id"])
    conn = service.reader.open("message/message_0.db")
    assert conn.execute(f'SELECT local_id FROM "{table_name(group)}" ORDER BY local_id').fetchall()[0][0] == 2
    assert conn.execute(f'SELECT COUNT(*) FROM "{table_name(other)}"').fetchone()[0] == 1
    conn.close()
    assert len([name for name in baseline if hashes(source)[name] != baseline[name]]) == 2
    assert service.restore(plan["id"])["status"] == "restored"
    assert hashes(source) == baseline


def test_blocked_and_forged_selections_are_rejected(environment):
    service = environment[0]
    result, ids = scan(environment)
    blocked = next(m["id"] for m in result["messages"] if m["blocked_reason"])
    with pytest.raises(CleanupError):
        service.prepare(result["scan_id"], [blocked])
    with pytest.raises(CleanupError):
        service.prepare(result["scan_id"], ["invented"])
    with pytest.raises(CleanupError):
        service.prepare(result["scan_id"], ids + ids)
    with pytest.raises(CleanupError):
        service.search({"group_ids": ["123@chatroom' OR 1=1"], "keyword": "草"})


def test_changed_source_blocks_install_and_restore(environment):
    service, source, *_ = environment
    plan = prepared(environment)
    (source / "new-data").write_text("new message")
    with pytest.raises(CleanupError, match="发生了变化"):
        service.execute(plan["id"])
    (source / "new-data").unlink()
    service.execute(plan["id"])
    (source / "new-data").write_text("new message")
    with pytest.raises(CleanupError, match="新"):
        service.restore(plan["id"])
    assert (source / "new-data").read_text() == "new message"


def test_backup_and_stage_corruption_stop_install(environment):
    service = environment[0]
    plan = prepared(environment)
    run = service.journal.root / plan["id"]
    stage = run / "staged/message/message_fts.db"
    data = stage.read_bytes()
    stage.write_bytes(data[:-1] + bytes([data[-1] ^ 1]))
    with pytest.raises(CleanupError, match="校验"):
        service.execute(plan["id"])
    stage.write_bytes(data)
    backup = run / "backup/session/session.db"
    backup.write_bytes(b"damaged")
    with pytest.raises(CleanupError, match="备份"):
        service.execute(plan["id"])


def test_second_file_failure_rolls_back_all_files_and_sidecars(environment):
    service, source, *_ = environment
    # Opaque sidecars are backed up and restored byte-for-byte; their fake values
    # are shorter than WAL headers, so they are not decoded as WAL frames.
    for suffix in ("-wal", "-shm", "-journal"):
        (source / ("message/message_0.db" + suffix)).write_bytes(b"fixture" if suffix != "-journal" else b"")
    baseline = hashes(source)
    plan = prepared(environment)
    calls = 0

    def fail_once(src, dst):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("simulated replacement failure")
        return os.replace(src, dst)

    service.journal.replace = fail_once
    with pytest.raises(OSError):
        service.execute(plan["id"])
    assert hashes(source) == baseline
    assert read(service.journal.root / plan["id"] / "plan.json")["status"] == "rolled_back"


def test_restart_recovery_with_partially_copied_temporary_file(environment):
    service, source, *_ = environment
    baseline = hashes(source)
    info = prepared(environment)
    run = service.journal.root / info["id"]
    plan = read(run / "plan.json")
    first = plan["changes"][0]["path"]
    shutil.copy2(run / "staged" / first, source / first)
    second = plan["changes"][1]["path"]
    temp = service.journal._temporary(source, second, info["id"])
    temp.write_bytes(b"interrupted copy")
    plan["status"] = "applying"
    save(run / "plan.json", plan)
    restarted = Journal(service.journal.root, stopped=lambda: None)
    assert restarted.pending()
    assert restarted.recover(info["id"])["status"] == "rolled_back"
    assert hashes(source) == baseline


def test_recovery_refuses_unexpected_data_changes(environment):
    service, source, *_ = environment
    info = prepared(environment)
    run = service.journal.root / info["id"]
    plan = read(run / "plan.json")
    plan["status"] = "applying"
    save(run / "plan.json", plan)
    (source / "new-records").write_text("preserve")
    with pytest.raises(CleanupError, match="计划以外"):
        service.journal.recover(info["id"])
    assert (source / "new-records").read_text() == "preserve"


def test_changed_target_invalidates_preview(environment):
    service, source, group, *_ = environment
    result, ids = scan(environment)
    from cleaner.database import encrypt_snapshot
    from cleaner.upstream import get_upstream
    upstream = get_upstream()
    rel = "message/message_0.db"
    cached = service.reader.open(rel)
    cached.close()
    plain_path = service.reader.cache / rel
    with sqlite3.connect(plain_path) as conn:
        conn.execute(f'UPDATE "{table_name(group)}" SET message_content=? WHERE local_id=1', ("草，已编辑",))
    original = (source / rel).read_bytes()
    encrypted, _ = encrypt_snapshot(plain_path.read_bytes(), original[:upstream.PAGE_SZ], service.reader.keys[str(__import__("pathlib").Path(rel))], upstream)
    (source / rel).write_bytes(encrypted)
    changed_baseline = hashes(source)
    with pytest.raises(CleanupError, match="已经变化"):
        service.prepare(result["scan_id"], ids)
    assert hashes(source) == changed_baseline


def test_unread_resource_and_version_guards(environment):
    service, source, group, *_ = environment
    result, ids = scan(environment)
    service.reader.version = "9.9.9"
    with pytest.raises(CleanupError, match="版本"):
        service.prepare(result["scan_id"], ids)
    service.reader.version = "4.2.0.13"
    service.stopped = lambda: (_ for _ in ()).throw(CleanupError("微信仍在运行"))
    with pytest.raises(CleanupError, match="仍在运行"):
        service.prepare(result["scan_id"], ids)


def test_exact_and_date_scopes(environment):
    service, source, group, *_ = environment
    result = service.search({"group_ids": [group], "keyword": "草是测试词", "match": "exact"})
    assert result["total"] == 1
    result = service.search({"group_ids": [group], "keyword": "草", "start":1700000003, "end":1700000004})
    assert result["total"] == 2 and result["eligible"] == 0
    with pytest.raises(CleanupError):
        service.search({"group_ids": [group], "keyword": "草", "start":5, "end":1})


def test_current_generation_wal_and_hot_journal_block_writeback(environment):
    import struct
    service, source, *_ = environment
    result, ids = scan(environment)
    from cleaner.upstream import get_upstream
    upstream = get_upstream()
    salt = os.urandom(8)
    header = struct.pack(">IIII",0x377f0682,3007000,4096,0) + salt + bytes(8)
    frame = struct.pack(">II",1,1) + salt + bytes(8) + bytes(4096)
    wal = source / "message/message_0.db-wal"
    wal.write_bytes(header + frame)
    before = hashes(source)
    with pytest.raises(CleanupError, match="归并"):
        service.prepare(result["scan_id"], ids)
    assert hashes(source) == before
    wal.unlink()
    (source / "message/message_0.db-journal").write_bytes(b"pending recovery")
    with pytest.raises(CleanupError, match="日志"):
        service.prepare(result["scan_id"], ids)


def test_plain_text_with_colon_is_not_mistaken_for_sender():
    from cleaner.text import content
    assert content({"message_content":"备注:\n草是测试词"}, True, {"wxid_sender"}) == "备注:\n草是测试词"


def test_plaintext_reader_cache_is_removed_on_close(environment):
    service = environment[0]
    scan(environment)
    assert (service.data_dir / "cache").exists()
    service.close()
    assert not (service.data_dir / "cache").exists()


def test_contacts_are_separate_from_groups_self_and_official_accounts(environment):
    service = environment[0]
    contacts = service.contacts()
    assert {c["id"] for c in contacts} == {"wxid_sender", "wxid_friend"}
    conversations = service.conversations()
    assert len(conversations) == 4
    assert {c["type"] for c in conversations} == {"contact", "group"}


def test_private_search_preserves_literal_sender_like_prefix(environment):
    service = environment[0]
    result = service.search({"conversation_ids": ["wxid_friend"], "keyword": "wxid_sender:\n草是测试词", "match": "exact"})
    assert result["total"] == 1
    assert result["messages"][0]["conversation_type"] == "contact"
    assert result["messages"][0]["sender"] == "测试群"
    assert result["messages"][0]["text"] == "wxid_sender:\n草是测试词"


def test_private_cleanup_preserves_unselected_groups_and_restores(environment):
    service, source, group, other = environment
    baseline = hashes(source)
    result = service.search({"conversation_ids": ["wxid_friend"], "keyword": "草"})
    assert result["total"] == result["eligible"] == 2
    plan = service.prepare(result["scan_id"], [m["id"] for m in result["messages"]])
    assert plan["conversation_count"] == 1 and plan["conversations"][0]["type"] == "contact"
    service.execute(plan["id"])
    conn = service.reader.open("message/message_0.db")
    try:
        assert [row[0] for row in conn.execute(f'SELECT local_id FROM "{table_name("wxid_friend")}"')] == [2]
        assert conn.execute(f'SELECT COUNT(*) FROM "{table_name(group)}"').fetchone()[0] == 4
        assert conn.execute(f'SELECT COUNT(*) FROM "{table_name(other)}"').fetchone()[0] == 1
    finally:
        conn.close()
    service.restore(plan["id"])
    assert hashes(source) == baseline


def test_mixed_same_name_conversations_keep_separate_identities(environment):
    service, source, group, other = environment
    baseline = hashes(source)
    result = service.search({"conversation_ids": [group, "wxid_friend"], "keyword": "草"})
    assert result["total"] == 5 and result["eligible"] == 3
    selected = [m for m in result["messages"] if not m["blocked_reason"]]
    assert len({m["id"] for m in selected}) == 3
    assert len({m["conversation_name"] for m in selected}) == 1
    plan = service.prepare(result["scan_id"], [m["id"] for m in selected])
    assert plan["conversation_count"] == 2 and len(plan["conversation_names"]) == 2
    service.execute(plan["id"])
    conn = service.reader.open("message/message_0.db")
    try:
        assert conn.execute(f'SELECT COUNT(*) FROM "{table_name(group)}"').fetchone()[0] == 3
        assert conn.execute(f'SELECT COUNT(*) FROM "{table_name("wxid_friend")}"').fetchone()[0] == 1
        assert conn.execute(f'SELECT COUNT(*) FROM "{table_name(other)}"').fetchone()[0] == 1
    finally:
        conn.close()
    service.restore(plan["id"])
    assert hashes(source) == baseline


def test_foreign_contacts_and_conflicting_selection_fields_are_rejected(environment):
    service, source, group, other = environment
    with pytest.raises(CleanupError):
        service.search({"conversation_ids": ["wxid_not_in_account"], "keyword": "草"})
    with pytest.raises(CleanupError):
        service.search({"conversation_ids": [group], "group_ids": [other], "keyword": "草"})
