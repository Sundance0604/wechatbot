'use strict';
const OP={data:null,draft:'',dirty:false,loading:false,error:''};
async function loadOwnerProfile(){
 OP.loading=true;OP.error='';render();
 try{OP.data=await api('/profile');OP.draft=OP.data.content;OP.dirty=false}
 catch(err){OP.error=err.message;toast(err.message,true)}
 finally{OP.loading=false;render()}
}
function profilePage(){
 if(!OP.data&&!OP.loading&&!OP.error)queueMicrotask(loadOwnerProfile);
 return `<section class="panel form-panel"><div class="panel-head"><div><h3>${icon('shield')} 账号主人的介绍</h3><p>由你维护，供 AI 助手理解你的基本背景与表达边界</p></div><span id="profile-dirty-hint">${badge(OP.dirty?'草稿未保存':'主人资料','purple')}</span></div><div class="inline-note">这份资料会用于已开启的群聊、AI 对话和 Codex 任务。使用 DeepSeek 时会随上下文发送至 api.deepseek.com；任务交给本机已登录的 Codex。只保存你愿意用于这些交流的内容。群聊记忆建议不会修改它。</div>${OP.data?`${field('主人资料 · Markdown',`<textarea id="owner-profile" rows="22" maxlength="20000" ${S.busy||OP.loading?'readonly':''} placeholder="# 我的公开介绍\n\n## 基本背景\n\n## 交流习惯\n\n## 可以透露的内容与边界">${e(OP.draft)}</textarea>`,'机器人使用你的授权背景，以你的 AI 助手身份交流；不冒充本人，不编造你的经历或私人意见。')}<p class="memory-path">本机文件：${e(OP.data.path)}</p><div class="buttons">${btn('保存主人资料','save-profile','check','primary',S.busy||OP.loading||!OP.dirty)}${btn('重新载入','reload-profile','refresh','',S.busy||OP.loading)}<span class="mono" id="profile-length">${OP.draft.length} / 20000 字</span></div>`:OP.error?`<div class="alert">${e(OP.error)}${btn('重新载入','reload-profile','refresh','small',OP.loading)}</div>`:'<p class="loading-inline">正在读取主人资料…</p>'}</section>`;
}
function handleProfileAction(name){
 if(!['save-profile','reload-profile'].includes(name))return false;
 if(name==='reload-profile'){
  if(OP.dirty&&!window.confirm('重新载入会放弃尚未保存的主人资料草稿，是否继续？'))return true;
  loadOwnerProfile();return true;
 }
 action(async()=>{OP.data=await api('/profile','PUT',{content:OP.draft,version:OP.data.version});OP.draft=OP.data.content;OP.dirty=false;toast('主人资料已保存，后续对话和任务自动读取。')});return true;
}
document.addEventListener('input',event=>{if(event.target.id==='owner-profile'){
 OP.draft=event.target.value;OP.dirty=true;document.querySelector('#profile-dirty-hint').innerHTML=badge('草稿未保存','purple');document.querySelector('#profile-length').textContent=OP.draft.length+' / 20000 字';document.querySelector('[data-action="save-profile"]').disabled=S.busy||OP.loading;
}});
