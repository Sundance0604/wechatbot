'use strict';
const M={group:'',data:null,draft:'',dirty:false,loading:false,error:'',request:0};
function memoryURL(){return '/groups/'+encodeURIComponent(M.group)+'/memory'}
async function loadMemory(group=M.group){
 const request=++M.request;M.group=group;M.loading=true;M.error='';render();
 try{const data=await api(memoryURL());if(request!==M.request)return;M.data=data;M.draft=data.content;M.dirty=false}
 catch(err){if(request===M.request){M.data=null;M.error=err.message;toast(err.message,true)}}
 finally{if(request===M.request){M.loading=false;render()}}
}
function memoryPage(){
 if(!S.cfg.groups.length)return `<section class="panel">${empty('先选择一个群聊','每个群有独立的 Markdown 长期记忆。','groups')}</section>`;
 if(!M.group||!S.cfg.groups.some(g=>g.id===M.group)){M.group=S.cfg.groups[0].id;M.data=null;M.dirty=false;queueMicrotask(()=>loadMemory())}
 const data=M.data,ready=data&&data.group_id===M.group;
 return `<section class="panel form-panel"><div class="panel-head"><div><h3>${icon('groups')} 群聊长期记忆</h3><p>确认后写入 Markdown；聊天与本群发起的 Agent 任务都会读取</p></div><span id="memory-dirty-hint">${badge(M.dirty?'草稿未保存':'各群独立',M.dirty?'purple':'')}</span></div>${field('选择群聊',`<select id="memory-group" ${M.loading||S.busy?'disabled':''}>${S.cfg.groups.map(g=>`<option value="${e(g.id)}" ${g.id===M.group?'selected':''}>${e(g.name)}</option>`).join('')}</select>`)}<div class="inline-note">记住确认的称呼、稳定偏好和共同背景。开始监听后，本群已确认记忆会随聊天上下文发送至 api.deepseek.com；本群 Agent 任务会交给本机已登录的 Codex 处理。待确认建议仅留本机，记忆不改变管理员或工具权限。</div>${ready?`${field('已确认的 Markdown',`<textarea id="memory-content" ${S.busy||M.loading?'readonly':''} rows="14" maxlength="16000" spellcheck="false" placeholder="# 本群长期上下文\n\n## 称呼与背景\n\n## 参与偏好">${e(M.draft)}</textarea>`,'可直接修改；保存后下一轮读取，最近十次旧版本保留在同目录的 history 文件夹。')}<p class="memory-path">本机文件：${e(data.path)}</p><div class="buttons">${btn('保存长期记忆','save-memory','check','primary',S.busy||M.loading||!M.dirty)}${btn('重新载入','reload-memory','refresh','',S.busy||M.loading)}<span class="mono" id="memory-length">${M.draft.length} / 16000 字</span></div>`:M.loading?'<p class="loading-inline">正在读取本群记忆…</p>':`<div class="alert">${e(M.error||'记忆尚未载入')}${btn('重新载入','reload-memory','refresh','small',S.busy)}</div>`}</section>
 ${ready?`<section class="panel memory-candidates"><div class="panel-head"><div><h3>待确认建议 · ${data.candidates.length}</h3><p>根据群友原消息提出。核对来源后确认，临时玩笑和模型猜测可直接忽略。</p></div>${btn('刷新建议','refresh-memory-candidates','refresh','small',S.busy||M.loading)}</div>${data.candidates.length?data.candidates.map(v=>`<article class="memory-candidate"><strong>${e(v.text)}</strong>${v.evidence.map(s=>`<blockquote><small>${e(s.speaker)} · ${new Date(s.time*1000).toLocaleString('zh-CN')}</small><p>${e(s.text)}</p></blockquote>`).join('')}<div class="buttons">${btn('确认并写入','accept-memory:'+v.id,'check','primary small',S.busy||M.dirty||M.loading)}${btn('忽略','reject-memory:'+v.id,'close','small',S.busy||M.dirty||M.loading)}</div></article>`).join(''):empty('暂时没有新建议','聊天生成的建议会保留在这里；管理员也可在群中发送“/记住 内容”直接保存。')}${M.dirty?'<p class="loading-inline">先保存或重新载入 Markdown 草稿，再处理建议。</p>':''}</section>`:''}`;
}
function handleMemoryAction(name,arg){
 if(!['save-memory','reload-memory','accept-memory','reject-memory','refresh-memory-candidates'].includes(name))return false;
 if(name==='reload-memory'){
  if(M.dirty&&!window.confirm('重新载入会放弃尚未保存的草稿，是否继续？'))return true;
  loadMemory();return true;
 }
 action(async()=>{
  if(name==='save-memory'){M.data=await api(memoryURL(),'PUT',{content:M.draft,version:M.data.version});M.draft=M.data.content;M.dirty=false;toast('本群长期记忆已保存，下一轮自动读取。')}
  if(name==='refresh-memory-candidates'){const data=await api(memoryURL());if(!M.dirty){M.data=data;M.draft=data.content}else M.data.candidates=data.candidates;toast('记忆建议已刷新。')}
  if(name==='accept-memory'||name==='reject-memory'){
   if(M.dirty)throw Error('请先保存或重新载入草稿。');
   M.data=await api(memoryURL()+'/candidates/'+encodeURIComponent(arg),'POST',{accept:name==='accept-memory'});M.draft=M.data.content;toast(name==='accept-memory'?'已确认并写入本群 Markdown。':'已忽略这条建议。');
  }
 });return true;
}
document.addEventListener('input',event=>{if(event.target.id==='memory-content'){M.draft=event.target.value;M.dirty=true;document.querySelector('#memory-dirty-hint').innerHTML=badge('草稿未保存','purple');document.querySelector('#memory-length').textContent=M.draft.length+' / 16000 字';const button=document.querySelector('[data-action="save-memory"]');if(button)button.disabled=S.busy;document.querySelectorAll('[data-action^="accept-memory:"],[data-action^="reject-memory:"]').forEach(b=>b.disabled=true)}});
document.addEventListener('change',event=>{if(event.target.id==='memory-group'){
 const group=event.target.value;if(M.dirty&&!window.confirm('切换群聊会放弃尚未保存的草稿，是否继续？')){event.target.value=M.group;return}loadMemory(group);
}});
