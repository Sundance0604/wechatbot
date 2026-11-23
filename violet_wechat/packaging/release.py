"""Prepare a clean release ZIP from the inspected executable, never from user data."""
import argparse
from email.parser import BytesParser
import hashlib
import json
from pathlib import Path, PurePosixPath
import shutil
import tomllib
import zipfile

from PyInstaller.archive.readers import CArchiveReader

from audit import audit


def digest(path):
    with path.open('rb') as stream:return hashlib.file_digest(stream,'sha256').hexdigest()


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('exe',type=Path)
    parser.add_argument('--smoke-report',type=Path,required=True)
    parser.add_argument('--browser-report',type=Path)
    parser.add_argument('--deny-file',type=Path)
    parser.add_argument('--source-tests',type=int)
    args=parser.parse_args()
    root=Path(__file__).resolve().parents[1]
    version=tomllib.loads((root/'pyproject.toml').read_text(encoding='utf-8'))['project']['version']
    exe=args.exe.resolve()
    denied=args.deny_file.read_text(encoding='utf-8').splitlines() if args.deny_file else []
    result=audit(exe,denied)
    assert result['passed'],result
    smoke=json.loads(args.smoke_report.read_text(encoding='utf-8'))
    assert smoke['status']=='passed' and smoke['version']==version and smoke['exe_sha256']==digest(exe)
    assert smoke['private_defaults_clean'] and smoke['graceful_quit_ok']
    name=f'Violet-{version}-Windows-x64'
    release=exe.parent/name
    release.mkdir(exist_ok=True)
    shutil.copy2(exe,release/'Violet.exe')
    for file in ['使用说明.txt','THIRD_PARTY_NOTICES.txt']:
        shutil.copy2(root/'packaging'/file,release/file)
    shutil.copy2(root/'LICENSE',release/'LICENSE')
    archive=CArchiveReader(str(exe))
    dependencies=[]
    for original in archive.toc:
        path=original.replace('\\','/')
        if path.startswith('licenses/'):
            parts=PurePosixPath(path).parts
            assert '..' not in parts
            target=release.joinpath(*parts)
            target.parent.mkdir(parents=True,exist_ok=True)
            target.write_bytes(archive.extract(original))
        if path.endswith('.dist-info/METADATA'):
            metadata=BytesParser().parsebytes(archive.extract(original))
            dependencies.append({'name':metadata['Name'],'version':metadata['Version']})
    manifest={'application':'Violet','version':version,'platform':'Windows x64',
        'dependencies':sorted(dependencies,key=lambda item:item['name'].lower())}
    (release/'依赖清单.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2),encoding='utf-8')
    for label,flag in [('停止监听','--stop'),('退出程序','--quit')]:
        (release/(label+'.cmd')).write_bytes(('@echo off\r\n"%~dp0Violet.exe" '+flag+'\r\n').encode('ascii'))
    safe_fields=['version','status','frozen','checks','exe_sha256','panel_started','assets_loaded',
        'private_defaults_clean','unauthorized_write_blocked','stale_settings_blocked',
        'repeated_launch_ok','graceful_quit_ok','wechat_read_connected',
        'native_recovery_connected','detected_wechat_version']
    verification={'exe':{key:smoke[key] for key in safe_fields if key in smoke},'privacy_audit':result,
        'actual_wechat_messages_sent':False,'tested_on_another_physical_pc':False}
    if args.source_tests is not None:verification['source_tests_passed']=args.source_tests
    if args.browser_report:
        browser=json.loads(args.browser_report.read_text(encoding='utf-8'))
        assert browser['exe_sha256']==digest(exe) and not browser['browserErrors']
        verification['browser']={key:browser[key] for key in ['stalePageRejected','enginePreserved',
            'newWorkspaceSaved','monitorToggleThenSave','browserErrors','exe_sha256']}
    (release/'验证结果.json').write_text(json.dumps(verification,ensure_ascii=False,indent=2),encoding='utf-8')
    checksum=digest(exe)+'  Violet.exe\n'
    (release/'SHA256SUMS.txt').write_text(checksum,encoding='ascii')
    zip_path=exe.parent/(name+'.zip')
    with zipfile.ZipFile(zip_path,'w',compression=zipfile.ZIP_DEFLATED,compresslevel=6) as bundle:
        for file in sorted(release.rglob('*')):
            if file.is_file():bundle.write(file,arcname=file.relative_to(release.parent).as_posix())
    with zipfile.ZipFile(zip_path) as bundle:
        assert bundle.testzip() is None
        assert hashlib.sha256(bundle.read(name+'/Violet.exe')).hexdigest()==digest(exe)
    checksums=exe.parent/'SHA256SUMS.txt'
    checksums.write_text(checksum+digest(zip_path)+'  '+zip_path.name+'\n',encoding='ascii')
    print(json.dumps({'exe':str(exe),'zip':str(zip_path),'checksums':str(checksums),
        'exe_sha256':digest(exe),'privacy_passed':True,'zip_integrity_passed':True},ensure_ascii=False))


if __name__=='__main__':main()
