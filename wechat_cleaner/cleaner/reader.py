from __future__ import annotations

import os
from pathlib import Path
import re
import sqlite3
import tempfile
import struct
import uuid

from .database import open_readonly
from .database import authenticated_decrypt
from .files import CleanupError, process_status, safe_path
from .upstream import get_upstream

SUPPORTED_VERSIONS = {"4.2.0.11", "4.2.0.13"}


def account_root(directory=None):
    if directory:
        root = Path(directory).expanduser().resolve()
        if root.name == "db_storage":
            root = root.parent.parent
        elif (root / "db_storage").is_dir():
            root = root.parent
    else:
        detected = get_upstream().auto_detect_db_dir()
        if not detected:
            raise CleanupError("未找到微信数据目录。请登录微信，或填写 xwechat_files 文件夹位置。")
        root = Path(detected).resolve()
    if not root.is_dir():
        raise CleanupError("微信数据目录不存在。")
    return root


def accounts(directory=None):
    root = account_root(directory)
    found = [{"id": p.name, "directory": str(root)} for p in sorted(root.iterdir())
             if p.is_dir() and not p.is_symlink() and p.name.startswith("wxid_")
             and (p / "db_storage/contact/contact.db").is_file()]
    return {"directory": str(root), "accounts": found}


def physical_check(path):
    conn = sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True)
    try:
        virtuals = conn.execute("SELECT name FROM sqlite_master WHERE sql LIKE 'CREATE VIRTUAL TABLE%'").fetchall()
        if not virtuals:
            return conn.execute("PRAGMA quick_check").fetchall() == [("ok",)]
        tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND sql NOT LIKE 'CREATE VIRTUAL TABLE%'")]
        return all(conn.execute('PRAGMA integrity_check("' + name.replace('"', '""') + '")').fetchall() == [("ok",)] for name in tables)
    except sqlite3.Error:
        return False
    finally:
        conn.close()


def require_checkpointed(source, upstream):
    journal = Path(str(source) + "-journal")
    if journal.exists() and journal.stat().st_size:
        raise CleanupError("微信还有待恢复的数据库日志，请重新打开并正常退出微信后重试。")
    wal = Path(str(source) + "-wal")
    if not wal.exists() or wal.stat().st_size <= upstream.WeChatDB.WAL_HEADER_SZ:
        return
    with wal.open("rb") as stream:
        header = stream.read(upstream.WeChatDB.WAL_HEADER_SZ)
        magic, version, page_size = struct.unpack(">III", header[:12])
        if magic not in (0x377f0682, 0x377f0683) or version != 3007000 or page_size != upstream.PAGE_SZ:
            raise CleanupError("写入日志格式不受支持，请保留备份后检查微信版本。")
        generation = header[16:24]
        frame_size = upstream.WeChatDB.WAL_FRAME_SZ
        if (wal.stat().st_size - len(header)) % frame_size:
            raise CleanupError("写入日志存在不完整帧，请重新打开并正常退出微信后重试。")
        for _ in range((wal.stat().st_size - len(header)) // frame_size):
            frame = stream.read(frame_size)
            if frame[8:16] == generation and struct.unpack(">I", frame[:4])[0]:
                # The experimental reader does not validate committed WAL frame
                # checksums. Never use that merger for production writeback.
                raise CleanupError("微信仍有尚未归并的写入日志。请重新打开并正常退出微信后重试；本次备份已保留。")


class LocalReader:
    def __init__(self, account, directory, cache):
        WeChatDB = get_upstream().WeChatDB
        root = account_root(directory)
        if not re.fullmatch(r"wxid_[A-Za-z0-9_]+", account):
            raise CleanupError("请选择有效的本地微信账号。")
        self.source = safe_path(root, account + "/db_storage")
        if not self.source.is_dir():
            raise CleanupError("该账号没有本地数据库。")

        class Reader(WeChatDB):
            def _find_weixin_pids(self):
                import psutil
                return [p.pid for p in psutil.process_iter(["name"])
                        if (p.info["name"] or "").lower() == "weixin.exe"]

            def _save_keys(self):
                pass  # This app keeps extracted keys in memory only.

            _check_merged = staticmethod(physical_check)

        keys_file = Path(tempfile.gettempdir()) / "wechatauto_db" / account / "keys.json"
        try:
            self.db = Reader(account=account, db_dir=str(root), workdir=str(Path(cache) / uuid.uuid4().hex), keys_file=str(keys_file))
        except RuntimeError as exc:
            raise CleanupError("连接未完成。请登录此账号的微信后重试；程序需要读取本机数据库密钥。") from exc
        self.account = account
        self.version = process_status()["version"]
        self.keys = self.db._keys
        self.names = {}
        conn = self.open("contact/contact.db")
        try:
            self.contacts = [dict(r) for r in conn.execute("SELECT username,nick_name,remark FROM contact WHERE delete_flag=0")]
            self.names = {r["username"]: r["remark"] or r["nick_name"] or r["username"] for r in self.contacts}
        finally:
            conn.close()
        self.username = re.sub(r"_[A-Za-z0-9]{4}$", "", account)

    def open(self, relative):
        relative = str(Path(relative)).replace("/", os.sep).replace("\\", os.sep)
        return self.db._open(relative)

    def shards(self):
        return [Path(rel).as_posix().replace("\\", "/") for rel in self.db._message_dbs()]

    def groups(self):
        return sorted([{"id": r["username"], "name": self.names[r["username"]]}
                       for r in self.contacts if r["username"].endswith("@chatroom")], key=lambda g: g["name"].casefold())

    def people(self):
        return sorted([{"id": r["username"], "name": self.names[r["username"]]}
                       for r in self.contacts if r["username"] != self.username
                       and not r["username"].endswith("@chatroom")
                       and not r["username"].startswith("gh_")], key=lambda person: (person["name"].casefold(), person["id"]))

    def info(self):
        return {"account": self.account, "name": self.names.get(self.username, self.account),
                "version": self.version, "supported": self.version in SUPPORTED_VERSIONS,
                "group_count": len(self.groups()), "contact_count": len(self.people())}

    def decode_backup(self, raw, plain, relatives):
        for relative in relatives:
            output = safe_path(plain, relative)
            output.parent.mkdir(parents=True, exist_ok=True)
            key = self.keys.get(str(Path(relative)))
            if not key:
                raise CleanupError(f"数据库没有可用密钥：{relative}")
            source = safe_path(raw, relative)
            upstream = get_upstream()
            require_checkpointed(source, upstream)
            encrypted = source.read_bytes()
            if len(key) != 32 or not upstream._verify_enc_key(key, encrypted[:upstream.PAGE_SZ]):
                raise CleanupError("数据库密钥格式不受支持或已失效，请重新连接微信。")
            mac_key = upstream._pbkdf2(key, bytes(v ^ 0x3A for v in encrypted[:16]), 2)
            output.write_bytes(authenticated_decrypt(encrypted, key, mac_key, upstream))
            if not physical_check(output):
                raise CleanupError(f"备份解密后完整性检查未通过：{relative}")


class SnapshotReader:
    """Read only already-decoded backup files, using the same selection logic."""
    def __init__(self, directory, source_reader):
        self.directory = Path(directory)
        self.names = source_reader.names
        self.account = source_reader.account

    def open(self, relative):
        return open_readonly(safe_path(self.directory, relative))
