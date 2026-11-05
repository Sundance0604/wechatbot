import asyncio
import json
from pathlib import Path

from fastapi.testclient import TestClient
import pytest

from violet.agent import AgentManager
from violet.engine import Engine
from violet.settings import ConfigStore
from violet.store import Store
from violet.web import create_app


class FakeRPC:
    calls=[]
    tools=[]
    closed=0
    mode='normal'
    release=None
    live=0
    peak=0
    def __init__(self,binary,cwd,on_event=None,on_request=None,options=None):
        self.cwd,self.event,self.request=cwd,on_event,on_request
        self.background=None
    async def start(self):return {}
    async def call(self,method,params,timeout=60):
        type(self).calls.append((method,params))
        if method=='account/read':return {'account':{'type':'chatgpt'}}
        if method in ['thread/start','thread/resume']:
            if method=='thread/start':type(self).tools=params.get('dynamicTools',[])
            return {'thread':{'id':params.get('threadId','thread_test')}}
        if method=='turn/interrupt':
            return {}
        if method=='turn/start':
            self.background=asyncio.create_task(self.work())
            return {'turn':{'id':'turn_test'}}
        return {}
    async def work(self):
        cls=type(self);cls.live+=1;cls.peak=max(cls.peak,cls.live)
        try:
            if cls.mode=='wait':await cls.release.wait()
            if cls.mode=='approval':
                decision=await self.request('item/commandExecution/requestApproval',{'itemId':'cmd','command':'python test.py','cwd':self.cwd,'reason':'运行验证'})
                if decision['decision']!='accept':
                    await self.event('turn/completed',{'turn':{'status':'failed'}});return
            if cls.mode=='send':
                result=await self.request('item/tool/call',{'tool':'violet_send_message','arguments':{'recipient':'联系人','text':'管理员授权正文'},'callId':'unique_call'})
                await self.event('item/completed',{'item':{'type':'agentMessage','phase':'final_answer','text':str(result['success'])}})
            else:
                await self.event('item/completed',{'item':{'type':'agentMessage','phase':'final_answer','text':'任务已完成'}})
            await self.event('turn/completed',{'turn':{'status':'completed'}})
        finally:cls.live-=1
    async def close(self):
        type(self).closed+=1
        if self.background:
            self.background.cancel();await asyncio.gather(self.background,return_exceptions=True)


@pytest.fixture
def manager(tmp_path,monkeypatch):
    import violet.agent as module
    monkeypatch.setattr(module,'codex_binary',lambda configured='':'fake_codex')
    FakeRPC.calls=[];FakeRPC.tools=[];FakeRPC.closed=0;FakeRPC.mode='normal';FakeRPC.live=0;FakeRPC.peak=0
    workspace=tmp_path/'project';workspace.mkdir()
    config=ConfigStore(tmp_path/'config.json')
    config.update({'workspaces':[{'id':'project','name':'测试项目','path':str(workspace)}]})
    store=Store(tmp_path/'state.db')
    manager=AgentManager(config,store,tmp_path,rpc_factory=FakeRPC)
    yield manager
    store.close()


async def finish(manager,identifier):
    task=manager.tasks.get(identifier)
    if task:await asyncio.wait_for(task,2)
    return manager.store.job(identifier)


def test_agent_reads_only_its_source_group_confirmed_markdown(manager):
    from violet.memory import GroupMemory
    from violet.settings import Group
    manager.config.value.groups=[Group(id='a@chatroom',name='甲群'),Group(id='b@chatroom',name='乙群')]
    memory=GroupMemory(manager.runtime/'group_memory')
    memory.save('a@chatroom','甲群','甲群独有背景')
    memory.save('b@chatroom','乙群','乙群独有背景')
    async def run():
        for source,expected,excluded in [('a@chatroom','甲群独有背景','乙群独有背景'),
                                         ('b@chatroom','乙群独有背景','甲群独有背景'),
                                         ('','','甲群独有背景')]:
            FakeRPC.calls=[]
            job=await manager.submit('project','当前管理员的任务',owner='owner' if source else '@panel',source=source)
            assert (await finish(manager,job['id']))['status']=='completed'
            turn=next(p for m,p in FakeRPC.calls if m=='turn/start')
            text=json.dumps(turn['input'],ensure_ascii=False)
            if expected:assert expected in text
            else:assert len(turn['input'])==1
            assert excluded not in text and turn['input'][-1]['text']=='当前管理员的任务'
        await manager.close()
    asyncio.run(run())


