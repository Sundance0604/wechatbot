import React,{useEffect,useMemo,useRef,useState} from 'react';
import {MessageCircle,RefreshCw,Search,UserRound,UsersRound} from 'lucide-react';
import {Badge,Button,Empty} from '../shared/ui';

export function SelectionCheckbox({mixed=false,...props}){
 const ref=useRef(null);
 useEffect(()=>{if(ref.current)ref.current.indeterminate=mixed;},[mixed,props.checked]);
 return <input ref={ref} type="checkbox" {...props}/>;
}

export default function ConversationPicker({conversations,selected,onChange,disabled,connected,onRefresh}){
 const [kind,setKind]=useState('group'),[query,setQuery]=useState('');
 const list=useMemo(()=>conversations.filter(c=>c.type===kind),[conversations,kind]);
 const visible=useMemo(()=>list.filter(c=>c.name.toLowerCase().includes(query.trim().toLowerCase())||c.id.toLowerCase().includes(query.trim().toLowerCase())),[list,query]);
 const chosen=useMemo(()=>new Set(selected),[selected]);
 const count=list.filter(c=>chosen.has(c.id)).length;
 const title=kind==='contact'?'联系人':'群聊';
 const toggleAll=(items,remove=false)=>{
  const next=new Set(selected);
  const all=remove||items.every(c=>next.has(c.id));
  items.forEach(c=>all?next.delete(c.id):next.add(c.id));
  onChange([...next]);
 };
 return <section className="panel group-panel conversation-panel">
  <div className="panel-heading"><div><h3><MessageCircle size={17}/>选择会话</h3><p>联系人与群聊可以一起查询</p></div><Badge>{selected.length} 个已选</Badge></div>
  <div className="conversation-tabs" role="tablist" aria-label="会话分类">{[['contact','联系人',UserRound],['group','群聊',UsersRound]].map(([type,label,Icon])=><button role="tab" aria-selected={kind===type} aria-controls="conversation-list" id={'tab-'+type} key={type} className={kind===type?'active':''} onClick={()=>{setKind(type);setQuery('');}}><Icon size={15}/>{label}<span>{conversations.filter(c=>c.type===type).length}</span></button>)}</div>
  <label className="search-field"><Search size={16}/><input aria-label={'搜索'+title} placeholder={kind==='contact'?'搜索联系人名称或微信标识':'搜索群名称或群标识'} value={query} onChange={e=>setQuery(e.target.value)} disabled={!connected||disabled}/></label>
  <div className="conversation-bulk"><label><SelectionCheckbox aria-label={'全选'+title} checked={list.length>0&&count===list.length} mixed={count>0&&count<list.length} disabled={disabled||!connected||!list.length} onChange={()=>toggleAll(list)}/>全选{title}</label><span>{count} / {list.length}</span><button disabled={disabled||!count} onClick={()=>toggleAll(list,true)}>清空本类</button></div>
  {query.trim()&&<div className="filtered-bulk"><span>找到 {visible.length} 个{title}</span><button disabled={disabled||!visible.length} onClick={()=>toggleAll(visible)}>全选 / 取消当前搜索结果</button></div>}
  <div className="group-list" role="tabpanel" id="conversation-list" aria-labelledby={'tab-'+kind}>{visible.length?visible.map(c=><label className={'group-row '+(chosen.has(c.id)?'selected':'')} key={c.id}><input type="checkbox" aria-label={`选择${title}：${c.name}（${c.id}）`} checked={chosen.has(c.id)} disabled={disabled} onChange={()=>{const next=new Set(selected);next.has(c.id)?next.delete(c.id):next.add(c.id);onChange([...next]);}}/><span className="group-avatar">{kind==='contact'?<UserRound size={18}/>:<MessageCircle size={18}/>}</span><span><strong>{c.name}</strong><small title={c.id}>{c.id}</small></span></label>):<Empty icon={kind==='contact'?UserRound:MessageCircle} title={connected?'没有找到'+title:'连接后显示会话'} description={connected?'试试其他名称，或重新连接刷新会话列表。':'选择本机微信账号，读取联系人和群聊。'}/>}</div>
  <div className="group-footer"><span>共 {list.length} 个{title}</span><button disabled={disabled||!connected} onClick={onRefresh} aria-label="刷新会话"><RefreshCw size={14}/></button></div>
 </section>;
}
