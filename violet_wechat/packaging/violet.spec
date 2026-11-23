# Build with the validated Python 3.11 x64 environment and PyInstaller 6.22.3.
from importlib.metadata import distribution, packages_distributions
from pathlib import Path
import sys
from PyInstaller.utils.hooks import collect_data_files,copy_metadata
from PyInstaller.building.utils import format_binaries_and_datas

root=Path(SPECPATH).parent
reader=distribution('wechatauto-replica')
assert reader.version=='1.2.1','Build requires the validated database reader'
datas=[(str(root/'violet/static'),'violet/static'),
       (str(root/'violet/native'),'violet/native'),
       (str(root/'violet/harness'),'violet/harness'),
       (str(reader.locate_file('wechatauto/db.py')),'violet/vendor'),
       (str(root/'packaging/THIRD_PARTY_NOTICES.txt'),'licenses')]
datas.append((str(root/'LICENSE'),'licenses/violet'))
datas+=collect_data_files('tzdata')
python_license=Path(sys.base_prefix)/'LICENSE_PYTHON.txt'
if python_license.is_file():datas.append((str(python_license),'licenses/python'))
packages=['fastapi','starlette','uvicorn','httpx','httpcore','pydantic','pydantic-core',
          'psutil','frida','wechatauto-replica','cryptography','zstandard','tzdata','h11',
          'anyio','click','certifi','idna','typing-extensions','annotated-types','typing-inspection','pyinstaller']
for name in packages:
    package=distribution(name)
    datas+=copy_metadata(name)
    for relative in package.files or []:
        if relative.name.upper().startswith(('LICENSE','COPYING','NOTICE')):
            path=package.locate_file(relative)
            if path.is_file():datas.append((str(path),'licenses/'+name+'/'+str(relative.parent)))
binaries=[(str(path),'.') for name in ['ffi.dll','sqlite3.dll']
          for path in [Path(sys.base_prefix)/'Library/bin'/name] if path.is_file()]
a=Analysis([str(root/'packaging/launcher.py')],pathex=[str(root)],binaries=binaries,datas=datas,
    hiddenimports=['uvicorn.logging','uvicorn.loops.auto','uvicorn.protocols.http.h11_impl',
                   'uvicorn.protocols.websockets.websockets_impl','uvicorn.lifespan.on',
                   'sqlite3','ctypes.wintypes','cryptography.hazmat.primitives.ciphers',
                   'zstandard.backend_c','frida._frida','zoneinfo','winreg','queue'],
    hookspath=[],hooksconfig={},runtime_hooks=[],
    excludes=['wechatauto','internship_agent','wechat_cleaner','pytest','numpy','scipy','pandas',
              'torch','tensorflow','matplotlib','tkinter','PyQt5','PyQt6','PySide6','IPython'],
    noarchive=False)
# Optional imports discovered by hooks can also enter the executable. Include
# their notices and metadata as well, so the manifest describes the actual bundle.
providers=packages_distributions()
bundled={name.split('.')[0] for name,_,_ in a.pure}
bundled.update(Path(name).parts[0].split('.')[0] for name,_,_ in a.binaries)
extra_packages={package for module in bundled for package in providers.get(module,[])
                if package.lower().replace('_','-')!='violet-wechat'}
extra_packages.add('pywin32')
extra_datas=[]
for name in sorted(extra_packages-set(packages)):
    package=distribution(name)
    extra_datas+=copy_metadata(name)
    for relative in package.files or []:
        if relative.name.upper().startswith(('LICENSE','COPYING','NOTICE')):
            path=package.locate_file(relative)
            if path.is_file():extra_datas.append((str(path),'licenses/'+name+'/'+str(relative.parent)))
a.datas += [(target,source,'DATA') for target,source in format_binaries_and_datas(extra_datas)]
# RECORD and editable-install URLs can contain absolute development-machine paths.
# Runtime metadata lookup requires METADATA, not these installation records.
a.datas = [(target,source,kind) for target,source,kind in a.datas
           if Path(target).name not in {'RECORD','direct_url.json'}
           and not target.replace('\\','/').startswith('violet_wechat.egg-info/')]
pyz=PYZ(a.pure)
exe=EXE(pyz,a.scripts,a.binaries,a.datas,[],name='Violet',debug=False,
    bootloader_ignore_signals=False,strip=False,upx=False,console=False)
