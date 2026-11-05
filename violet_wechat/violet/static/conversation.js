'use strict';
function conversationControls(g,index){
 if(!g.id.endsWith('@chatroom'))return `<div class="conversation-state"><div>${badge(S.status.groups?.[g.id]?.paused?'回复已暂停':(S.status.conversation?.[g.id]?.quiet_remaining||0)>0?'私聊安静中':'私聊直接回应')}<small>安静期间普通消息不回复；新 @ 可单次呼叫</small></div>${btn('恢复回复','wake-group:'+g.id,'play','small',S.busy)}${btn('安静一会','quiet-group:'+g.id,'stop','small',S.busy)}</div><div class="form-grid">${field('本会话机器人称呼',`<input data-group="${index}" data-property="bot_name" value="${e(g.bot_name||'')}" placeholder="留空使用全局称呼">`)}${field('安静时可呼叫的 @ 称呼',`<input data-group="${index}" data-property="mentions" value="${e((g.mentions||[]).join('、'))}">`)}</div>${field('Agent 携带近期消息 / 条',`<input type="number" min="0" max="200" data-group="${index}" data-property="agent_context_messages" value="${g.agent_context_messages??50}">`)}${field('要求安静后保持 / 秒',`<input type="number" min="10" max="3600" data-group="${index}" data-property="quiet_seconds" value="${g.quiet_seconds??120}">`)}`;
 const state=S.status.conversation?.[g.id]||{},paused=S.status.groups?.[g.id]?.paused;
 const setting=(label,name,value,min,max,hint='')=>field(label,`<input type="number" data-group="${index}" data-property="${name}" value="${value}" min="${min}" max="${max}">`,hint);
 return `${field('聊天风格',`<select data-group="${index}" data-property="chat_style"><option value="balanced" ${(g.chat_style||'balanced')==='balanced'?'selected':''}>稳健参与</option><option value="classmates" ${g.chat_style==='classmates'?'selected':''}>熟人互损 · 接梗、反击、少叠甲</option><option value="family" ${g.chat_style==='family'?'selected':''}>家族群 · 亲切稳重</option></select>`)}<div class="conversation-state"><div>${badge(paused?'闲聊已暂停':state.quiet_remaining>0?'闲聊安静中':state.blocked_by==='cooldown'?'闲聊冷却中':state.blocked_by==='budget'?'闲聊预算已满':'可以判断是否参与',state.quiet_remaining>0||paused?'purple':'')}<small>窗口内已发 ${state.used_messages||0} 条 · ${state.used_characters||0} 字</small>${state.quiet_remaining>0?`<small>安静至 ${when(state.quiet_until)}</small>`:''}</div>${btn(paused||state.quiet_remaining>0?'恢复闲聊':'安静一会',`${paused||state.quiet_remaining>0?'wake-group':'quiet-group'}:${g.id}`,paused||state.quiet_remaining>0?'play':'stop','small',S.busy)}</div>
 <div class="form-grid">${field('本群机器人称呼',`<input data-group="${index}" data-property="bot_name" value="${e(g.bot_name||'')}" placeholder="留空使用全局称呼">`)}${field('本群响应的 @ 称呼',`<input data-group="${index}" data-property="mentions" value="${e((g.mentions||[]).join('、'))}" placeholder="留空使用全局呼叫称呼">`)}</div>
 ${setting('Agent 携带近期消息 / 条','agent_context_messages',g.agent_context_messages??50,0,200,'只带来源群；0 关闭近期消息。默认最多50条，文字量过大时保留最新部分。')}
 <details class="conversation-settings"><summary>参与节奏与群友称呼</summary><p class="conversation-note">设置保存后生效；预算只限制主动插话；明确 @ 或引用机器人时直接回应，不占主动发言额度。收到闭嘴指令后安静两分钟，其间仍回应新 @。同轮分条共享冷却，跳过的普通聊天不补发。</p><div class="form-grid">
 ${setting('两轮闲聊最短间隔 / 秒','cooldown_seconds',g.cooldown_seconds??45,0,3600)}
 ${setting('预算窗口 / 秒','budget_window_seconds',g.budget_window_seconds??300,30,3600)}
 ${setting('窗口内最多主动发言 / 条','message_budget',g.message_budget??4,1,30)}
 ${setting('窗口内最多主动发言 / 字','character_budget',g.character_budget??600,50,10000)}
 ${setting('要求安静后保持 / 秒','quiet_seconds',g.quiet_seconds??120,10,3600)}
 ${setting('普通接梗最多 / 字','casual_max_chars',g.casual_max_chars??80,10,300)}</div>
 ${field('其他群友的常用称呼 / 简称',`<textarea rows="2" data-group="${index}" data-property="other_names" placeholder="一行一个，例如 friend_a、friend_b、friend_c">${e((g.other_names||[]).join('\n'))}</textarea>`,'点名这些称呼时默认不抢答。无需提供真实姓名，也不用于核验管理员。')}</details>`;
}
function handleConversationAction(name,arg){
 if(!['quiet-group','wake-group'].includes(name))return false;
 action(async()=>{S.status=await api('/groups/'+encodeURIComponent(arg)+'/quiet','POST',{quiet:name==='quiet-group'});toast(name==='quiet-group'?'本群闲聊已安静，待发旧回复已取消。':'本群闲聊已恢复，旧回复不会补发。')});return true;
}
