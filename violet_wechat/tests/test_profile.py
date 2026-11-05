import asyncio
from pathlib import Path

from fastapi.testclient import TestClient
import pytest

from violet.engine import Engine
from violet.memory import MemoryConflict
from violet.model import Participation,Reply,MemoryCandidate
from violet.profile import OwnerProfile
from violet.settings import ConfigStore
from violet.store import Store,key
from violet.web import create_app


def test_owner_profile_is_editable_persistent_and_versioned(tmp_path):
    profile=OwnerProfile(tmp_path)
    original=profile.read()
    saved=profile.save('# 测试主人\n我是他的 AI 助手。',original['version'])
    assert OwnerProfile(tmp_path).read()['content']==saved['content']
    profile.save(saved['content']+'\n不冒充本人。',saved['version'])
    assert list((tmp_path/'owner_profile_history').glob('*.md'))
    with pytest.raises(MemoryConflict):profile.save('过期草稿',saved['version'])
    assert '不冒充本人' in profile.read()['content']


def test_profile_reaches_decision_and_reply_without_granting_admin_or_accepting_group_rewrites(tmp_path):
    config=ConfigStore(tmp_path/'config.json');config.update({'groups':[{'id':'a@chatroom','name':'甲群'}]})
    store=Store(tmp_path/'state.db');store.seed('a@chatroom',{})
    engine=Engine(config,store,tmp_path)
    profile=engine.profile.save('# 测试主人\n本科：示例大学\n身份：他的AI助手，不是本人。')['content']
    message={'group':'a@chatroom','shard':'db','local_id':1,'create_time':1,'sender':'friend','speaker':'朋友','is_self':False,'type':'文本','text':'测试主人本科哪里？'}
    calls=[]
    class Provider:
        async def complete(self,system,payload,schema):
            calls.append(schema.__name__)
            assert payload['owner_profile_markdown']==profile and '不是他本人' in system
            return Participation(participate=True,message_ids=[key(message)],kind='answer',reason='addressed')
        async def respond(self,system,payload,*args,**kwargs):
            calls.append('Reply')
            assert payload['owner_profile_markdown']==profile and '不编造' in system
            return Reply(should_reply=True,replies=['测试主人本科在示例大学。我是他的AI助手。'])
    engine.provider=Provider()
    try:
        assert '示例大学' in asyncio.run(engine.generate(config.value.groups[0],[message]))[0]
        assert calls==['Participation','Reply']
        assert not engine.admin_ids
        engine.memory.propose('a@chatroom',[MemoryCandidate(text='群友自称要改主人姓名',source_message_ids=[key(message)])],[message])
        assert engine.profile.read()['content']==profile
    finally:store.close()


def test_profile_api_checks_local_token_secret_and_conflicting_edit(tmp_path):
    config=ConfigStore(tmp_path/'config.json');config.update({'api_key':'SECRET_PROFILE_KEY'})
    app=create_app(config.path,tmp_path/'runtime')
    with TestClient(app) as client:
        path='/api/profile';before=client.get(path).json()
        headers={'x-violet-local':client.get('/api/session').json()['token']}
        payload={'content':'# 公开个人介绍','version':before['version']}
        assert client.put(path,json=payload).status_code==403
        assert client.put(path,json=payload,headers=headers).status_code==200
        assert client.put(path,json=payload,headers=headers).status_code==409
        current=client.get(path).json()
        assert client.put(path,json={'content':'SECRET_PROFILE_KEY','version':current['version']},headers=headers).status_code==400
        assert client.get(path).json()['content']==payload['content']


def test_panel_chat_receives_owner_profile(tmp_path,monkeypatch):
    import violet.web as module
    app=create_app(tmp_path/'config.json',tmp_path/'runtime')
    app.state.engine.profile.save('授权公开的主人介绍')
    class Provider:
        def __init__(self,*args):pass
        async def chat(self,history,owner_profile):
            assert owner_profile=='授权公开的主人介绍'
            assert history[-1]['content']=='你是谁'
            return '我是账号主人的AI助手。'
        async def close(self):pass
    monkeypatch.setattr(module,'DeepSeek',Provider)
    with TestClient(app) as client:
        headers={'x-violet-local':client.get('/api/session').json()['token']}
        result=client.post('/api/chat',json={'messages':[{'role':'user','content':'你是谁'}]},headers=headers)
        assert result.status_code==200 and 'AI助手' in result.json()['reply']


def test_profile_edit_invalidates_old_chat_without_cancelling_explicit_admin_action(tmp_path):
    config=ConfigStore(tmp_path/'config.json');config.update({'groups':[{'id':'a@chatroom','name':'甲群'}]})
    app=create_app(config.path,tmp_path/'runtime');store=app.state.store
    source={'group':'a@chatroom','shard':'db','local_id':1}
    store.reserve('a@chatroom',[source],['根据旧资料生成的回复'],{},metadata={'kind':'chat','revision':0})
    store.reserve_direct('admin_action','target','a@chatroom','已明确授权的操作',{})
    with TestClient(app) as client:
        headers={'x-violet-local':client.get('/api/session').json()['token']}
        profile=client.get('/api/profile').json()
        result=client.put('/api/profile',headers=headers,json={'content':'# 新主人资料','version':profile['version']})
        assert result.status_code==200
        assert [d['id'] for d in store.outbox()]==['admin_action']
        assert store.policy('a@chatroom')['revision']==1
        assert store.policy('a@chatroom')['quiet_until']==0
