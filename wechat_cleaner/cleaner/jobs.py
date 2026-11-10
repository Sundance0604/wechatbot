from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import logging
import threading
import uuid

from .files import CleanupError
from .diagnostics import _current, describe_error


class Jobs:
    def __init__(self, diagnostics):
        self.diagnostics = diagnostics
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="cleaner")
        self.lock = threading.Lock()
        self.items = {}
        self.active = None

    def submit(self, kind, operation, context=None):
        with self.lock:
            if self.active:
                raise CleanupError("已有任务正在执行，请等待完成。")
            identifier = uuid.uuid4().hex
            self.diagnostics.start(identifier, kind, context)
            self.items[identifier] = {"id": identifier, "kind": kind, "state": "running", "progress": 0, "message": "正在准备", "result": None}
            self.active = identifier

        def progress(percent, message):
            self.diagnostics.progress(identifier, percent)
            with self.lock:
                self.items[identifier].update(progress=percent, message=message)

        def work():
            binding = _current.set((self.diagnostics, identifier))
            try:
                result = operation(progress)
                try:
                    self.diagnostics.finish(identifier, result=result)
                except OSError:
                    logging.getLogger("cleaner").error("Task completed but diagnostic storage failed")
                with self.lock:
                    self.items[identifier].update(state="completed", progress=100, message="已完成", result=result)
            except Exception as error:
                logging.getLogger("cleaner").error("%s failed: %s", kind, type(error).__name__)
                message = str(error) if isinstance(error, CleanupError) else "操作未完成，原始备份已保留。请检查连接和备份页中的操作状态。"
                try:
                    detail = self.diagnostics.finish(identifier, error=error)
                except OSError:
                    detail = describe_error(error, kind)
                    detail["storage_failed"] = True
                    message += " 诊断文件写入失败，请检查磁盘空间和目录权限。"
                with self.lock:
                    self.items[identifier].update(state="failed", message=message, diagnostic=detail)
            finally:
                _current.reset(binding)
                with self.lock:
                    self.active = None
        self.executor.submit(work)
        return self.get(identifier)

    def get(self, identifier):
        with self.lock:
            if identifier not in self.items:
                raise CleanupError("未找到该任务。")
            return dict(self.items[identifier])

    def close(self):
        self.executor.shutdown(wait=True, cancel_futures=False)
