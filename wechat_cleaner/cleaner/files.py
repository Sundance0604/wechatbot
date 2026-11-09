from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import tempfile
import shutil


class CleanupError(RuntimeError):
    pass


def sha(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def save(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=path.name + ".", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=True, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if Path(temporary).exists():
            Path(temporary).unlink()


def read(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def safe_path(root, relative):
    root = Path(root).resolve()
    relative = Path(relative)
    if relative.is_absolute() or ".." in relative.parts or relative.drive:
        raise CleanupError("备份中的文件路径无效。")
    path = root / relative
    current = path
    while current != root:
        if current.is_symlink() or (hasattr(current, "is_junction") and current.is_junction()):
            raise CleanupError("数据目录包含链接，请使用实际的微信数据目录。")
        current = current.parent
    if not path.resolve().is_relative_to(root):
        raise CleanupError("文件路径超出了当前账号。")
    return path


def hashes(root):
    root = Path(root).resolve()
    result = {}
    for path in sorted(root.rglob("*")):
        safe_path(root, path.relative_to(root))
        if path.is_file():
            result[path.relative_to(root).as_posix()] = sha(path)
    return result


def remove_workspace_tree(root, relative):
    """Remove only an explicitly named child of our own application workspace."""
    root = Path(root).resolve()
    path = safe_path(root, relative)
    resolved = path.resolve()
    if resolved == root or not resolved.is_relative_to(root):
        raise CleanupError("临时目录超出应用工作区。")
    if path.exists():
        for child in path.rglob("*"):
            safe_path(root, child.relative_to(root))
        shutil.rmtree(path)


def require_stopped():
    import psutil
    running = [p.pid for p in psutil.process_iter(["name"])
               if (p.info["name"] or "").lower() in ("weixin.exe", "wechat.exe")]
    if running:
        raise CleanupError("微信仍在运行。请在微信菜单中退出，关闭窗口不会退出微信。")


def process_status():
    import psutil
    processes = [p for p in psutil.process_iter(["name", "exe"])
                 if (p.info["name"] or "").lower() in ("weixin.exe", "wechat.exe")]
    version = None
    for process in processes:
        path = process.info.get("exe")
        if not path:
            continue
        try:
            import ctypes
            from ctypes import wintypes
            size = ctypes.windll.version.GetFileVersionInfoSizeW(path, None)
            buffer = ctypes.create_string_buffer(size)
            if size and ctypes.windll.version.GetFileVersionInfoW(path, 0, size, buffer):
                pointer, length = ctypes.c_void_p(), wintypes.UINT()
                if ctypes.windll.version.VerQueryValueW(buffer, "\\", ctypes.byref(pointer), ctypes.byref(length)):
                    class Info(ctypes.Structure):
                        _fields_ = [("values", wintypes.DWORD * 13)]
                    values = ctypes.cast(pointer, ctypes.POINTER(Info)).contents.values
                    version = ".".join(str(v) for v in (values[2] >> 16, values[2] & 65535, values[3] >> 16, values[3] & 65535))
                    break
        except (OSError, AttributeError, ValueError):
            continue
    return {"running": bool(processes), "version": version}