def test_agent_reads_owner_authored_profile_without_changing_task_owner(manager):
    from violet.profile import OwnerProfile
    OwnerProfile(manager.runtime).save('# 主人资料\n测试主人授权的公开背景，不改变管理员。')
    async def run():
        job=await manager.submit('project','检查当前项目',owner='verified_owner')
        assert (await finish(manager,job['id']))['status']=='completed'
        turn=next(p for m,p in FakeRPC.calls if m=='turn/start')
        text=json.dumps(turn['input'],ensure_ascii=False)
        assert '测试主人授权的公开背景' in text
        assert turn['input'][-1]['text']=='检查当前项目'
        assert manager.store.job(job['id'])['owner']=='verified_owner'
        await manager.close()
    asyncio.run(run())


def test_agent_receives_frozen_recent_group_context_and_rejects_wrong_source(manager):
    snapshot={'group_id':'a@chatroom','group_name':'来源群','requester_id':'owner','messages':[{'text':'刚才的具体问题'}],
              'message_count':1,'group_memory_markdown':'','has_group_memory':False,'chat_style':'family','persona':'亲切简洁'}
    async def run():
        with pytest.raises(ValueError):
            await manager.submit('project','任务',owner='owner',source='b@chatroom',source_context=snapshot)
        assert not manager.store.jobs()
        job=await manager.submit('project','解释这是什么意思',owner='owner',source='a@chatroom',source_context=snapshot)
        snapshot['messages'][0]['text']='提交之后的新内容'
        assert (await finish(manager,job['id']))['status']=='completed'
        turn=next(p for m,p in FakeRPC.calls if m=='turn/start')
        text=json.dumps(turn['input'],ensure_ascii=False)
        assert '刚才的具体问题' in text and '提交之后的新内容' not in text
        assert 'source_group_context' in text and '来源群' in text
        assert turn['input'][-1]['text']=='解释这是什么意思'
        await manager.close()
    asyncio.run(run())


def test_job_executes_in_configured_workspace_and_resumes_only_same_context(manager):
    async def run():
        first=await manager.submit('测试项目','第一项',owner='owner',source='group_a')
        assert (await finish(manager,first['id']))['status']=='completed'
        second=await manager.submit('project','第二项',owner='owner',source='group_a')
        await finish(manager,second['id'])
        assert any(m=='thread/resume' for m,p in FakeRPC.calls)
        third=await manager.submit('project','独立来源',owner='owner',source='group_b')
        await finish(manager,third['id'])
        assert manager.store.job(first['id'])['session_key']!=manager.store.job(third['id'])['session_key']
        turn=next(p for m,p in FakeRPC.calls if m=='turn/start')
        assert turn['cwd']==manager.config.value.workspaces[0].path
        assert turn['sandboxPolicy']['writableRoots']==[turn['cwd']]
        await manager.close()
    asyncio.run(run())


def test_workspace_path_change_creates_new_session_and_cannot_resume_old_task(manager,tmp_path):
    async def run():
        first=await manager.submit('project','任务');await finish(manager,first['id'])
        path=tmp_path/'another';path.mkdir()
        manager.config.value.workspaces[0].path=str(path.resolve())
        second=await manager.submit('project','新目录任务');await finish(manager,second['id'])
        assert first['session_key']!=second['session_key']
        with pytest.raises(ValueError):await manager.submit('project','不应续接',parent=first['id'])
        await manager.close()
    asyncio.run(run())


def test_nonconfigured_directory_and_job_id_reuse_are_rejected(manager,tmp_path):
    async def run():
        with pytest.raises(ValueError):await manager.submit(str(tmp_path),'任务')
        first=await manager.submit('project','任务',owner='one',identifier='fixed');await finish(manager,first['id'])
        with pytest.raises(ValueError):await manager.submit('project','任务',owner='other',identifier='fixed')
        await manager.close()
    asyncio.run(run())


