import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import {createGuard,apply} from '../violet/harness/workspace_policy.mjs';

const root=fs.mkdtempSync(path.join(os.tmpdir(),'violet-policy-'));
try {
  fs.writeFileSync(path.join(root,'main.py'),'print(1)');
  const policy={root,access:'workspace-write',commands:true,network:false};
  const guard=createGuard(policy), check=(name,args={})=>guard({name,arguments:args});
  assert.equal(check('read',{file_path:'main.py'}),undefined);
  assert.equal(check('write',{file_path:'src/new.py',content:'x'}),undefined);
  assert.ok(check('read',{file_path:'../outside'}));
  assert.ok(check('read',{file_path:'.env'}));
  assert.ok(check('write',{file_path:'.git/hooks/pre-commit'}));
  assert.ok(check('write',{file_path:'main.py:stream'}));
  assert.ok(check('glob',{pattern:'../**'}));
  assert.ok(check('web_fetch',{url:'https://example.com'}));
  assert.ok(check('unknown_future_tool',{}));
  assert.ok(check('subagent',{}));
  assert.equal(check('pwsh',{command:'python -m pytest',workdir:root}),undefined);
  assert.ok(check('pwsh',{command:'whoami',workdir:'..'}));
  assert.ok(check('pwsh',{command:'whoami',sandbox_permissions:'danger-full-access'}));
  const readonly=createGuard({...policy,access:'read-only',commands:false});
  assert.ok(readonly({name:'write',arguments:{file_path:'main.py',content:'x'}}));
  assert.ok(readonly({name:'pwsh',arguments:{command:'whoami'}}));
  fs.linkSync(path.join(root,'main.py'),path.join(root,'hardlink.py'));
  assert.ok(check('read',{file_path:'hardlink.py'}));
  process.env.VIOLET_DSH_POLICY=JSON.stringify(policy);
  await assert.rejects(apply({tools:{guard(){throw Error('guard failed')}}}),/guard failed/);
  console.log('Harness policy checks passed');
} finally {
  // This exact temporary directory was created by this test.
  for (const name of ['main.py','hardlink.py']) {
    const filename=path.join(root,name);
    if (fs.existsSync(filename)) fs.unlinkSync(filename);
  }
  fs.rmdirSync(root);
}
