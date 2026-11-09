let token = '';
export function setToken(value){token=value;}
export async function api(path, options={}){
  const headers={'X-Cleaner-Client':'1', 'X-Cleaner-Token':token,...options.headers};
  if(options.body){headers['Content-Type']='application/json';options={...options,body:JSON.stringify(options.body)};}
  const response=await fetch('/api'+path,{...options,headers});
  let value;try{value=await response.json();}catch{throw new Error('无法连接本地程序，请重新启动清简。');}
  if(!response.ok)throw new Error(typeof value.detail==='string'?value.detail:'操作未完成，请重试。');
  return value;
}
