// Native Harness tools execute the task. This monotonic guard limits their scope.
import fs from 'node:fs';
import path from 'node:path';

export const name = 'violet-workspace-policy';
export const inject = ['tools'];
const forbidden = new Set(['.git','.codex','.agents','.dsh','.ssh','.aws','config.local.json']);
const hidden = part => forbidden.has(part.toLowerCase()) || /^\.env(?:\.|$)/i.test(part) || /\.(pem|key|pfx|p12)$/i.test(part);

export function createGuard(policy) {
  const root = fs.realpathSync(policy.root);
  function locate(value = '.') {
    if (typeof value !== 'string' || !value || value.includes('\0')) throw Error('无效路径');
    const full = path.resolve(root,value), relative = path.relative(root,full);
    if (relative === '..' || relative.startsWith('..'+path.sep) || path.isAbsolute(relative)) throw Error('不能直接访问工作区之外的路径');
    const segments = relative ? relative.split(path.sep) : [];
    if (segments.some(part => hidden(part) || part.includes(':') || /[. ]$/.test(part))) throw Error('此路径不向项目成员开放');
    let current = root;
    for (const part of segments) {
      current = path.join(current,part);
      let st;
      try { st = fs.lstatSync(current); } catch (e) { if (e.code === 'ENOENT') continue; throw e; }
      if (st.isSymbolicLink() || (st.isFile() && st.nlink !== 1)) throw Error('不允许直接操作链接文件或目录');
      const rel = path.relative(root,fs.realpathSync(current));
      if (rel === '..' || rel.startsWith('..'+path.sep) || path.isAbsolute(rel)) throw Error('目录已越界');
    }
    return full;
  }
  // Native discovery recursively opens files. Refuse trees with links/private files
  // instead of relying on version-dependent grep/glob follow-link behavior.
  function discoveryTree(rootPath) {
    const pending=[rootPath];let count=0;
    while (pending.length) {
      if (++count>10000) throw Error('目录过大，请指定更小的子目录');
      const folder=pending.pop();
      for (const entry of fs.readdirSync(folder,{withFileTypes:true})) {
        if (['.git','.hg','.svn'].includes(entry.name)) continue; // Native rg excludes VCS metadata.
        if (hidden(entry.name) || entry.isSymbolicLink()) throw Error('该目录含私密路径或链接，请指定具体文件或更小的子目录');
        if (entry.isDirectory()) pending.push(path.join(folder,entry.name));
      }
    }
  }
  return execution => {
    try {
      const args=execution.arguments || {}, tool=execution.name;
      if (args.sandbox_permissions && args.sandbox_permissions!==policy.access) return '不能扩大项目沙箱权限';
      if (['read','read_image','write','edit'].includes(tool)) {
        if (['write','edit'].includes(tool) && policy.access!=='workspace-write') return '工作区为只读';
        locate(args.file_path);return;
      }
      if (['glob','grep','ls'].includes(tool)) {
        if (tool==='glob' && (typeof args.pattern!=='string' || args.pattern.split(/[\\/]/).includes('..') || path.isAbsolute(args.pattern) || args.pattern.includes(':'))) return '搜索范围不能越过工作区';
        const target=locate(args.path || '.');
        if (tool==='grep' && fs.statSync(target).isDirectory()) discoveryTree(target);
        return;
      }
      if (['bash','pwsh'].includes(tool)) {
        if (!policy.commands) return '主人尚未为此工作区开放终端执行';
        if (typeof args.command!=='string') return '缺少终端命令';
        locate(args.workdir || '.');return;
      }
      if (['web_search','web_fetch'].includes(tool)) return policy.network ? undefined : '此工作区未开放联网工具';
      if (['todo_write','job_output','job_kill'].includes(tool)) return;
      return '此项目未开放该工具；不能修改权限、工作区、插件或跨会话发信';
    } catch (error) { return error.message; }
  };
}

export async function apply(ctx) {
  const policy = JSON.parse(process.env.VIOLET_DSH_POLICY || '{}');
  ctx.tools.guard(createGuard(policy));
  const {Service} = await import('@deepseek-ai/cordis');
  // ACP cannot admit any session unless policy registration succeeds.
  class ProjectPolicy extends Service { constructor(context) { super(context,'violetProjectPolicy'); } }
  new ProjectPolicy(ctx);
}
