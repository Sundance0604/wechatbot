"""Recover current-process context only for the authenticated WeChat 4.2.0.11 binary."""
import asyncio
import hashlib
import json
from pathlib import Path
import time

import frida
import psutil


async def recover(runtime):
    return await asyncio.to_thread(_recover,runtime)


def _recover(runtime):
    template = json.loads((Path(__file__).parent/'native/weixin_4_2_0_11.json').read_text(encoding='utf-8'))
    processes = [p for p in psutil.process_iter(['name','memory_info']) if (p.info['name'] or '').lower()=='weixin.exe']
    if not processes:
        raise RuntimeError('请先登录电脑版微信。')
    process = max(processes,key=lambda p:p.info['memory_info'].rss)
    session = frida.get_local_device().attach(process.pid)
    script = None
    try:
        script = session.create_script('''
const m=Process.getModuleByName('Weixin.dll');
let hook=null;const counts={};
rpc.exports={
 meta(){return {path:m.path,pid:Process.id};},
 watch(){hook=Interceptor.attach(m.base.add(0x4a9d40),{onEnter(){const t=Process.getCurrentThreadId();counts[t]=(counts[t]||0)+1;}});},
 scan(){
  const v=Memory.alloc(8);v.writePointer(m.base.add(0x93b68e8));
  const pattern=Array.from(new Uint8Array(v.readByteArray(8))).map(x=>x.toString(16).padStart(2,'0')).join(' ');
  const found=[];
  for(const r of Process.enumerateRanges({protection:'rw-',coalesce:true})){
   if(r.file||r.size>256*1024*1024)continue;
   try{for(const hit of Memory.scanSync(r.base,r.size,pattern)){
    try{const state=hit.address.add(0xbf8).readPointer();if(state.isNull())continue;
     const mask=state.add(0xc0).readU64().toNumber(),bucket=state.add(0xa8).readPointer();
     if(mask<1||mask>1048575||(mask&(mask+1))!==0||bucket.isNull())continue;
     bucket.readByteArray(16);found.push(hit.address.toString());
    }catch(_){}
   }}catch(_){}
  }return found;
 },
 counts(){return counts;},stop(){if(hook)hook.detach();}
};
''')
        script.load()
        meta = script.exports_sync.meta()
        if hashlib.sha256(Path(meta['path']).read_bytes()).hexdigest()!=template['module_sha256']:
            raise RuntimeError('当前微信版本尚未适配；已停止，未调用发送函数。当前仅验证4.2.0.11。')
        script.exports_sync.watch()
        candidates = script.exports_sync.scan()
        deadline = time.monotonic()+12
        counts = {}
        while time.monotonic()<deadline:
            counts = script.exports_sync.counts()
            if counts:
                break
            time.sleep(0.5)
        (runtime/'recovery_diagnostic.json').write_text(json.dumps({'pid':process.pid,'candidates':candidates,'coroutine_counts':counts},indent=2),encoding='utf-8')
        if not candidates:
            raise RuntimeError('微信尚未创建发送连接。请先完成登录，手动发送一条文字，再点击“重新连接发送”。')
        if len(candidates)!=1 or len(counts)!=1:
            raise RuntimeError('暂时无法唯一定位发送工作线程，请在微信手动发送一条消息后重新连接。')
        template.update(module_path=meta['path'],pid=process.pid,process_creation_time=process.create_time(),
                        manager_address=candidates[0],ui_thread_id=int(next(iter(counts))),
                        recipient='filehelper',recipient_name='文件传输助手',text='Violet预检')
        path = runtime/'native_profile.json'
        temporary = path.with_suffix('.tmp')
        temporary.write_text(json.dumps(template,ensure_ascii=False,indent=2),encoding='utf-8')
        temporary.replace(path)
        return {'connected':True,'version':'4.2.0.11'}
    finally:
        if script:
            script.exports_sync.stop()
            script.unload()
        session.detach()
