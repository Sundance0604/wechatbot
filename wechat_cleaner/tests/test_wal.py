from contextlib import closing
import sqlite3
import struct

import pytest

from cleaner.files import CleanupError
from cleaner.wal import header_checksum, inspect_inactive_wal


SALT = b"current!"
OLD_SALT = b"previous"


def wal_header(magic=0x377F0682, salt=SALT):
    prefix = struct.pack(">IIII", magic, 3007000, 4096, 2) + salt
    return prefix + struct.pack(">II", *header_checksum(prefix, magic))


def wal_frame(salt=OLD_SALT):
    return struct.pack(">II", 1, 1) + salt + bytes(8) + b"x" * 4096


@pytest.mark.parametrize("padding", [1, 7, 16, 511, 4096, 4120, 8192])
def test_zero_padding_is_not_an_incomplete_current_frame(tmp_path, padding):
    path = tmp_path / "message_0.db-wal"
    data = wal_header() + bytes(padding)
    path.write_bytes(data)
    result = inspect_inactive_wal(path)
    assert result["state"] == "zero_padding"
    assert result["trailing_bytes"] == padding % 4120
    assert path.read_bytes() == data


@pytest.mark.parametrize("magic", [0x377F0682, 0x377F0683])
@pytest.mark.parametrize("extra", [b"garbage", bytes(4040), b"t" * 123], ids=["text-tail", "zero-reserve", "nonzero-reserve"])
def test_stale_frame_allocation_may_have_an_unaligned_tail(tmp_path, magic, extra):
    path = tmp_path / "message_fts.db-wal"
    data = wal_header(magic) + wal_frame() + extra
    path.write_bytes(data)
    assert inspect_inactive_wal(path)["state"] == "stale_generation"
    assert path.read_bytes() == data


@pytest.mark.parametrize("length", [16, 24, 512, 4119])
def test_partial_stale_frame_is_identified_by_its_old_salts(tmp_path, length):
    path = tmp_path / "sample.db-wal"
    path.write_bytes(wal_header() + wal_frame()[:length])
    assert inspect_inactive_wal(path)["state"] == "stale_generation"


def test_current_frame_still_blocks_with_an_unaligned_tail(tmp_path):
    path = tmp_path / "message_0.db-wal"
    data = wal_header() + wal_frame(SALT) + bytes(512)
    path.write_bytes(data)
    with pytest.raises(CleanupError, match="当前世代") as error:
        inspect_inactive_wal(path)
    assert path.name in str(error.value) and "512" in str(error.value)
    assert path.read_bytes() == data


@pytest.mark.parametrize("tail", [b"abc", wal_frame(SALT)[:24], wal_frame(SALT)[:1000]], ids=["ambiguous-short", "current-header", "current-partial-page"])
def test_ambiguous_or_current_partial_frame_is_not_ignored(tmp_path, tail):
    path = tmp_path / "message_0.db-wal"
    data = wal_header() + tail
    path.write_bytes(data)
    with pytest.raises(CleanupError, match="尚不能确认"):
        inspect_inactive_wal(path)
    assert path.read_bytes() == data


def test_header_checksum_failure_is_not_treated_as_stale_data(tmp_path):
    path = tmp_path / "sample.db-wal"
    header = bytearray(wal_header())
    header[24] ^= 1
    path.write_bytes(header + wal_frame())
    with pytest.raises(CleanupError, match="日志头校验"):
        inspect_inactive_wal(path)


def test_stale_first_frame_ends_the_valid_prefix(tmp_path):
    path = tmp_path / "sample.db-wal"
    path.write_bytes(wal_header() + wal_frame() + wal_frame(SALT) + b"tail")
    assert inspect_inactive_wal(path)["state"] == "stale_generation"


def test_unknown_header_format_still_blocks(tmp_path):
    path = tmp_path / "sample.db-wal"
    header = bytearray(wal_header())
    header[8:12] = struct.pack(">I", 8192)
    path.write_bytes(header + wal_frame())
    with pytest.raises(CleanupError, match="格式不受支持"):
        inspect_inactive_wal(path)


@pytest.mark.parametrize("allocation", [bytes(8160), wal_frame() + bytes(4040), wal_frame() + b"leftover"], ids=["zero-reserve", "stale-zero-tail", "stale-text-tail"])
def test_sqlite_reads_the_same_main_database_with_inactive_allocation(tmp_path, allocation):
    path = tmp_path / "reference.db"
    with closing(sqlite3.connect(path)) as conn:
        conn.execute("PRAGMA page_size=4096")
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("CREATE TABLE messages(id INTEGER PRIMARY KEY,body TEXT)")
        conn.execute("INSERT INTO messages VALUES(1,'keep synthetic text')")
        conn.commit()
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    wal = path.with_name(path.name + "-wal")
    wal.write_bytes(wal_header() + allocation)
    assert inspect_inactive_wal(wal)["state"] in {"zero_padding", "stale_generation"}
    with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as conn:
        assert conn.execute("SELECT * FROM messages").fetchall() == [(1, "keep synthetic text")]
        assert conn.execute("PRAGMA integrity_check").fetchall() == [("ok",)]
