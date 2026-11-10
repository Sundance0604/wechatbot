import React,{useEffect,useState} from 'react';
import {Download,FileText,RefreshCw,TriangleAlert} from 'lucide-react';
import {Button,Empty} from '../shared/ui';
import {api,downloadDiagnostics} from './api';
import './diagnostics.css';

const kinds={connect:'连接账号',search:'查询消息',prepare:'备份与准备',execute:'写入清理',restore:'恢复备份',recover:'恢复中断',api:'界面请求'};
const states={running:'执行中',completed:'已完成',failed:'失败',interrupted:'程序中断'};
const stages={starting:'开始检查',connect_database:'连接数据库',query_messages:'查询消息',backup_files:'备份文件',decode_and_validate:'解密备份与核对消息',update_messages_and_fts:'更新消息与全文索引',encrypt_and_verify:'加密与逐页验证',verify_and_install:'核对并写入',verify_and_restore:'核对并恢复',verify_and_recover:'恢复中断操作',request:'界面请求'};
const stamp=value=>value?new Date(value).toLocaleString('zh-CN'):'—';

export default function Diagnostics({onError}){
 const [data,setData]=useState({tasks:[]}),[loading,setLoading]=useState(false),[exporting,setExporting]=useState(false),[selected,setSelected]=useState('');
 const refresh=async()=>{setLoading(true);try{setData(await api('/diagnostics/tasks'));}catch(error){onError(error.message);}finally{setLoading(false);}};
 useEffect(()=>{refresh();const timer=setInterval(refresh,3000);return()=>clearInterval(timer);},[]);
 const download=async()=>{setExporting(true);try{await downloadDiagnostics();}catch(error){onError(error.message);}finally{setExporting(false);}};
 const item=data.tasks.find(t=>t.id===selected)||data.tasks[0];
 return <><div className="page-heading"><div className="eyebrow">TASKS & DIAGNOSTICS</div><h1>把问题发生的位置，记录下来。</h1><p>失败后直接导出诊断包，汇总不同电脑上的任务和结构差异。重启后仍会保留记录。</p></div>
 <section className="panel diagnostics-panel"><div className="panel-heading"><div><h3><FileText size={18}/>任务与诊断</h3><p>共 {data.tasks.length} 次任务 · {data.tasks.filter(t=>['failed','interrupted'].includes(t.state)).length} 次失败或中断</p></div><div className="diagnostic-actions"><Button icon={RefreshCw} onClick={refresh} disabled={loading}>刷新</Button><Button icon={Download} variant="primary" onClick={download} disabled={exporting}>{exporting?'正在导出':'导出诊断包'}</Button></div></div>
 <div className="diagnostic-note"><TriangleAlert size={18}/><p>包内包含真实路径、账号、会话、删除词、程序环境、阶段日志与错误堆栈。请仅分享给协助排查的人。不会自动上传，不包含数据库文件和密钥。旧版产生的报错需要用新版重新操作一次才能记录。</p></div>
 {data.unreadable_records>0&&<p className="inline-notice">有 {data.unreadable_records} 条诊断记录无法读取，其余记录仍可导出。</p>}
 {!item?<Empty icon={FileText} title="还没有任务日志" description="连接、查询、准备、清理及恢复操作会自动记录。现在也可以导出程序环境信息。"/>:<div className="diagnostic-layout"><div className="diagnostic-list">{data.tasks.map(task=><button key={task.id} className={'diagnostic-item '+(item.id===task.id?'active':'')} onClick={()=>setSelected(task.id)}><strong>{kinds[task.kind]||task.kind}<span className={task.state==='failed'?'diagnostic-failed':''}>{states[task.state]||task.state}</span></strong><small>{stamp(task.started_at)}</small><small>{stages[task.stage]||task.stage}</small></button>)}</div><article className="diagnostic-detail"><h3>{kinds[item.kind]} · {states[item.state]}</h3><dl><dt>任务编号</dt><dd>{item.id}</dd><dt>程序版本</dt><dd>{item.environment?.app_version}</dd><dt>执行阶段</dt><dd>{stages[item.stage]||item.stage}</dd><dt>开始 / 结束</dt><dd>{stamp(item.started_at)} / {stamp(item.ended_at)}</dd></dl>
 {item.error&&<div className="diagnostic-error"><strong>{item.error.code}</strong><p>{item.error.message}</p><p>代码位置：{item.error.location.file}:{item.error.location.line} · {item.error.location.function}</p><small>同类错误编号：{item.error.signature}</small></div>}
 <details><summary>任务上下文</summary><pre>{JSON.stringify(item.context,null,2)}</pre></details><ol className="diagnostic-events">{item.events.map((event,index)=><li key={index}><strong>{event.name==='stage'?(stages[event.details.stage]||event.details.stage):event.name}</strong><small>{stamp(event.at)}</small>{event.name!=='stage'&&<details><summary>查看记录详情</summary><pre>{JSON.stringify(event.details,null,2)}</pre></details>}</li>)}</ol>
 {item.error&&<details><summary>错误调用堆栈</summary><pre>{JSON.stringify(item.error,null,2)}</pre></details>}</article></div>}
 </section></>;
}
