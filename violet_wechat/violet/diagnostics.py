"""Offline distribution checks; never loads private settings or sends messages."""
import json
from pathlib import Path
import sqlite3
import sys
from zoneinfo import ZoneInfo

from . import __version__
from .settings import ROOT


def self_test():
    checks={}
    def check(name,operation):
        try:
            result=operation()
            checks[name]={'ok':True,'value':result}
        except Exception as exc:
            checks[name]={'ok':False,'error_type':type(exc).__name__}
    def sqlite():
        with sqlite3.connect(':memory:') as db:
            assert db.execute('select 1').fetchone()[0]==1
        return sqlite3.sqlite_version
    def compression():
        import zstandard
        content=b'Violet offline check'
        assert zstandard.ZstdDecompressor().decompress(zstandard.ZstdCompressor().compress(content))==content
        return True
    def encryption():
        from cryptography.hazmat.primitives.ciphers import Cipher,algorithms,modes
        cipher=Cipher(algorithms.AES(bytes(32)),modes.CBC(bytes(16)))
        enc=cipher.encryptor();encrypted=enc.update(bytes(16))+enc.finalize()
        dec=cipher.decryptor();assert dec.update(encrypted)+dec.finalize()==bytes(16)
        return True
    def reader():
        from .reader import database_reader
        assert callable(database_reader().WeChatDB)
        assert 'wechatauto' not in sys.modules
        return True
    def native_profiles():
        profiles=[]
        for path in sorted((ROOT/'violet/native').glob('weixin_*.json')):
            data=json.loads(path.read_text(encoding='utf-8'))
            assert len(data['module_sha256'])==64 and set(data['functions'])==set(data['prefixes'])
            profiles.append(data.get('version') or path.stem.removeprefix('weixin_').replace('_','.'))
        assert profiles
        return profiles
    def assets():
        for name in ['index.html','app.js','agents.js','styles.css']:
            assert (ROOT/'violet/static'/name).is_file()
        assert (ROOT/'violet/harness/workspace_policy.mjs').is_file()
        return True
    def instrumentation():
        import frida
        assert callable(frida.get_local_device)
        return frida.__version__
    check('sqlite',sqlite);check('zstandard',compression);check('cryptography',encryption)
    check('database_reader',reader);check('frida',instrumentation)
    check('timezone',lambda:str(ZoneInfo('Asia/Shanghai')))
    check('native_profiles',native_profiles);check('assets',assets)
    return {'version':__version__,'frozen':bool(getattr(sys,'frozen',False)),
            'status':'passed' if all(row['ok'] for row in checks.values()) else 'failed','checks':checks}
