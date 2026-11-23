import asyncio
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from violet.agent import AgentManager
from violet.settings import Settings
from violet.web import create_app
from test_commands_and_source import setup,message
from test_agents import manager,finish


def grant(engine,root):
    root.mkdir(exist_ok=True)
    engine.config.update({'workspaces':[{'id':'project','name':'小组作业','path':str(root),
                                        'project_groups':['a@chatroom']}],
                          'agent':{'deepseek_enabled':True}})


def test_defaults_never_open_existing_workspaces():
    settings=Settings.model_validate({'workspaces':[{'id':'p','name':'P','path':'D:/p'}]})
    assert not settings.agent.deepseek_enabled
    assert settings.workspaces[0].project_groups==[]
    assert settings.workspaces[0].project_all_members
    assert not settings.project_access(settings.workspaces[0],'friend','a@chatroom')


def test_project_binding_validation_and_member_restriction(setup,tmp_path):
    engine,_=setup;grant(engine,tmp_path/'project');s=engine.config.value;ws=s.workspaces[0]
    assert s.project_access(ws,'friend','a@chatroom')
    assert not s.project_access(ws,'friend','b@chatroom')
    assert not s.project_access(ws,'friend','wxid_private')
    ws.project_all_members=False;ws.project_members=['wxid_classmate']
    assert not s.project_access(ws,'friend','a@chatroom')
    assert s.project_access(ws,'wxid_classmate','a@chatroom')
    with pytest.raises(ValueError):engine.config.update({'workspaces':[dict(ws.model_dump(),project_groups=['unknown@chatroom'])]})
    with pytest.raises(ValueError):engine.config.update({'workspaces':[dict(ws.model_dump(),project_members=['假的管理员'])]})


def test_group_member_can_submit_native_deepseek_without_admin_powers(setup,tmp_path):
    engine,store=setup;grant(engine,tmp_path/'project');calls=[]
    class Manager:
        async def submit(self,*args,**kwargs):calls.append((args,kwargs));return {'id':'native123'}
    engine.agent_manager=Manager()
    reply=asyncio.run(engine.admin(message(text='/deepseek project 修复项目并运行测试',sender='friend')))
    assert 'DeepSeek' in reply and calls[0][1]['backend']=='deepseek'
    assert calls[0][1]['owner']=='friend' and calls[0][1]['source_context']['group_id']=='a@chatroom'
    assert '只有已核验' in asyncio.run(engine.agent_command(message(sender='friend'),'/agent project x'))
    assert asyncio.run(engine.admin(message(text='/command send Bob | x',sender='friend'))) is None
    assert not store.outbox()
    assert asyncio.run(engine.admin(message(text='/deepseek project x',sender='friend',is_self=True))) is None
    assert '尚未获得' in asyncio.run(engine.admin(message(text='/deepseek project x',sender='friend',group='b@chatroom')))


def test_generate_routes_member_commands_before_model_and_mention_filter(setup,tmp_path):
    engine,_=setup;grant(engine,tmp_path/'project');group=engine.config.value.groups[0];group.mode='mention'
    engine.agent_manager=object()
    # Help is deterministic, even for a member in mention-only mode.
    async def run():return await engine.generate(group,[message(text='/deepseek 帮助',sender='friend')])
    result=asyncio.run(run())
    assert '/deepseek 工作区' in ''.join(result)


def test_status_cancel_and_results_are_scoped_to_owner_and_source(setup,tmp_path):
    engine,store=setup;grant(engine,tmp_path/'project');cancelled=[]
    class Manager:
        async def cancel(self,identifier):cancelled.append(identifier)
    engine.agent_manager=Manager()
    ws=engine.config.value.workspaces[0]
    store.create_job('native123','project','friend','a@chatroom','task','session',ws.model_dump(),'deepseek')
    store.update_job('native123',status='completed',result='真实结果')
    assert '真实结果' in asyncio.run(engine.admin(message(text='/deepseek 状态 native123',sender='friend')))
    assert '没有找到' in asyncio.run(engine.admin(message(text='/deepseek 状态 native123',sender='other')))
    asyncio.run(engine.admin(message(text='/deepseek 取消 native123',sender='other')))
    assert not cancelled
    asyncio.run(engine.agent_finished(store.job('native123')))
    delivery=store.outbox()[0]
    assert delivery['group_id']=='a@chatroom' and 'DeepSeek 任务' in delivery['body']
    assert engine.delivery_allowed(delivery)
    engine.config.value.workspaces[0].project_groups=[]
    assert not engine.delivery_allowed(delivery)