def test_approval_is_visible_and_must_be_resolved_before_continuing(manager):
    FakeRPC.mode='approval'
    async def run():
        job=await manager.submit('project','运行检查')
        while not manager.store.approvals():await asyncio.sleep(0)
        a=manager.store.approvals()[0]
        assert manager.store.job(job['id'])['status']=='awaiting_approval'
        assert a['details']['command']=='python test.py'
        await manager.decide(a['id'],True)
        assert (await finish(manager,job['id']))['status']=='completed'
        await manager.close()
    asyncio.run(run())


def test_read_only_workspace_does_not_accept_elevated_write_or_command_approval(manager):
    FakeRPC.mode='approval';manager.config.value.workspaces[0].access='read-only'
    async def run():
        job=await manager.submit('project','只读任务')
        assert (await finish(manager,job['id']))['status']=='failed'
        assert not manager.store.approvals()
        await manager.close()
    asyncio.run(run())


def test_cancel_interrupts_native_turn_and_closes_owned_process(manager):
    FakeRPC.mode='wait'
    async def run():
        FakeRPC.release=asyncio.Event()
        job=await manager.submit('project','耗时任务')
        while not manager.store.job(job['id'])['turn_id']:await asyncio.sleep(0)
        await manager.cancel(job['id'])
        assert manager.store.job(job['id'])['status']=='cancelled'
        assert any(m=='turn/interrupt' for m,p in FakeRPC.calls)
        assert FakeRPC.closed>=1
        await manager.close()
    asyncio.run(run())


def test_same_workspace_tasks_do_not_execute_concurrently(manager):
    FakeRPC.mode='wait'
    async def run():
        FakeRPC.release=asyncio.Event()
        a=await manager.submit('project','一');b=await manager.submit('project','二')
        while not manager.store.job(a['id'])['turn_id']:await asyncio.sleep(0)
        assert manager.store.job(b['id'])['status']=='queued'
        FakeRPC.release.set()
        await finish(manager,a['id']);await finish(manager,b['id'])
        assert FakeRPC.peak==1
        await manager.close()
    asyncio.run(run())


def test_nested_workspace_paths_are_not_modified_concurrently(manager):
    from violet.settings import Workspace
    child=Path(manager.config.value.workspaces[0].path)/'nested';child.mkdir()
    manager.config.value.workspaces.append(Workspace(id='nested',name='嵌套项目',path=str(child)))
    FakeRPC.mode='wait'
    async def run():
        FakeRPC.release=asyncio.Event()
        a=await manager.submit('project','一');b=await manager.submit('nested','二')
        while not manager.store.job(a['id'])['turn_id']:await asyncio.sleep(0)
        assert manager.store.job(b['id'])['status']=='queued'
        FakeRPC.release.set();await finish(manager,a['id']);await finish(manager,b['id'])
        assert FakeRPC.peak==1
        await manager.close()
    asyncio.run(run())


def test_agent_host_send_uses_queue_once_and_respects_stop(manager):
    from violet.settings import Group
    manager.config.value.groups=[Group(id='a@chatroom',name='甲群')]
    engine=Engine(manager.config,manager.store,manager.runtime)
    class Bridge:
        def resolve(self,query):return {'username':'wxid_target'}
        def positions(self,group):return {}
    engine.bridge=Bridge();engine.admin_ids={'owner'}
    async def connect():pass
    engine.connect=connect
    manager.store.create_job('job1','project','owner','a@chatroom','发信','session')
    manager.store.update_job('job1',status='running')
    job=manager.store.job('job1')
    async def run():
        with pytest.raises(ValueError):await engine.agent_send_message(job,'联系人','正文','call')
        engine.intent_running=True
        async def consume():
            while not manager.store.outbox():await asyncio.sleep(0)
            row=manager.store.outbox()[0]
            manager.store.update_outgoing(row['id'],'sent',12345)
        consumer=asyncio.create_task(consume())
        first=await engine.agent_send_message(job,'联系人','正文','call')
        await consumer
        second=await engine.agent_send_message(job,'联系人','正文','call')
        assert first==second and '12345' in first
        assert manager.store.overview()['counts']['sent']==1
        await manager.close()
    asyncio.run(run())


