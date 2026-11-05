"""Back up a stopped WeChat account and prepare inspectable offline databases."""
import argparse
from datetime import datetime
import hashlib
import json
import re
from pathlib import Path
import shutil
import sys

import psutil


def require_stopped():
    running = [p.pid for p in psutil.process_iter(["name"])
               if (p.info["name"] or "").lower() in ("weixin.exe", "wechat.exe")]
    if running:
        raise RuntimeError("WeChat restarted; stopping before any write-back")


def digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def main(account, db_dir=None):
    from wechatauto import WeChatDB
    if not re.fullmatch(r"[A-Za-z0-9_@.-]{1,100}", account) or account in (".", ".."):
        raise ValueError("Account must be one local folder name")
    require_stopped()
    db = WeChatDB(account=account, db_dir=db_dir)
    source = (Path(db.account_dir) / "db_storage").resolve()
    run = Path(__file__).resolve().parent / "writeback_runs" / datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    run.mkdir(parents=True)
    backup = run / "backup_db_storage"
    original_hashes = {str(p.relative_to(source)): digest(p) for p in source.rglob("*") if p.is_file()}
    shutil.copytree(source, backup)
    backup_hashes = {str(p.relative_to(backup)): digest(p) for p in backup.rglob("*") if p.is_file()}
    require_stopped()
    if backup_hashes != original_hashes or any(digest(source / rel) != value for rel, value in original_hashes.items()):
        raise RuntimeError("Source changed or backup verification failed")
    manifest = {"source_db_storage": str(source), "account": db.account,
                "backup_db_storage": str(backup), "original_hashes": original_hashes,
                "backup_verified": True, "original_files_modified": False}
    (run / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    for rel, _, _ in db._db_files:
        original = backup / rel
        plain = run / "decrypted" / rel
        db._decrypt_file(str(original), str(plain), db._keys[rel])
        wal = Path(str(original) + "-wal")
        if wal.exists() and wal.stat().st_size > db.WAL_HEADER_SZ:
            db._merge_wal(str(plain), str(wal), db._keys[rel], 0)
    print(json.dumps({"backup_verified": True, "backed_up_files": len(original_hashes),
                      "run": str(run), "source": str(source)}, ensure_ascii=False))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--account", required=True)
    parser.add_argument("--db-dir", help="Directory containing local account folders")
    args = parser.parse_args()
    main(args.account, args.db_dir)
