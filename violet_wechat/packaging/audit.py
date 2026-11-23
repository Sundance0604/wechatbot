"""Inspect EXE data and Python constants without printing private matches."""
import argparse
import json
from pathlib import Path
import re
import types

from PyInstaller.archive.readers import CArchiveReader


def audit(exe,denied=()):
    archive=CArchiveReader(str(exe))
    issues=[]
    private_parts={'runtime','memory','group_memory','analyses','owner_profile_history'}
    private_names={'config.local.json','owner_profile.md','keys.json','state.sqlite3','RECORD','direct_url.json'}
    patterns=[re.compile(r'\bwxid_[A-Za-z0-9]{12,}\b'),
              re.compile(r'\b\d{8,}@chatroom\b'),
              re.compile(r'[A-Za-z]:[\\/]+Users[\\/]+(?!Public\b|Example\b|USERNAME\b|<user>)[^\s\\/"\']+')]
    encoded=[value.encode('utf-8') for value in denied if value]
    def check_text(text,location):
        if any(value and value in text for value in denied):
            issues.append({'entry':location,'reason':'private_string'})
        if any(pattern.search(text) for pattern in patterns):
            issues.append({'entry':location,'reason':'personal_identifier_or_home'})
    def inspect(value,location):
        if isinstance(value,types.CodeType):
            check_text(value.co_filename,location)
            for constant in value.co_consts:inspect(constant,location)
        elif isinstance(value,str):check_text(value,location)
        elif isinstance(value,(tuple,list,frozenset)):
            for item in value:inspect(item,location)
    for original in archive.toc:
        name=original.replace('\\','/')
        path=Path(name)
        if set(path.parts)&private_parts or path.name in private_names:
            issues.append({'entry':name,'reason':'private_path'})
        data=archive.extract(original)
        if isinstance(data,bytes) and any(value in data for value in encoded):
            issues.append({'entry':name,'reason':'private_bytes'})
        if path.suffix in {'.js','.mjs','.py','.html','.css','.json','.txt','.md'}:
            check_text(data.decode('utf-8',errors='replace'),name)
        if name.endswith('.pyz'):
            embedded=archive.open_embedded_archive(original)
            for module in embedded.toc:inspect(embedded.extract(module),module)
    return {'passed':not issues,'archive_entries':len(archive.toc),'issues':issues}


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('exe',type=Path)
    parser.add_argument('--deny-file',type=Path)
    args=parser.parse_args()
    denied=args.deny_file.read_text(encoding='utf-8').splitlines() if args.deny_file else []
    result=audit(args.exe,denied)
    print(json.dumps(result,ensure_ascii=False,indent=2))
    raise SystemExit(0 if result['passed'] else 1)