def test_cancelled_agent_cannot_submit_a_queued_send(manager):
    from violet.settings import Group
    manager.config.value.groups=[Group(id='a@chatroom',name='甲群')]
    engine=Engine(manager.config,manager.store,manager.runtime);engine.intent_running=True
    manager.store.create_job('job1','project','owner','a@chatroom','发信','session')
    manager.store.update_job('job1',status='running')
    manager.store.reserve_direct('agent_send_job1_call','target','a@chatroom','正文',{})
    delivery=manager.store.outbox()[0]
    assert engine.delivery_allowed(delivery)
    manager.store.update_job('job1',status='cancelled')
    asyncio.run(engine.agent_cancelled('job1'))
    assert not engine.delivery_allowed(delivery)
    assert manager.store.delivery(delivery['id'])['status']=='cancelled'


def test_agent_send_goes_through_host_tool_with_original_owner(manager):
    FakeRPC.mode='send';calls=[]
    async def send(job,recipient,text,call):calls.append((job['owner'],recipient,text,call));return '服务器编号已确认'
    manager.send_tool=send
    async def run():
        job=await manager.submit('project','发信',owner='wxid_owner',source='a@chatroom')
        assert (await finish(manager,job['id']))['result']=='True'
        assert any(t['name']=='violet_send_message' for t in FakeRPC.tools)
        await manager.close()
    asyncio.run(run())
    assert calls==[('wxid_owner','联系人','管理员授权正文','unique_call')]


def test_disallowed_agent_send_is_not_advertised_or_executed(manager):
    FakeRPC.mode='send';manager.config.value.workspaces[0].allow_send_messages=False;calls=[]
    async def send(*args):calls.append(args)
    manager.send_tool=send
    async def run():
        job=await manager.submit('project','发信')
        assert (await finish(manager,job['id']))['result']=='False'
        assert not any(t['name']=='violet_send_message' for t in FakeRPC.tools)
        await manager.close()
    asyncio.run(run())
    assert not calls


def test_non_admin_group_member_cannot_create_agent_job(manager):
    engine=Engine(manager.config,manager.store,manager.runtime);engine.agent_manager=manager;engine.admin_ids={'wxid_owner'}
    result=asyncio.run(engine.admin({'sender':'outsider','is_self':False,'type':'文本','text':'/agent project 修改文件','group':'a@chatroom'}))
    assert result is None and not manager.store.jobs()


def test_global_stop_cancels_agents_even_without_group_monitoring(manager):
    FakeRPC.mode='wait'
    engine=Engine(manager.config,manager.store,manager.runtime);engine.agent_manager=manager
    async def run():
        FakeRPC.release=asyncio.Event()
        job=await manager.submit('project','任务')
        while not manager.store.job(job['id'])['turn_id']:await asyncio.sleep(0)
        await engine.stop()
        assert manager.store.job(job['id'])['status']=='cancelled'
        assert not engine.running and (manager.runtime/'AGENT_STOP').exists()
        await manager.close()
    asyncio.run(run())


def test_agent_api_ignores_no_forged_owner_and_requires_local_token(tmp_path,monkeypatch):
    import violet.agent as module
    monkeypatch.setattr(module,'codex_binary',lambda configured='':'fake')
    project=tmp_path/'project';project.mkdir()
    config=ConfigStore(tmp_path/'config.json');config.update({'workspaces':[{'id':'project','name':'项目','path':str(project)}]})
    app=create_app(config.path,tmp_path/'runtime');app.state.agents.rpc_factory=FakeRPC;FakeRPC.mode='normal'
    with TestClient(app) as client:
        body={'workspace':'project','prompt':'test'}
        assert client.post('/api/agent/jobs',json=body).status_code==403
        token=client.get('/api/session').json()['token'];headers={'x-violet-local':token}
        assert client.post('/api/agent/jobs',headers=headers,json={**body,'owner':'wxid_owner'}).status_code==422
        result=client.post('/api/agent/jobs',headers=headers,json=body)
        assert result.status_code==200 and result.json()['owner']=='@panel'
