import hashlib
import json
from pathlib import Path
import threading
import frida
import psutil
from .errors import SendCancelled

BASE = Path(__file__).resolve().parent/'native'
GROUP = 'filehelper'

def event(kind, **data):
    pass

class Sender:
    def __init__(self, profile_path):
        self.session = self.script = None
        self.blocked = threading.Event()
        self.profile = json.loads(profile_path.read_text(encoding='utf-8'))
        spec = self.profile
        spec['recipient'] = GROUP
        if hashlib.sha256(Path(spec['module_path']).read_bytes()).hexdigest() != spec['module_sha256']:
            raise RuntimeError('当前微信版本尚未适配；已停止，未调用发送函数。当前仅验证4.2.0.11。')
        process = psutil.Process(spec['pid'])
        if process.name().lower() != 'weixin.exe' or process.create_time() != spec['process_creation_time']:
            raise RuntimeError('微信已经重启，请重新连接发送。')
        self.process = process
        spec['functions']['coroutine_entry'] = '0x4a9d40'
        spec['prefixes']['coroutine_entry'] = '554883ec20488d6c'
        spec['functions']['operator_delete'] = '0x76a4400'
        # Prefix from the authenticated binary, mapped using the PE section table.
        import struct
        data = Path(spec['module_path']).read_bytes()
        pe = struct.unpack_from('<I', data, 60)[0]
        sections = pe + 24 + struct.unpack_from('<H', data, pe + 20)[0]
        for index in range(struct.unpack_from('<H', data, pe + 6)[0]):
            _, va, size, raw = struct.unpack_from('<IIII', data, sections + index*40 + 8)
            rva = int(spec['functions']['operator_delete'], 16)
            if va <= rva < va + size:
                spec['prefixes']['operator_delete'] = data[raw+rva-va:raw+rva-va+8].hex()
                break
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
