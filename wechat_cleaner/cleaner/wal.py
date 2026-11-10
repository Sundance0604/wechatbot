"""Recognize an inactive WAL without modifying it or merging current frames.

SQLite reuses WAL allocation. Valid frames form a prefix beginning immediately
after the checked header; a salt mismatch or zero page terminates that prefix.
The physical file length alone is not the valid-frame boundary.
"""
from pathlib import Path
import struct

from .files import CleanupError
from .diagnostics import event


def header_checksum(data, magic):
    order = "<" if magic == 0x377F0682 else ">"
    words = struct.unpack(order + "6I", data[:24])
    first = second = 0
    for index in range(0, 6, 2):
        first = (first + words[index] + second) & 0xFFFFFFFF
        second = (second + words[index + 1] + first) & 0xFFFFFFFF
    return first, second


def inspect_inactive_wal(path, page_size=4096):
    """Return non-content diagnostics only when no current frames can be used.

    This gate deliberately refuses *any* apparent current-generation frame,
    including uncommitted or damaged ones; it does not attempt WAL recovery.
    """
    path = Path(path)
    frame_size = 24 + page_size
    size = path.stat().st_size if path.exists() else 0
    result = {"bytes": size, "frame_size": frame_size,
              "complete_slots": max(0, size - 32) // frame_size,
              "trailing_bytes": max(0, size - 32) % frame_size}
    event("inspect_wal", path=str(path), **result)
    if size <= 32:
        return {**result, "state": "no_frame_data"}
    detail = f"{path.name}；{size} 字节，尾部 {result['trailing_bytes']} 字节"
    with path.open("rb") as stream:
        header = stream.read(32)
        if len(header) != 32:
            raise CleanupError(f"日志在检查期间发生变化，已停止（{detail}）。")
        magic, version, stored_page_size = struct.unpack(">III", header[:12])
        event("wal_header", format_magic=hex(magic), format_version=version, page_size=stored_page_size)
        if magic not in (0x377F0682, 0x377F0683) or version != 3007000 or stored_page_size != page_size:
            raise CleanupError(f"写入日志格式不受支持，请保留备份后检查微信版本（{detail}）。")
        if header_checksum(header, magic) != struct.unpack(">II", header[24:32]):
            raise CleanupError(f"写入日志头校验未通过，已停止；请保留备份（{detail}）。")
        first = stream.read(frame_size)
        # A zero-filled first slot cannot be a frame (page number zero). Once
        # the prefix ends, later allocation is not searched for new frames.
        if first and not any(first):
            return {**result, "state": "zero_padding", "header_checksum_verified": True}
        if len(first) >= 16 and first[8:16] != header[16:24]:
            return {**result, "state": "stale_generation", "header_checksum_verified": True}
        if len(first) < frame_size:
            raise CleanupError(f"日志尾部尚不能确认是旧数据或空白，已停止；请重新打开并正常退出微信后重试（{detail}）。")
        if struct.unpack(">I", first[:4])[0]:
            raise CleanupError(f"微信仍有当前世代的写入日志，暂不支持安全归并；原始数据未改动（{detail}）。")
        raise CleanupError(f"日志帧内容无法安全识别，已停止；请保留备份（{detail}）。")
