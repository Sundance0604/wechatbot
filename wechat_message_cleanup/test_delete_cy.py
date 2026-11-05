"""Backend-only keyword cleanup feasibility test using the installed WeChatDB.

Run with the project's Python environment. Scans every local message shard,
including conversations outside the configured group list. Does not use UI
automation. Reports unsupported deletion rather than changing a decrypted copy
and claiming the original WeChat message was deleted.
"""
from __future__ import annotations

import argparse
import hashlib
import inspect
import json
from datetime import datetime, timezone
from importlib.metadata import version
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def run(keyword: str, account: str | None, report_path: Path, db_dir: str | None = None) -> int:
    report = {
        "started_at": datetime.now(timezone.utc).isoformat(),
        "mode": "backend-only",
        "keyword": keyword,
        "match": "case-sensitive substring of parsed message text",
        "status": "starting",
        "scanned_shards": 0,
        "scanned_tables": 0,
        "scanned_messages": 0,
        "matches": 0,
        "deleted_original_messages": 0,
        "errors": [],
    }
    candidates_path = report_path.with_name("cy_candidates.jsonl")
    try:
        from wechatauto import WeChatDB
        from contacts import load_contact_names
        from message_parser import clean_group_content

        report["library_version"] = version("wechatauto-replica")
        # Inspect only the database class; GUI wrappers are excluded.
        report["database_mutation_methods"] = [
            name for name, method in inspect.getmembers(WeChatDB, inspect.isfunction)
            if not name.startswith("_")
            and any(word in name.lower() for word in ("delete", "recall", "revoke"))
        ]
        report["reader_opens_readonly_cache"] = "mode=ro" in inspect.getsource(WeChatDB._open)
        # tasklist can be denied even when process enumeration is available.
        # Override discovery only; leave installed package files unchanged.
        import psutil

        class ProcessAwareWeChatDB(WeChatDB):
            def _find_weixin_pids(self):
                pids = []
                for process in psutil.process_iter(["name"]):
                    try:
                        if (process.info["name"] or "").lower() == "weixin.exe":
                            pids.append(process.pid)
                    except (psutil.NoSuchProcess, psutil.AccessDenied):
                        continue
                report["process_detection"] = "psutil"
                report["weixin_process_count"] = len(pids)
                return pids

        print("Connecting to local WeChat database...", flush=True)
        db = ProcessAwareWeChatDB(account=account, db_dir=db_dir)
        # Account is recorded locally so the tested account can be checked.
        report["account"] = db.account
        report["account_selection"] = "explicit" if account else "library automatic selection"
        try:
            names = load_contact_names(db)
        except Exception as exc:
            names = {}
            report["errors"].append({"stage": "contacts", "error": str(exc)})
        table_users = {
            "Msg_" + hashlib.md5(user.encode("utf-8")).hexdigest(): user
            for user in names
        }
        with candidates_path.open("w", encoding="utf-8") as candidates:
            for shard in db._message_dbs():
                conn = None
                try:
                    conn = db._open(shard)
                    conn.execute("PRAGMA query_only=ON")
                    tables = [row[0] for row in conn.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    ) if re.fullmatch(r"Msg_[a-fA-F0-9]{32}", row[0])]
                    for table in tables:
                        user = table_users.get(table, "")
                        report["scanned_tables"] += 1
                        for row in conn.execute(f'SELECT * FROM "{table}"'):
                            report["scanned_messages"] += 1
                            item = dict(row)
                            text = clean_group_content(item, names, user)
                            if keyword not in text:
                                continue
                            report["matches"] += 1
                            candidate = {
                                "account": db.account,
                                "conversation": user or table,
                                "name": names.get(user, ""),
                                "shard": str(shard),
                                "table": table,
                                "local_id": item.get("local_id"),
                                "server_id": item.get("server_id"),
                                "create_time": item.get("create_time"),
                                "local_type": item.get("local_type"),
                                "text": text,
                            }
                            candidates.write(json.dumps(candidate, ensure_ascii=False) + "\n")
                    report["scanned_shards"] += 1
                    print(f"Scanned shards: {report['scanned_shards']}; matches: {report['matches']}", flush=True)
                except Exception as exc:
                    report["errors"].append({"stage": "messages", "shard": str(shard), "error": str(exc)})
                finally:
                    if conn is not None:
                        conn.close()
        report["candidates_file"] = str(candidates_path)
        report["scan_complete"] = not report["errors"]
        report["status"] = "backend_delete_unavailable"
        report["reason"] = (
            "The installed WeChatDB exposes no verified original-message deletion API. "
            "Its SQLite connection reads a decrypted cache, not a writable original database. "
            "Keyword candidates were collected; no original messages were deleted."
        )
        return 2
    except KeyboardInterrupt:
        report["status"] = "backend_test_interrupted"
        report["reason"] = "Stopped the bounded experiment before database initialization or scanning completed."
        return 130
    except Exception as exc:
        report["status"] = "backend_test_failed"
        report["reason"] = f"{type(exc).__name__}: {exc}"
        return 1
    finally:
        report["finished_at"] = datetime.now(timezone.utc).isoformat()
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps({key: report[key] for key in (
            "status", "scanned_shards", "scanned_messages", "matches", "deleted_original_messages"
        )}, ensure_ascii=False), flush=True)
        print(f"Report: {report_path}", flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--keyword", default="cy")
    parser.add_argument("--account", required=True, help="Explicit local account folder name")
    parser.add_argument("--db-dir", help="Directory containing local account folders")
    args = parser.parse_args()
    if not args.keyword:
        parser.error("keyword must not be empty")
    raise SystemExit(run(args.keyword, args.account, Path(__file__).with_name("test_delete_cy_result.json"), args.db_dir))
