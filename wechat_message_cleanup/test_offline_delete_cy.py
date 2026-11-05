"""Delete keyword matches in isolated snapshots; never install files into WeChat.

Validates SQLite integrity, unchanged retained rows, page authentication and a
re-encrypt/decrypt round trip. This is not a verified WeChat write-back tool.
"""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import struct
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def encoded_row(row):
    return json.dumps([
        {"blob": value.hex()} if isinstance(value, bytes) else value
        for value in row
    ], ensure_ascii=True, separators=(",", ":"))


def open_readonly(path):
    conn = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    conn.text_factory = lambda data: data.decode("utf-8") if is_utf8(data) else data
    return conn


def is_utf8(data):
    try:
        data.decode("utf-8")
        return True
    except UnicodeDecodeError:
        return False


def inventories(conn):
    tables = [row[0] for row in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
    )]
    return {
        table: Counter(encoded_row(row) for row in conn.execute(
            'SELECT * FROM "' + table.replace('"', '""') + '"'
        )) for table in tables
    }


def require_integrity(conn):
    result = [row[0] for row in conn.execute("PRAGMA integrity_check")]
    if result != ["ok"]:
        raise RuntimeError(f"SQLite integrity check failed: {result}")
    if list(conn.execute("PRAGMA foreign_key_check")):
        raise RuntimeError("Foreign key check failed")


def encrypt_snapshot(plain, original_header, key, upstream):
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

    page_size, reserve = upstream.PAGE_SZ, upstream.RESERVE_SZ
    if len(plain) % page_size or plain[20] != reserve:
        raise RuntimeError("Unexpected SQLite page size or encryption reserve")
    if int.from_bytes(plain[16:18], "big") != page_size:
        raise RuntimeError("Unexpected page-size header")
    salt = key[32:] if len(key) == 48 else original_header[:16]
    mac_key = upstream._pbkdf2(key[:32], bytes(x ^ 0x3A for x in salt), 2)
    output = bytearray()
    for offset in range(0, len(plain), page_size):
        pgno = offset // page_size + 1
        page = plain[offset:offset + page_size]
        start = 16 if pgno == 1 else 0
        iv = os.urandom(16)
        encryptor = Cipher(algorithms.AES(key[:32]), modes.CBC(iv)).encryptor()
        ciphertext = encryptor.update(page[start:page_size - reserve]) + encryptor.finalize()
        tag = hmac.digest(mac_key, ciphertext + iv + struct.pack("<I", pgno), "sha512")
        output.extend((original_header[:16] if pgno == 1 else b"") + ciphertext + iv + tag)
    return bytes(output), mac_key


def authenticated_decrypt(data, key, mac_key, upstream):
    page_size, reserve = upstream.PAGE_SZ, upstream.RESERVE_SZ
    if not data or len(data) % page_size:
        raise RuntimeError("Invalid encrypted file length")
    output = bytearray()
    for offset in range(0, len(data), page_size):
        pgno = offset // page_size + 1
        page = data[offset:offset + page_size]
        start = 16 if pgno == 1 else 0
        expected = hmac.digest(mac_key, page[start:page_size - 64] + struct.pack("<I", pgno), "sha512")
        if not hmac.compare_digest(expected, page[-64:]):
            raise RuntimeError(f"Page authentication failed: {pgno}")
        output.extend(upstream._decrypt_page(key, page, pgno))
    return bytes(output)


