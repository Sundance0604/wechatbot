"""Prepare and install a backed-up, backend-only cleanup for one stopped account.

Never runs UI automation. Replays existing FTS tokens only for deleting rows;
does not replace WeChat's tokenizer for future inserts or change its schema.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from datetime import datetime, timezone
import hashlib
import hmac
import json
from pathlib import Path
import re
import shutil
import sqlite3
import sys

import apsw

from prepare_offline_writeback import require_stopped
from test_offline_delete_cy import (
    authenticated_decrypt, encoded_row, encrypt_snapshot, inventories,
    open_readonly, require_integrity,
)

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from message_parser import clean_group_content

ACCOUNT = None
SOURCE = None
DB_ROOT = None
RUNS = Path(__file__).resolve().parent / "writeback_runs"


def configure_target(account, db_dir):
    """Require an explicit local account/data root; never publish a machine-specific target."""
    global ACCOUNT, SOURCE, DB_ROOT
    if not re.fullmatch(r"[A-Za-z0-9_@.-]{1,100}", account) or account in (".", ".."):
        raise ValueError("Account must be one local folder name")
    DB_ROOT = Path(db_dir).expanduser().resolve()
    ACCOUNT = account
    SOURCE = (DB_ROOT / account / "db_storage").resolve()
    if not SOURCE.is_relative_to(DB_ROOT):
        raise ValueError("Account escapes selected data directory")



def sha(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def save(path, value):
    Path(path).write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def checked_source(rel):
    if SOURCE is None:raise RuntimeError("Configure an explicit target first")
    path = (SOURCE / rel).resolve()
    if not path.is_relative_to(SOURCE.resolve()):
        raise RuntimeError("Path escapes the selected account")
    return path


def checked_run(run):
    run = Path(run).resolve()
    if not run.is_relative_to(RUNS.resolve()):
        raise RuntimeError("Run directory must be in the cleanup workspace")
    manifest = json.loads((run / "manifest.json").read_text(encoding="utf-8"))
    if Path(manifest["source_db_storage"]).resolve() != SOURCE.resolve() or manifest["account"] != ACCOUNT:
        raise RuntimeError("Unexpected account or database directory")
    return run, manifest


def fts_inventory(conn, names):
    return {name: Counter(encoded_row(row) for row in conn.execute(f'SELECT * FROM "{name}"'))
            for name in names}


def open_fts(path):
    """Build an exact token replay map from the existing index, without guessing segmentation."""
    conn = apsw.Connection(str(path))
    replay = {}

    def factory(connection, args):
        def tokenize(data, flags, locale):
            if data not in replay:
                raise RuntimeError("No exact original token stream for this text")
            for tokens in replay[data]:
                yield 0, len(data), *tokens
        return tokenize

    conn.register_fts5_tokenizer("MMFtsTokenizer", factory)
    virtuals = [row[0] for row in conn.execute(
        "SELECT name FROM sqlite_master WHERE sql LIKE 'CREATE VIRTUAL TABLE%fts5%' ORDER BY name"
    )]
    vocabularies = {}
    for i, table in enumerate(virtuals):
        conn.execute(f"CREATE VIRTUAL TABLE temp.v{i} USING fts5vocab(main, '{table}', 'instance')")
        tokens = list(conn.execute(f"SELECT term,doc,col,offset FROM temp.v{i} ORDER BY doc,offset,term"))
        vocabularies[table] = tokens
        positions = defaultdict(lambda: defaultdict(list))
        for term, doc, col, offset in tokens:
            if col != "acontent":
                raise RuntimeError("Unexpected indexed column")
            positions[doc][offset].append(term)
        for doc, text in conn.execute(f'SELECT rowid,acontent FROM "{table}"'):
            data = text.encode("utf-8") if isinstance(text, str) else text
            sequence = positions.get(doc, {})
            if sequence and sorted(sequence) != list(range(max(sequence) + 1)):
                raise RuntimeError("Token offsets cannot be replayed exactly")
            stream = [tuple(sequence[offset]) for offset in sorted(sequence)]
            if data in replay and replay[data] != stream:
                raise RuntimeError("Identical text has conflicting token streams")
            replay[data] = stream
    return conn, virtuals, vocabularies


def build(run):
    from wechatauto import WeChatDB
    from wechatauto import db as upstream
    run, manifest = checked_run(run)
    require_stopped()
    for rel, value in manifest["original_hashes"].items():
        if sha(checked_source(rel)) != value or sha(run / "backup_db_storage" / rel) != value:
            raise RuntimeError("Original or backup changed; prepare a new backup")
    modified_dir = run / "modified"
    modified_dir.mkdir(exist_ok=False)
    contact = open_readonly(run / "decrypted/contact/contact.db")
    try:
        names = {row[0]: row[2] or row[1] or row[0]
                 for row in contact.execute("SELECT username,nick_name,remark FROM contact") if row[0]}
    finally:
        contact.close()
    table_users = {"Msg_" + hashlib.md5(user.encode()).hexdigest(): user for user in names}
    targets = []
    changed = []
    for before_path in sorted((run / "decrypted/message").glob("message_[0-9]*.db")):
        rel = str(before_path.relative_to(run / "decrypted"))
        before_conn = open_readonly(before_path)
        try:
            require_integrity(before_conn)
            before = inventories(before_conn)
            expected = {table: values.copy() for table, values in before.items()}
            local_targets = []
            for table in before:
                if not re.fullmatch(r"Msg_[a-fA-F0-9]{32}", table):
                    continue
                user = table_users.get(table)
                if not user:
                    raise RuntimeError("Conversation is not mapped to a verified username")
                for row in before_conn.execute(f'SELECT * FROM "{table}"'):
                    item = dict(row)
                    if "cy" in clean_group_content(item, names, user):
                        local_targets.append({"shard": rel, "table": table, "user": user,
                                              "local_id": item["local_id"], "sort_seq": item["sort_seq"],
                                              "create_time": item["create_time"], "server_id": item["server_id"]})
                        expected[table][encoded_row(row)] -= 1
        finally:
            before_conn.close()
        if not local_targets:
            continue
        after_path = modified_dir / rel
        after_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(before_path, after_path)
        with sqlite3.connect(after_path) as writer:
            writer.execute("PRAGMA journal_mode=DELETE")
            writer.execute("PRAGMA secure_delete=ON")
            writer.execute("BEGIN IMMEDIATE")
            for target in local_targets:
                result = writer.execute(f'DELETE FROM "{target["table"]}" WHERE local_id=? AND sort_seq=?',
                                        (target["local_id"], target["sort_seq"]))
                if result.rowcount != 1:
                    raise RuntimeError("Message identity is not unique")
        conn = open_readonly(after_path)
        try:
            require_integrity(conn)
            if inventories(conn) != {table: +values for table, values in expected.items()}:
                raise RuntimeError("Non-target message or auxiliary data changed")
        finally:
            conn.close()
        targets.extend(local_targets)
        changed.append(rel)
    if len(targets) != 2:
        raise RuntimeError("This reviewed experiment expects exactly the two previously identified messages")
    # Only this sample is supported: no last-message, unread or media references.
    session = open_readonly(run / "decrypted/session/session.db")
    resource = open_readonly(run / "decrypted/message/message_resource.db")
    try:
        for target in targets:
            row = session.execute("SELECT last_msg_locald_id,unread_count,unread_first_msg_srv_id,unread_first_pat_msg_local_id FROM SessionTable WHERE username=?", (target["user"],)).fetchone()
            if row and (row[0] == target["local_id"] or row[1] or row[2] == target["server_id"] or row[3] == target["local_id"]):
                raise RuntimeError("Session metadata needs additional updates; no write-back")
            if session.execute("SELECT 1 FROM SessionUnreadListTable_1 WHERE local_id=? AND server_id=?", (target["local_id"], target["server_id"])).fetchone():
                raise RuntimeError("Message has an unread-list reference")
            if resource.execute("SELECT 1 FROM MessageResourceInfo WHERE message_svr_id=? AND message_local_id=? AND message_create_time=?", (target["server_id"],target["local_id"],target["create_time"])).fetchone():
                raise RuntimeError("Message has a media resource reference")
    finally:
        session.close()
        resource.close()
    # Use FTS5's own delete operation with original indexed tokens.
    fts_rel = "message\\message_fts.db"
    fts_path = modified_dir / fts_rel
    shutil.copy2(run / "decrypted" / fts_rel, fts_path)
    fts, virtuals, before_vocab = open_fts(fts_path)
    ordinary = [row[0] for row in fts.execute("SELECT name FROM sqlite_master WHERE type='table' AND sql NOT LIKE 'CREATE VIRTUAL TABLE%' ORDER BY name")]
    baseline = fts_inventory(fts, ordinary)
    expected_fts = {table: rows.copy() for table, rows in baseline.items()}
    removed_docs = defaultdict(set)
    changed_blocks = set()
    try:
        fts.execute("PRAGMA journal_mode=DELETE")
        fts.execute("PRAGMA secure_delete=ON")
        fts.execute("BEGIN IMMEDIATE")
        for target in targets:
            sid = fts.execute("SELECT rowid FROM name2id WHERE username=?", (target["user"],)).fetchone()[0]
            found = 0
            for i in range(4):
                table = f"message_fts_v4_{i}"
                records = list(fts.execute(f'SELECT rowid FROM "{table}" WHERE message_local_id=? AND session_id=? AND sort_seq=? AND create_time=?',
                                           (target["local_id"],sid,target["sort_seq"],target["create_time"])))
                for (doc,) in records:
                    for suffix, column in (("content", "id"), ("docsize", "id")):
                        physical = f"{table}_{suffix}"
                        row = fts.execute(f'SELECT * FROM "{physical}" WHERE {column}=?', (doc,)).fetchone()
                        if row:
                            expected_fts[physical][encoded_row(row)] -= 1
                    fts.execute(f'DELETE FROM "{table}" WHERE rowid=?', (doc,))
                    removed_docs[table].add(doc)
                    changed_blocks.update((table + "_data", table + "_idx"))
                    found += 1
                aux = f"message_fts_v4_aux_{i}"
                rows = list(fts.execute(f'SELECT * FROM "{aux}" WHERE message_local_id=? AND session_id=? AND sort_seq=?',
                                       (target["local_id"],sid,target["sort_seq"])))
                for row in rows:
                    expected_fts[aux][encoded_row(row)] -= 1
                fts.execute(f'DELETE FROM "{aux}" WHERE message_local_id=? AND session_id=? AND sort_seq=?',
                            (target["local_id"],sid,target["sort_seq"]))
            if found != 1:
                raise RuntimeError("Target does not have exactly one verified full-text document")
        for table in virtuals:
            fts.execute(f'INSERT INTO "{table}"("{table}",rank) VALUES(\'integrity-check\',1)')
        if list(fts.execute("PRAGMA integrity_check")) != [("ok",)]:
            raise RuntimeError("Search database integrity failed")
        for index, table in enumerate(virtuals):
            after = list(fts.execute(f'SELECT term,doc,col,offset FROM temp.v{index} ORDER BY doc,offset,term'))
            if after != [row for row in before_vocab[table] if row[1] not in removed_docs[table]]:
                raise RuntimeError("Retained search tokens changed or target tokens remain")
        observed = fts_inventory(fts, ordinary)
        for table in ordinary:
            if table not in changed_blocks and observed[table] != +expected_fts[table]:
                raise RuntimeError(f"Unexpected change in search table {table}")
        fts.execute("COMMIT")
    except BaseException:
        fts.execute("ROLLBACK")
        raise
    finally:
        fts.close()
    changed.append(fts_rel)
    db = WeChatDB(account=ACCOUNT, db_dir=str(DB_ROOT))
    staged = run / "staged_encrypted"
    changes = []
    for rel in changed:
        plain = (modified_dir / rel).read_bytes()
        header = (run / "backup_db_storage" / rel).read_bytes()[:upstream.PAGE_SZ]
        encrypted, mac_key = encrypt_snapshot(plain, header, db._keys[rel], upstream)
        if not upstream._verify_enc_key(db._keys[rel], encrypted[:upstream.PAGE_SZ]):
            raise RuntimeError("Original reader rejected the staged encryption key or header")
        recovered = authenticated_decrypt(encrypted, db._keys[rel], mac_key, upstream)
        if recovered != plain:
            raise RuntimeError("Encrypted round-trip mismatch")
        destination = staged / rel
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(encrypted)
        changes.append({"relative_path": rel, "before_sha256": manifest["original_hashes"][rel],
                        "after_sha256": sha(destination), "staged": str(destination)})
    require_stopped()
    plan = {"account": ACCOUNT, "keyword": "cy", "deleted_messages": len(targets),
            "targets": targets, "changes": changes, "backup_verified": True,
            "remaining_rows_unchanged": True, "fts_remaining_tokens_unchanged": True,
            "fts_integrity_verified": True, "session_and_resource_updates_needed": False,
            "status": "prepared", "client_acceptance_tested": False}
    save(run / "plan.json", plan)
    print(json.dumps({"status": "prepared", "messages": len(targets), "files": len(changes), "run": str(run)}))


def install(run, restore=False):
    import os

    run, manifest = checked_run(run)
    plan = json.loads((run / "plan.json").read_text(encoding="utf-8"))
    require_stopped()
    expected_rels = {"message\\message_0.db", "message\\message_fts.db"}
    if plan["account"] != ACCOUNT or plan["deleted_messages"] != 2 or {c["relative_path"] for c in plan["changes"]} != expected_rels:
        raise RuntimeError("Unexpected or unreviewed write-back plan")
    changes = plan["changes"]
    for item in changes:
        rel = item["relative_path"]
        if sha(run / "backup_db_storage" / rel) != item["before_sha256"]:
            raise RuntimeError("Rollback backup verification failed")
        staged = (run / "staged_encrypted" / rel).resolve()
        if sha(staged) != item["after_sha256"]:
            raise RuntimeError("Staged database changed")
        wanted = item["after_sha256"] if restore else item["before_sha256"]
        if sha(checked_source(rel)) != wanted:
            raise RuntimeError("Installed file changed; refusing to overwrite newer data")
        if restore and any(checked_source(rel + suffix).exists() for suffix in ("-wal", "-shm", "-journal")):
            raise RuntimeError("New journaling files exist; refusing to overwrite newer data")
    if not restore:
        actual = {str(p.relative_to(SOURCE)): sha(p) for p in SOURCE.rglob("*") if p.is_file()}
        if actual != manifest["original_hashes"]:
            raise RuntimeError("Account database files changed after backup")
    log = {"started_at": datetime.now(timezone.utc).isoformat(), "action": "restore" if restore else "install",
           "status": "in_progress", "changes": [], "client_acceptance_tested": False}
    save(run / "installation.json", log)
    try:
        for item in changes:
            require_stopped()
            rel = item["relative_path"]
            target = checked_source(rel)
            # Remove stale journaling files only after backing up their exact contents.
            for suffix in ("-wal", "-shm", "-journal"):
                sidecar = checked_source(rel + suffix)
                if sidecar.exists():
                    quarantine = run / "removed_sidecars" / (rel + suffix)
                    quarantine.parent.mkdir(parents=True, exist_ok=True)
                    if not quarantine.exists():
                        shutil.copy2(sidecar, quarantine)
                    sidecar.unlink()
            source = run / ("backup_db_storage" if restore else "staged_encrypted") / rel
            temporary = target.with_name(target.name + ".codex-cleanup.tmp")
            if temporary.exists():
                raise RuntimeError("Unexpected pre-existing staging file")
            shutil.copy2(source, temporary)
            if sha(temporary) != sha(source):
                raise RuntimeError("Installation copy hash mismatch")
            require_stopped()
            os.replace(temporary, target)
            if restore:
                for suffix in ("-wal", "-shm", "-journal"):
                    saved = run / "backup_db_storage" / (rel + suffix)
                    if saved.exists():
                        shutil.copy2(saved, checked_source(rel + suffix))
            log["changes"].append(rel)
        require_stopped()
        for item in changes:
            wanted = item["before_sha256"] if restore else item["after_sha256"]
            if sha(checked_source(item["relative_path"])) != wanted:
                raise RuntimeError("Installed hash verification failed")
        log["status"] = "restored" if restore else "installed"
    except BaseException as exc:
        try:
            require_stopped()
        except RuntimeError:
            log["status"] = "interrupted_by_wechat_restart_recovery_deferred"
            log["error"] = str(exc)
            raise
        for item in changes:
            rel = item["relative_path"]
            temporary = checked_source(rel).with_name(Path(rel).name + ".codex-cleanup.tmp")
            if temporary.exists() and sha(temporary) in (item["before_sha256"], item["after_sha256"]):
                temporary.unlink()
            shutil.copy2(run / "backup_db_storage" / rel, checked_source(rel))
            for suffix in ("-wal", "-shm", "-journal"):
                saved = run / "backup_db_storage" / (rel + suffix)
                if saved.exists():
                    shutil.copy2(saved, checked_source(rel + suffix))
        log["status"] = "failed_and_originals_restored"
        log["error"] = str(exc)
        raise
    finally:
        log["finished_at"] = datetime.now(timezone.utc).isoformat()
        save(run / "installation.json", log)
    print(json.dumps({"status": log["status"], "files": len(changes), "messages": 0 if restore else plan["deleted_messages"],
                      "client_acceptance_tested": False, "backup": str(run / "backup_db_storage")}))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("build", "install", "restore"))
    parser.add_argument("run")
    parser.add_argument("--account", required=True, help="Explicit local account folder")
    parser.add_argument("--db-dir", required=True, help="Directory containing local account folders")
    args = parser.parse_args()
    configure_target(args.account, args.db_dir)
    if args.action == "build":
        build(args.run)
    else:
        install(args.run, restore=args.action == "restore")
