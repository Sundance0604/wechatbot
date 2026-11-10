# Run from wechat_cleaner: python -m PyInstaller packaging/cleaner.spec --noconfirm
from importlib.metadata import distribution
from pathlib import Path
import sys
import os
from PyInstaller.utils.hooks import collect_data_files, copy_metadata

root = Path(SPECPATH).parent
reader = distribution('wechatauto-replica')
assert reader.version == '1.2.1', 'Use the pinned, validated reader version'
datas = [(str(root / 'cleaner/static'), 'cleaner/static'),
         (str(reader.locate_file('wechatauto/db.py')), 'cleaner/vendor')]
# Use the original basename in the bundle; the runtime loader checks db.py too.
datas += [(str(reader.locate_file('wechatauto_replica-1.2.1.dist-info/licenses/LICENSE')), 'licenses/wechatauto-replica')]
datas += collect_data_files('tzdata')
for name in ('fastapi','starlette','uvicorn','apsw','cryptography','psutil','wechatauto-replica'):
    datas += copy_metadata(name)
if (root.parent / 'LICENSE').is_file():
    datas.append((str(root.parent / 'LICENSE'), 'licenses/WeChatCleaner'))
datas += [(str(root / 'packaging/THIRD_PARTY_NOTICES.txt'), 'licenses')]
for name in ('fastapi','starlette','uvicorn','pydantic','pydantic-core','psutil','apsw',
             'cryptography','zstandard','tzdata','h11','anyio','click'):
    package = distribution(name)
    for relative in package.files or []:
        if relative.name.lower().startswith(('license','copying','notice')):
            path = package.locate_file(relative)
            if path.is_file():
                datas.append((str(path), 'licenses/' + name + '/' + relative.parent.name))
for name in ('react','react-dom','lucide-react'):
    license = root / 'frontend/node_modules' / name / 'LICENSE'
    if license.is_file():
        datas.append((str(license), 'licenses/' + name))
# Conda keeps these outside DLLs; include them so another PC needs no Conda.
binaries = [(str(path), '.') for name in ('ffi.dll','sqlite3.dll')
            for path in [Path(sys.base_prefix) / 'Library/bin' / name] if path.is_file()]
a = Analysis([str(root / 'run.py')], pathex=[str(root)], binaries=binaries, datas=datas,
    hiddenimports=['uvicorn.logging','uvicorn.loops.auto','uvicorn.protocols.http.h11_impl',
                   'uvicorn.protocols.websockets.wsproto_impl','uvicorn.lifespan.on',
                   'ctypes.wintypes','queue','sqlite3','cryptography.hazmat.primitives.ciphers'],
    hookspath=[], hooksconfig={}, runtime_hooks=[],
    excludes=['wechatauto','frida','torch','tensorflow','matplotlib','numpy','scipy',
              'pandas','cv2','PIL','tkinter','PyQt5','PyQt6','PySide6','IPython',
              'google','internship_agent','violet','pytest'], noarchive=False)
pyz = PYZ(a.pure)
if os.environ.get('CLEANER_ONEFILE') == '1':
    exe = EXE(pyz,a.scripts,a.binaries,a.datas,[],name='WeChatCleaner',debug=False,
              bootloader_ignore_signals=False,strip=False,upx=False,console=False)
else:
    exe = EXE(pyz,a.scripts,[],exclude_binaries=True,name='WeChatCleaner',debug=False,
              bootloader_ignore_signals=False,strip=False,upx=False,console=False)
    coll = COLLECT(exe,a.binaries,a.datas,strip=False,upx=False,name='WeChatCleaner')
