import ctypes
import os
from pathlib import Path
import re
import sys

import apsw
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from cleaner.database import encrypt_snapshot, open_readonly
from cleaner.reader import LocalReader
from cleaner.service import CleanerService
from cleaner.text import table_name
from cleaner.upstream import get_upstream

ACCOUNT = "wxid_fixture_abcd"
GROUP = "123@chatroom"
OTHER = "456@chatroom"
FRIEND = "wxid_friend"


def database(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = apsw.Connection(str(path))
    reserve = ctypes.c_int(80)
    conn.file_control("main", apsw.SQLITE_FCNTL_RESERVE_BYTES, ctypes.addressof(reserve))
    conn.execute("VACUUM")
    assert path.read_bytes()[20] == 80
    return conn


@pytest.fixture
def environment(tmp_path):
    upstream = get_upstream()
    plain = tmp_path / "fixture_plain"
    source = tmp_path / "accounts" / ACCOUNT / "db_storage"
    c = database(plain / "contact/contact.db")
    c.execute("CREATE TABLE contact(username TEXT PRIMARY KEY,nick_name,remark,delete_flag)")
    for user, name in (("wxid_fixture", "测试本人"), ("wxid_sender", "测试成员"), (GROUP, "测试群"), (OTHER, "测试群"), (FRIEND, "测试群"), ("gh_official_test", "公众号")):
        c.execute("INSERT INTO contact VALUES(?,?,?,0)", (user, name, ""))
    c.close()
    c = database(plain / "session/session.db")
    c.execute("CREATE TABLE SessionTable(username TEXT PRIMARY KEY,last_msg_locald_id,unread_count,unread_first_msg_srv_id,unread_first_pat_msg_local_id)")
    c.execute("CREATE TABLE SessionUnreadListTable_1(local_id,server_id)")
    c.execute("INSERT INTO SessionTable VALUES(?,3,0,0,0)", (GROUP,))
    c.execute("INSERT INTO SessionTable VALUES(?,2,0,0,0)", (OTHER,))
    c.execute("INSERT INTO SessionTable VALUES(?,2,0,0,0)", (FRIEND,))
    c.close()
    c = database(plain / "message/message_resource.db")
    c.execute("CREATE TABLE MessageResourceInfo(message_svr_id,message_local_id,message_create_time)")
    c.close()
    c = database(plain / "message/message_0.db")
    c.execute("CREATE TABLE Name2Id(user_name TEXT PRIMARY KEY)")
    c.execute("INSERT INTO Name2Id VALUES('wxid_sender')")
    c.execute("INSERT INTO Name2Id VALUES('wxid_fixture')")
    c.execute("INSERT INTO Name2Id VALUES(?)", (FRIEND,))
    records = [(1,101,1,1001,1,1700000001,"wxid_sender:\n草是测试词"),
               (2,102,1,1002,1,1700000002,"保留的消息"),
               (3,103,1,1003,1,1700000003,"草，最后一条"),
               (4,104,49,1004,1,1700000004,"草，附件")]
    for user in (GROUP, OTHER, FRIEND):
        c.execute(f'CREATE TABLE "{table_name(user)}"(local_id INTEGER PRIMARY KEY,server_id,local_type,sort_seq,real_sender_id,create_time,message_content,compress_content)')
    for row in records:
        c.execute(f'INSERT INTO "{table_name(GROUP)}" VALUES(?,?,?,?,?,?,?,NULL)', row)
    c.execute(f'INSERT INTO "{table_name(OTHER)}" VALUES(1,201,1,2001,1,1700000001,\'草是测试词\',NULL)')
    private_records = [(1,301,1,3001,3,1700000001,"wxid_sender:\n草是测试词"),
                       (2,302,1,3002,2,1700000002,"私聊最后一条"),
                       (3,303,1,3000,3,1699999999,"草是测试词")]
    for row in private_records:
        c.execute(f'INSERT INTO "{table_name(FRIEND)}" VALUES(?,?,?,?,?,?,?,NULL)', row)
    c.close()
    c = database(plain / "message/message_fts.db")

    def factory(conn, args):
        def tokenize(data, flags, locale):
            text = data.decode()
            for match in re.finditer(r"\w", text):
                yield len(text[:match.start()].encode()), len(text[:match.end()].encode()), match.group().lower()
        return tokenize
    c.register_fts5_tokenizer("MMFtsTokenizer", factory)
    c.execute("CREATE TABLE name2id(username TEXT PRIMARY KEY)")
    c.execute("INSERT INTO name2id VALUES(?)", (GROUP,))
    c.execute("INSERT INTO name2id VALUES(?)", (OTHER,))
    c.execute("INSERT INTO name2id VALUES(?)", (FRIEND,))
    for i in range(4):
        c.execute(f"CREATE VIRTUAL TABLE message_fts_v4_{i} USING fts5(acontent,message_local_id UNINDEXED,session_id UNINDEXED,sort_seq UNINDEXED,create_time UNINDEXED,local_type UNINDEXED,message_svr_id UNINDEXED,tokenize='MMFtsTokenizer')")
        c.execute(f"CREATE TABLE message_fts_v4_aux_{i}(message_local_id,sort_seq,session_id)")
    for row in records:
        local_id, server, kind, seq, sender, created, text = row
        c.execute("INSERT INTO message_fts_v4_0 VALUES(?,?,?,?,?,?,?)", (text.split(":\n")[-1], local_id, 1, seq, created, kind, server))
        c.execute("INSERT INTO message_fts_v4_aux_0 VALUES(?,?,1)", (local_id, seq))
    c.execute("INSERT INTO message_fts_v4_0 VALUES('草是测试词',1,2,2001,1700000001,1,201)")
    c.execute("INSERT INTO message_fts_v4_aux_0 VALUES(1,2001,2)")
    for row in private_records:
        local_id, server, kind, seq, sender, created, text = row
        c.execute("INSERT INTO message_fts_v4_0 VALUES(?,?,?,?,?,?,?)", (text, local_id, 3, seq, created, kind, server))
        c.execute("INSERT INTO message_fts_v4_aux_0 VALUES(?,?,3)", (local_id, seq))
    c.close()
    keys = {}
    for path in plain.rglob("*.db"):
        rel = str(path.relative_to(plain))
        key = os.urandom(32)
        keys[rel] = key
        encrypted, _ = encrypt_snapshot(path.read_bytes(), os.urandom(upstream.PAGE_SZ), key, upstream)
        out = source / rel
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(encrypted)

    class FixtureReader(LocalReader):
        def __init__(self, account, directory, cache):
            WeChatDB = get_upstream().WeChatDB
            self.account, self.source, self.keys = ACCOUNT, source, keys
            self.version = "4.2.0.13"
            self.db = WeChatDB.__new__(WeChatDB)
            self.names = {GROUP: "测试群", OTHER: "测试群", FRIEND: "测试群", "wxid_sender": "测试成员", "wxid_fixture": "测试本人", "gh_official_test": "公众号"}
            self.username = "wxid_fixture"
            self.contacts = [{"username": user} for user in self.names]
            self.cache = Path(cache)

        def open(self, rel):
            rel = str(Path(rel))
            path = self.cache / rel
            self.db._decrypt_file(str(self.source / rel), str(path), self.keys[rel])
            return open_readonly(path)

        def shards(self):
            return ["message/message_0.db"]

    service = CleanerService(tmp_path / "app", reader_factory=FixtureReader, stopped=lambda: None)
    service.connect(ACCOUNT)
    return service, source, GROUP, OTHER
