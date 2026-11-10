let token = '';
export function setToken(value){token=value;}
export async function api(path, options={}){
  const headers={'X-Cleaner-Client':'1', 'X-Cleaner-Token':token,...options.headers};
  if(options.body){headers['Content-Type']='application/json';options={...options,body:JSON.stringify(options.body)};}
  const response=await fetch('/api'+path,{...options,headers});
  let value;try{value=await response.json();}catch{throw new Error('无法连接本地程序，请重新启动清简。');}
  if(!response.ok)throw new Error((typeof value.detail==='string'?value.detail:'操作未完成，请重试。')+(value.diagnostic_id?'（已记录诊断，可导出）':''));
  return value;
}

export async function downloadDiagnostics(){
 const response=await fetch('/api/diagnostics/export',{headers:{'X-Cleaner-Client':'1','X-Cleaner-Token':token}});
 if(!response.ok)throw new Error('诊断包导出失败，请检查程序是否仍在运行。');
 const blob=await response.blob();
 const url=URL.createObjectURL(blob),link=document.createElement('a');
 link.href=url;link.download=response.headers.get('Content-Disposition')?.match(/filename="([^"]+)"/)?.[1]||'WeChatCleaner-diagnostics.zip';
 document.body.append(link);link.click();link.remove();setTimeout(()=>URL.revokeObjectURL(url),30000);
}