def main(keyword, account, db_dir=None):
    import psutil
    from wechatauto import WeChatDB
    from wechatauto import db as upstream
    from contacts import load_contact_names
    from message_parser import clean_group_content

    class ProcessAwareDB(WeChatDB):
        def _find_weixin_pids(self):
            return [process.pid for process in psutil.process_iter(["name"])
                    if (process.info["name"] or "").lower() == "weixin.exe"]

    run_dir = Path(__file__).resolve().parent / "offline_runs" / datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    run_dir.mkdir(parents=True, exist_ok=False)
    report = {"started_at": datetime.now(timezone.utc).isoformat(), "keyword": keyword,
              "mode": "isolated-copy-only", "status": "running", "shards": [],
              "deleted_from_copies": 0, "deleted_from_originals": 0,
              "client_acceptance_tested": False}
    try:
        db = ProcessAwareDB(account=account, db_dir=db_dir)
        report["account"] = db.account
        names = load_contact_names(db)
        table_users = {"Msg_" + hashlib.md5(user.encode()).hexdigest(): user for user in names}
        for index, shard in enumerate(db._message_dbs()):
            folder = run_dir / f"shard_{index}"
            folder.mkdir()
            source = db._open(shard)
            original = Path(db._db_path(shard))
            key = db._keys[shard]
            with original.open("rb") as stream:
                original_header = stream.read(upstream.PAGE_SZ)
            if not upstream._verify_enc_key(key, original_header):
                raise RuntimeError("Source encryption key verification failed")
            baseline = folder / "before.db"
            try:
                with sqlite3.connect(baseline) as destination:
                    source.backup(destination)
            finally:
                source.close()
            modified = folder / "after.db"
            shutil.copy2(baseline, modified)
            conn = open_readonly(baseline)
            try:
                require_integrity(conn)
                schema_before = list(map(tuple, conn.execute("SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY name")))
                before = inventories(conn)
                expected = {table: counter.copy() for table, counter in before.items()}
                matches = []
                message_tables = [name for name in before if re.fullmatch(r"Msg_[0-9a-fA-F]{32}", name)]
                for table in message_tables:
                    user = table_users.get(table, "")
                    for row in conn.execute(f'SELECT * FROM "{table}"'):
                        item = dict(row)
                        text = clean_group_content(item, names, user)
                        if keyword in text:
                            matches.append((table, item["local_id"], text))
                            expected[table][encoded_row(row)] -= 1
                expected = {table: +counter for table, counter in expected.items()}
            finally:
                conn.close()
            with sqlite3.connect(modified) as writer:
                writer.execute("PRAGMA journal_mode=DELETE")
                writer.execute("PRAGMA foreign_keys=ON")
                writer.execute("BEGIN IMMEDIATE")
                for table, local_id, _ in matches:
                    result = writer.execute(f'DELETE FROM "{table}" WHERE local_id=?', (local_id,))
                    if result.rowcount != 1:
                        raise RuntimeError("Deletion did not uniquely identify one row")
                require_integrity(writer)
            conn = open_readonly(modified)
            try:
                require_integrity(conn)
                if inventories(conn) != expected:
                    raise RuntimeError("Retained rows or auxiliary tables changed unexpectedly")
                if list(map(tuple, conn.execute("SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY name"))) != schema_before:
                    raise RuntimeError("Schema changed unexpectedly")
                remaining = sum(keyword in clean_group_content(dict(row), names, table_users.get(table, ""))
                                for table in message_tables
                                for row in conn.execute(f'SELECT * FROM "{table}"'))
                if remaining:
                    raise RuntimeError("Keyword matches remain in modified snapshot")
            finally:
                conn.close()
            plain = modified.read_bytes()
            encrypted, mac_key = encrypt_snapshot(plain, original_header, key, upstream)
            if not upstream._verify_enc_key(key, encrypted[:upstream.PAGE_SZ]):
                raise RuntimeError("Re-encrypted first page rejected by original reader")
            recovered = authenticated_decrypt(encrypted, key, mac_key, upstream)
            for offset in range(0, len(plain), upstream.PAGE_SZ):
                end = offset + upstream.PAGE_SZ - upstream.RESERVE_SZ
                if recovered[offset:end] != plain[offset:end]:
                    raise RuntimeError("Encryption round trip changed page data")
            damaged = bytearray(encrypted)
            damaged[-upstream.PAGE_SZ + 32] ^= 1
            try:
                authenticated_decrypt(bytes(damaged), key, mac_key, upstream)
            except RuntimeError:
                corruption_rejected = True
            else:
                raise RuntimeError("Tampered encrypted page was accepted")
            (folder / "after.encrypted.db").write_bytes(encrypted)
            roundtrip_path = folder / "roundtrip.db"
            roundtrip_path.write_bytes(recovered)
            conn = open_readonly(roundtrip_path)
            try:
                require_integrity(conn)
                if inventories(conn) != expected:
                    raise RuntimeError("Reopened round-trip database has unexpected records")
            finally:
                conn.close()
            result = {"shard": str(shard), "before_messages": sum(sum(before[t].values()) for t in message_tables),
                      "after_messages": sum(sum(expected[t].values()) for t in message_tables),
                      "deleted_from_copy": len(matches), "remaining_keyword_matches": remaining,
                      "sqlite_integrity": "ok", "retained_rows_unchanged": True,
                      "schema_unchanged": True, "encrypted_pages_verified": len(encrypted) // upstream.PAGE_SZ,
                      "roundtrip_verified": True, "tampered_page_rejected": corruption_rejected,
                      "output_folder": str(folder)}
            report["shards"].append(result)
            report["deleted_from_copies"] += len(matches)
            print(json.dumps({k: v for k, v in result.items() if k != "output_folder"}), flush=True)
        report["status"] = "copy_experiment_passed" if report["shards"] else "no_message_shards"
    except Exception as exc:
        report["status"] = "failed"
        report["reason"] = f"{type(exc).__name__}: {exc}"
    finally:
        report["finished_at"] = datetime.now(timezone.utc).isoformat()
        report_path = run_dir / "result.json"
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps({"status": report["status"], "report": str(report_path),
                          "deleted_from_copies": report["deleted_from_copies"],
                          "deleted_from_originals": 0, "reason": report.get("reason")}), flush=True)
    return 0 if report["status"] == "copy_experiment_passed" else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--keyword", default="cy")
    parser.add_argument("--account", required=True, help="Explicit local account folder name")
    parser.add_argument("--db-dir", help="Directory containing local account folders")
    args = parser.parse_args()
    if not args.keyword:
        parser.error("keyword must not be empty")
    raise SystemExit(main(args.keyword, args.account, args.db_dir))