class FakeHarness:
    calls=[];closed=0
    def __init__(self,command,cwd,event,request,home,patch,policy,api_key):
        self.event,self.request=event,request
        type(self).calls.append(('policy',policy))
        patch_data=json.loads(patch.read_text(encoding='utf-8'))
        assert next(p for p in patch_data if p.get('id')=='acp')['inject']==['acpAppStartup','violetProjectPolicy']
    async def start(self):return {}
    async def call(self,method,params,timeout=60):
        type(self).calls.append((method,params))
        if method=='session/new':return {'sessionId':'native-session'}
        if method=='session/resume':return {} # ACP resume does not repeat sessionId.
        if method=='session/prompt':
            decision=await self.request('session/request_permission',{})
            assert decision['outcome']['outcome']=='cancelled'
            for kind,text in [('agent_thought_chunk','不应持久化的推理'),('agent_message_chunk','已修复并验证')]:
                await self.event('session/update',{'sessionId':params['sessionId'],'update':{
                    'sessionUpdate':kind,'content':{'type':'text','text':text}}})
            return {'stopReason':'end_turn'}
    async def close(self):type(self).closed+=1


def test_native_backend_reuses_queue_and_separates_sessions_and_thoughts(manager,monkeypatch):
    import violet.deepseek_agent as module
    monkeypatch.setattr(module,'HarnessRPC',FakeHarness)
    monkeypatch.setattr(module,'harness_command',lambda path:['fake-dsh'])
    manager.config.update({'api_key':'test-key','agent':{'deepseek_enabled':True}})
    FakeHarness.calls=[];FakeHarness.closed=0
    async def run():
        first=await manager.submit('project','修复并测试',backend='deepseek')
        result=await finish(manager,first['id'])
        assert result['status']=='completed' and result['backend']=='deepseek'
        assert result['result']=='已修复并验证'
        second=await manager.submit('project','继续检查',backend='deepseek')
        await finish(manager,second['id'])
        assert first['session_key']==second['session_key']
        codex=await manager.submit('project','分析')
        await finish(manager,codex['id'])
        assert codex['session_key']!=first['session_key']
        with pytest.raises(ValueError):await manager.submit('project','续接',parent=first['id'])
    asyncio.run(run())
    assert any(method=='session/resume' for method,_ in FakeHarness.calls)
    assert FakeHarness.closed==2
    assert '不应持久化的推理' not in json.dumps(manager.store.job_events(manager.store.jobs()[-1]['id']),ensure_ascii=False)


def test_submit_backend_checks_acl_again_and_does_not_fall_back_to_codex(manager,monkeypatch):
    import violet.deepseek_agent as module
    monkeypatch.setattr(module,'harness_command',lambda path:['fake-dsh'])
    manager.config.update({'api_key':'test-key','agent':{'deepseek_enabled':True},
                          'groups':[{'id':'a@chatroom','name':'同学群'}]})
    async def run():
        with pytest.raises(ValueError,match='项目经理'):await manager.submit('project','x',owner='friend',source='a@chatroom',backend='deepseek')
        with pytest.raises(ValueError,match='API Key'):await manager.submit('project','test-key',backend='deepseek')
        manager.config.value.agent.deepseek_enabled=False
        with pytest.raises(ValueError,match='尚未启用'):await manager.submit('project','x',backend='deepseek')
    asyncio.run(run());assert not manager.store.jobs()


def test_local_api_round_trips_binding_and_rejects_forged_owner(tmp_path):
    from violet.settings import ConfigStore
    config=ConfigStore(tmp_path/'config.json');app=create_app(config.path,tmp_path/'runtime')
    with TestClient(app) as client:
        headers={'x-violet-local':client.get('/api/session').json()['token']}
        payload={'groups':[{'id':'a@chatroom','name':'同学群'}],
                 'workspaces':[{'id':'project','name':'小组作业','path':str(tmp_path),'project_groups':['a@chatroom']}],
                 'agent':{'deepseek_enabled':False,'deepseek_model':'deepseek-flash'}}
        assert client.put('/api/settings',headers=headers,json=payload).status_code==200
        current=client.get('/api/settings').json()
        assert current['workspaces'][0]['project_all_members']
        assert current['workspaces'][0]['project_groups']==['a@chatroom']
        body={'workspace':'project','prompt':'x','backend':'deepseek'}
        assert client.post('/api/agent/jobs',json=body).status_code==403
        assert client.post('/api/agent/jobs',headers=headers,json={**body,'owner':'friend'}).status_code==422
        response=client.post('/api/agent/jobs',headers=headers,json=body)
        assert response.status_code==400 and '尚未启用' in response.text
