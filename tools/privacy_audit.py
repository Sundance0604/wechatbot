"""Scan publishable source paths and text without printing potentially private values.

Use --staged before committing. --deny-file may contain additional local-only strings,
one per line; do not commit this file. This heuristic is a release aid, not a proof.
"""
import argparse
import json
from pathlib import Path
import re
import subprocess
import sys

ROOT=Path(__file__).resolve().parents[1]
PRIVATE_PARTS={'runtime','private_cache','offline_runs','writeback_runs','hook_watch_runs','hook_dependencies',
               'soul_runtime','group_memory','analyses','node_modules','.venv','__pycache__',
               '.build','comparison_runs','cache','runs'}
PRIVATE_NAMES={'config.local.json','config.json','keys.json','owners.json','owner_profile.md','monitored_groups.json'}
PRIVATE_SUFFIXES={'.db','.sqlite','.sqlite3','.dmp','.bin','.log','.jsonl','.pem','.key','.pfx','.png','.jpg','.jpeg','.mp3','.mp4','.zip','.whl','.exe'}
PATTERNS={
    'possible_api_key':re.compile(r'\bsk-[A-Za-z0-9_-]{20,}\b'),
    'real_account_identifier':re.compile(r'\bwxid_[a-zA-Z0-9]{12,}\b'),
    'real_numeric_group_identifier':re.compile(r'\b\d{8,}@chatroom\b'),
    'personal_windows_home':re.compile(r'[A-Za-z]:[\\/]+Users[\\/]+(?!Public\b|Example\b|USERNAME\b)[^\s\\/"\']+'),
    'private_key_material':re.compile(r'-----BEGIN (?:RSA |OPENSSH |EC )?PRIVATE KEY-----'),
}


def audit(files,denied=()):
    issues=[]
    for relative,data in files:
        path=Path(relative)
        if set(path.parts)&PRIVATE_PARTS or path.name in PRIVATE_NAMES or path.suffix.lower() in PRIVATE_SUFFIXES:
            issues.append({'path':relative,'reason':'private_artifact_path'})
        try:text=data.decode('utf-8-sig')
        except UnicodeDecodeError:
            issues.append({'path':relative,'reason':'non_text_file'});continue
        for reason,pattern in PATTERNS.items():
            if pattern.search(text):issues.append({'path':relative,'reason':reason})
        if any(value and value in text for value in denied):issues.append({'path':relative,'reason':'local_denylist_match'})
    return issues


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--staged',action='store_true')
    parser.add_argument('--deny-file',type=Path)
    args=parser.parse_args()
    denied=args.deny_file.read_text(encoding='utf-8').splitlines() if args.deny_file else []
    if args.staged:
        names=subprocess.check_output(['git','diff','--cached','--name-only','--diff-filter=ACMR','-z'],cwd=ROOT).split(b'\0')
        files=[(n.decode(),subprocess.check_output(['git','show',':'+n.decode()],cwd=ROOT)) for n in names if n]
    else:
        names=subprocess.check_output(['git','ls-files','--cached','--others','--exclude-standard','-z'],cwd=ROOT).split(b'\0')
        files=[(n.decode(),(ROOT/n.decode()).read_bytes()) for n in names if n and (ROOT/n.decode()).is_file()]
    issues=audit(files,denied)
    print(json.dumps({'scanned_files':len(files),'issues':issues,'passed':not issues},ensure_ascii=False,indent=2))
    return 1 if issues else 0


if __name__=='__main__':sys.exit(main())
