"""Offline executable diagnostics using synthetic data only."""
from __future__ import annotations

import ctypes
from pathlib import Path
import os
import sys
import tempfile

import apsw
import zstandard

from .database import authenticated_decrypt, encrypt_snapshot, open_fts
from .files import save
from .upstream import get_upstream


def run_selftest(root):
    root = Path(root).resolve()
    report = {"synthetic_only": True, "status": "failed", "frozen": bool(getattr(sys,"frozen",False))}
    try:
        with tempfile.TemporaryDirectory(dir=root, prefix="diagnostics-") as temporary:
            path = Path(temporary) / "sample.db"
            conn = apsw.Connection(str(path))
            reserve = ctypes.c_int(80)
            conn.file_control("main", apsw.SQLITE_FCNTL_RESERVE_BYTES, ctypes.addressof(reserve))
            conn.execute("VACUUM")
            conn.register_fts5_tokenizer("MMFtsTokenizer", lambda c,a: lambda d,f,l: iter([(0,len(d),d.decode())]))
            conn.execute("CREATE VIRTUAL TABLE message_fts_v4_0 USING fts5(acontent,tokenize='MMFtsTokenizer')")
            conn.execute("INSERT INTO message_fts_v4_0 VALUES('sample')")
            conn.close()
            fts, tables, tokens = open_fts(path)
            try:
                fts.execute("DELETE FROM message_fts_v4_0 WHERE rowid=1")
                fts.execute("INSERT INTO message_fts_v4_0(message_fts_v4_0,rank) VALUES('integrity-check',1)")
                assert fts.execute("SELECT COUNT(*) FROM message_fts_v4_0").fetchone()[0] == 0
            finally:
                fts.close()
            upstream = get_upstream()
            key = os.urandom(32)
            plain = path.read_bytes()
            encrypted, mac_key = encrypt_snapshot(plain,os.urandom(upstream.PAGE_SZ),key,upstream)
            assert upstream._verify_enc_key(key,encrypted[:upstream.PAGE_SZ])
            assert authenticated_decrypt(encrypted,key,mac_key,upstream) == plain
            compressed = zstandard.ZstdCompressor().compress(b"synthetic text")
            assert zstandard.ZstdDecompressor().decompress(compressed) == b"synthetic text"
            forbidden = [name for name in sys.modules if name.split('.')[0] in {"frida","violet","internship_agent","wechatauto"}]
            assert not forbidden
            report.update(status="passed", aes_hmac_roundtrip=True, fts_delete_integrity=True,
                          zstandard=True, isolated_database_reader=True, forbidden_modules_loaded=forbidden)
    except Exception as error:
        report["error"] = type(error).__name__
        raise
    finally:
        save(root / "selftest.json",report)
    return report
