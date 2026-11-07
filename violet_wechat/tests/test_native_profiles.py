"""Compatibility selection must reject stale/unknown binaries before native use."""
import hashlib
import json
from pathlib import Path
import struct

import pytest
from violet import native_profiles


@pytest.fixture
def binary_profile(tmp_path, monkeypatch):
    data=bytearray(0x400)
    struct.pack_into('<I',data,0x3c,0x80)
    struct.pack_into('<H',data,0x80+6,1)
    struct.pack_into('<H',data,0x80+20,0xf0)
    section=0x80+24+0xf0
    struct.pack_into('<IIII',data,section+8,0x200,0x1000,0x200,0x200)
    data[0x200:0x208]=bytes.fromhex('554883ec20488d6c')
    module=tmp_path/'Weixin.dll';module.write_bytes(data)
    profile={'version':'test','module_sha256':hashlib.sha256(data).hexdigest(),
             'functions':{'entry':'0x1000'},'prefixes':{'entry':data[0x200:0x208].hex()},
             'manager_vtable_rva':'0x1200','manager_state_offset':0xbf8}
    native=tmp_path/'native';native.mkdir()
    template=native/'weixin_test.json';template.write_text(json.dumps(profile),encoding='utf-8')
    monkeypatch.setattr(native_profiles,'BASE',native)
    return module,template,profile


def test_exact_module_selects_profile_and_mapped_prefix(binary_profile):
    module,_,profile=binary_profile
    assert native_profiles.load_module_profile(module)==profile


def test_changed_binary_never_attaches(binary_profile, tmp_path, monkeypatch):
    from violet.native_sender import Sender
    import frida
    module,_,_=binary_profile
    module.write_bytes(module.read_bytes()+b'changed build')
    cached=tmp_path/'cached.json'
    cached.write_text(json.dumps({'module_path':str(module)}),encoding='utf-8')
    def forbidden():raise AssertionError('must reject before attaching')
    monkeypatch.setattr(frida,'get_local_device',forbidden)
    with pytest.raises(RuntimeError,match='尚未适配'):Sender(cached)


@pytest.mark.parametrize('change',['prefix','missing_prefix'])
def test_incomplete_or_wrong_prefix_rejected(binary_profile,change):
    module,template,profile=binary_profile
    profile['prefixes']={} if change=='missing_prefix' else {'entry':'0000000000000000'}
    template.write_text(json.dumps(profile),encoding='utf-8')
    with pytest.raises(RuntimeError,match='校验'):native_profiles.load_module_profile(module)


def test_cached_offsets_are_replaced_with_authenticated_layout(binary_profile):
    module,_,profile=binary_profile
    cached={'module_path':str(module),'pid':123,'process_creation_time':42,
            'manager_address':'0x12340000','ui_thread_id':456,
            'functions':{'entry':'0xdeadbeef'},'manager_vtable_rva':'0xbad',
            'recipient':'untrusted_destination','text':'untrusted message'}
    bound=native_profiles.bind_runtime_profile(cached)
    assert bound['functions']==profile['functions']
    assert bound['manager_vtable_rva']==profile['manager_vtable_rva']
    assert bound['recipient']=='filehelper' and bound['text']=='Violet预检'
    assert bound['pid']==123 and bound['ui_thread_id']==456


def test_prefix_must_fit_wholly_in_file_section(binary_profile):
    module,_,_=binary_profile
    with pytest.raises(RuntimeError,match='文件映射'):
        native_profiles._file_prefix(module.read_bytes(),0x11fc)
