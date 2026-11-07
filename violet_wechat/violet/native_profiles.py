"""Select immutable, version-specific native layouts by exact module content."""
import hashlib
import json
from pathlib import Path
import struct

BASE = Path(__file__).resolve().parent/'native'


def _file_prefix(data, rva):
    pe = struct.unpack_from('<I',data,60)[0]
    sections = pe+24+struct.unpack_from('<H',data,pe+20)[0]
    for index in range(struct.unpack_from('<H',data,pe+6)[0]):
        _,va,size,raw = struct.unpack_from('<IIII',data,sections+index*40+8)
        if va<=rva and rva+8<=va+size:
            return data[raw+rva-va:raw+rva-va+8].hex()
    raise RuntimeError('发送函数不在已验证模块的文件映射内。')


def load_module_profile(module_path):
    data = Path(module_path).read_bytes()
    digest = hashlib.sha256(data).hexdigest()
    for path in sorted(BASE.glob('weixin_*.json')):
        profile = json.loads(path.read_text(encoding='utf-8'))
        if profile.get('module_sha256')!=digest:
            continue
        # Preserve the original authenticated build's layout and sized-delete thunk.
        if path.name=='weixin_4_2_0_11.json':
            profile.update(version='4.2.0.11',manager_vtable_rva='0x93b68e8',
                           manager_state_offset=0xbf8,state_bucket_offset=0xa8,state_mask_offset=0xc0)
            profile['functions'].update(coroutine_entry='0x4a9d40',operator_delete='0x76a4400')
            profile['prefixes'].update(coroutine_entry='554883ec20488d6c',
                operator_delete=_file_prefix(data,0x76a4400))
        if set(profile['functions'])!=set(profile['prefixes']):
            raise RuntimeError('原生适配配置缺少函数校验。')
        for name,rva in profile['functions'].items():
            if _file_prefix(data,int(rva,16))!=profile['prefixes'][name]:
                raise RuntimeError('原生函数校验失败：'+name)
        return profile
    raise RuntimeError('当前微信版本尚未适配；已停止，未调用发送函数。当前仅验证4.2.0.11和4.2.0.13的指定模块。')


def bind_runtime_profile(cached):
    """Never trust cached offsets; retain only bindings to this process instance."""
    profile = load_module_profile(cached['module_path'])
    for name in ('module_path','pid','process_creation_time','manager_address','ui_thread_id'):
        profile[name] = cached[name]
    profile.update(recipient='filehelper',text='Violet预检')
    return profile
