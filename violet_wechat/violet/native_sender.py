import json
from pathlib import Path
import threading
import frida
import psutil
from .errors import SendCancelled
from .native_profiles import bind_runtime_profile

BASE = Path(__file__).resolve().parent/'native'
GROUP = 'filehelper'

def event(kind, **data):
    pass

class Sender:
    def __init__(self, profile_path):
        self.session = self.script = None
        self.blocked = threading.Event()
        self.profile = bind_runtime_profile(json.loads(profile_path.read_text(encoding='utf-8')))
        spec = self.profile
        spec['recipient'] = GROUP
        process = psutil.Process(spec['pid'])
        if process.name().lower() != 'weixin.exe' or process.create_time() != spec['process_creation_time']:
            raise RuntimeError('微信已经重启，请重新连接发送。')
        self.process = process
        try:
            self.session = frida.get_local_device().attach(spec['pid'])
            source = (BASE / 'native_text_trial_agent.js').read_text(encoding='utf-8')
            source += '\n' + (BASE / 'soul_sender_agent.js').read_text(encoding='utf-8')
            self.script = self.session.create_script(source.replace('__PROFILE__', json.dumps(spec)))
            self.script.on('message', lambda value, data: event('hook_event', detail=value))
            self.script.load()
            self.runtime = self.script.exports_sync.inspect()
            self.test = self.script.exports_sync.selftest()
        except Exception:
            cleanup = threading.Thread(target=self.close,daemon=True)
            cleanup.start()
            cleanup.join(timeout=5)
            raise

    def send(self, reply, identifier, recipient=GROUP, allowed=None):
        def check():
            if self.blocked.is_set() or (allowed is not None and not allowed()):
                raise SendCancelled('监听已停止，未调用发送入口。')
        check()
        if len(reply.encode('utf-8')) > 2048:
            raise ValueError('Reply exceeds native message allocation limit')
        self.script.exports_sync.inspect()
        check()
        try:
            return self.script.exports_sync.sendtext(reply, identifier, recipient)
        except frida.RPCException as exc:
            if 'SEND_CANCELLED_BEFORE_INVOCATION' in str(exc):
                raise SendCancelled('已取消尚未进入微信发送函数的消息。') from None
            raise

    def block(self):
        self.blocked.set()

    def cancel_pending(self):
        if self.script:
            return self.script.exports_sync.cancelpending()

    def alive(self):
        try:
            return self.process.is_running() and self.process.create_time() == self.profile['process_creation_time']
        except psutil.Error:
            return False

    def close(self):
        if self.script:
            try:
                self.script.exports_sync.stop()
                self.script.unload()
            except Exception:
                pass
            self.script = None
        if self.session:
            try:
                self.session.detach()
            except Exception:
                pass
            self.session = None
