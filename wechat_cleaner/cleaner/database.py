"""Database primitives extracted from the verified cleanup experiments."""
from __future__ import annotations
from collections import Counter, defaultdict
from pathlib import Path
import hashlib
import hmac
import json
import os
import sqlite3
import struct
import apsw

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
