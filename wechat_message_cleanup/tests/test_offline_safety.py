"""Synthetic fixtures only: never construct WeChatDB or touch a real account."""
import hashlib
import json
import hmac
from pathlib import Path

import pytest

import offline_writeback as wb
from test_offline_delete_cy import encrypt_snapshot,authenticated_decrypt


class PageCodec:
    """Independent synthetic decoder: avoids importing the live-account reader in tests."""
    PAGE_SZ=4096
    RESERVE_SZ=80

    @staticmethod
    def _pbkdf2(key,salt,iterations):
        return hashlib.pbkdf2_hmac('sha512',key,salt,iterations,dklen=32)

    @staticmethod
    def _decrypt_page(key,page,pgno):
        from cryptography.hazmat.primitives.ciphers import Cipher,algorithms,modes
        decrypt=Cipher(algorithms.AES(key[:32]),modes.CBC(page[4016:4032])).decryptor()
        start=16 if pgno==1 else 0
        prefix=(page[:16] if len(key)==48 else b'SQLite format 3\0') if pgno==1 else b''
        return prefix+decrypt.update(page[start:4016])+decrypt.finalize()+bytes(80)

    @classmethod
    def _verify_enc_key(cls,key,page):
        salt=key[32:] if len(key)==48 else page[:16]
        mac_key=cls._pbkdf2(key[:32],bytes(x^0x3a for x in salt),2)
        return hmac.compare_digest(hmac.digest(mac_key,page[16:4032]+b'\x01\0\0\0','sha512'),page[4032:])


upstream=PageCodec


@pytest.mark.parametrize('explicit_salt',[False,True])
def test_page_encryption_authentication_roundtrip_and_tamper_rejection(explicit_salt):
    key=bytes(range(32))+(bytes(range(16)) if explicit_salt else b'')
    page=bytearray(4096);page[:16]=b'SQLite format 3\0';page[16:18]=(4096).to_bytes(2,'big');page[20]=80
    page[100:200]=b'A'*100
    original=bytes(page[:16]) if explicit_salt else b'S'*16
    encrypted,mac_key=encrypt_snapshot(bytes(page),original,key,upstream)
    assert len(encrypted)==4096
    assert authenticated_decrypt(encrypted,key,mac_key,upstream)==bytes(page)
    assert upstream._verify_enc_key(key,encrypted)
    damaged=bytearray(encrypted);damaged[123]^=1
    with pytest.raises(RuntimeError,match='authentication'):authenticated_decrypt(bytes(damaged),key,mac_key,upstream)


def test_encryption_requires_exact_page_geometry():
    with pytest.raises(RuntimeError):encrypt_snapshot(b'x'*4096,b'S'*16,b'K'*32,upstream)


@pytest.fixture
def plan(tmp_path,monkeypatch):
    wb.configure_target('example_account',tmp_path/'data')
    monkeypatch.setattr(wb,'RUNS',tmp_path/'runs')
    monkeypatch.setattr(wb,'require_stopped',lambda:None)
    run=wb.RUNS/'one';run.mkdir(parents=True)
    original_hashes={};changes=[]
    for i,rel in enumerate(['message\\message_0.db','message\\message_fts.db']):
        original=f'original-{i}'.encode();staged=f'changed-{i}'.encode()
        for prefix,data in [(wb.SOURCE,original),(run/'backup_db_storage',original),(run/'staged_encrypted',staged)]:
            path=prefix/rel;path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(data)
        original_hashes[rel]=hashlib.sha256(original).hexdigest()
        changes.append({'relative_path':rel,'before_sha256':original_hashes[rel],'after_sha256':hashlib.sha256(staged).hexdigest()})
    wb.save(run/'manifest.json',{'source_db_storage':str(wb.SOURCE),'account':wb.ACCOUNT,'original_hashes':original_hashes})
    wb.save(run/'plan.json',{'account':wb.ACCOUNT,'deleted_messages':2,'changes':changes})
    return run,changes


@pytest.mark.skipif(__import__('sys').platform!='win32',reason='write-back paths intentionally retain Windows schema/layout')
def test_install_and_restore_preserve_exact_bytes(plan):
    run,changes=plan
    wb.install(run)
    assert all(wb.sha(wb.checked_source(c['relative_path']))==c['after_sha256'] for c in changes)
    wb.install(run,restore=True)
    assert all(wb.sha(wb.checked_source(c['relative_path']))==c['before_sha256'] for c in changes)


@pytest.mark.skipif(__import__('sys').platform!='win32',reason='Windows write-back layout')
def test_restore_refuses_to_overwrite_newer_data(plan):
    run,changes=plan;wb.install(run)
    target=wb.checked_source(changes[0]['relative_path']);target.write_bytes(b'newer messages')
    with pytest.raises(RuntimeError,match='newer data'):wb.install(run,restore=True)
    assert target.read_bytes()==b'newer messages'


@pytest.mark.skipif(__import__('sys').platform!='win32',reason='Windows write-back layout')
def test_restore_refuses_new_wal_even_when_main_database_hash_is_unchanged(plan):
    run,changes=plan;wb.install(run)
    wal=wb.checked_source(changes[0]['relative_path']+'-wal');wal.write_bytes(b'newer uncheckpointed data')
    with pytest.raises(RuntimeError,match='journaling'):wb.install(run,restore=True)
    assert wal.read_bytes()==b'newer uncheckpointed data'


@pytest.mark.skipif(__import__('sys').platform!='win32',reason='Windows write-back layout')
def test_second_replace_failure_restores_both_original_files(plan,monkeypatch):
    import os
    run,changes=plan;replace=os.replace;count=0
    def fail_second(source,target):
        nonlocal count
        count+=1
        if count==2:raise OSError('synthetic second-file failure')
        return replace(source,target)
    monkeypatch.setattr(os,'replace',fail_second)
    with pytest.raises(OSError):wb.install(run)
    assert all(wb.sha(wb.checked_source(c['relative_path']))==c['before_sha256'] for c in changes)
    assert json.loads((run/'installation.json').read_text())['status']=='failed_and_originals_restored'


def test_target_and_manifest_paths_are_bound_to_explicit_local_account(tmp_path):
    with pytest.raises(ValueError):wb.configure_target('../escape',tmp_path)
    wb.configure_target('example_account',tmp_path)
    with pytest.raises(RuntimeError,match='escapes'):wb.checked_source('../../outside.db')
    with pytest.raises(RuntimeError,match='workspace'):wb.checked_run(tmp_path/'untrusted-run')
