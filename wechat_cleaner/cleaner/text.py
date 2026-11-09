"""Decode plain-text candidates; other message types remain unsupported."""
import hashlib

from .database import encoded_row


def decoded(value):
    if isinstance(value, str):
        return value
    if not isinstance(value, bytes):
        return ""
    try:
        return value.decode("utf-8")
    except UnicodeDecodeError:
        marker = value.find(b"\x28\xb5\x2f\xfd")
        if marker >= 0:
            try:
                import zstandard
                return zstandard.ZstdDecompressor().decompress(value[marker:], max_output_size=5_000_000).decode("utf-8")
            except (ValueError, UnicodeDecodeError, zstandard.ZstdError):
                pass
    return ""


def content(row, group=False, known_ids=()):
    text = decoded(row.get("message_content")) or decoded(row.get("compress_content"))
    if group:
        first, separator, rest = text.partition(":\n")
        if separator and first in known_ids:
            text = rest
    return text


def row_hash(row):
    return hashlib.sha256(encoded_row(tuple(row)).encode()).hexdigest()


def table_name(user):
    return "Msg_" + hashlib.md5(user.encode()).hexdigest()
