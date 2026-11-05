import asyncio
import json
from types import SimpleNamespace

from fastapi.testclient import TestClient
import httpx
from pydantic import SecretStr,ValidationError
import pytest

from violet.bridge import resolve_contact
from violet.engine import Engine,parts,is_command
from violet.model import DeepSeek,DeepSeekError,Reply,Participation
from violet.settings import ConfigStore,Settings,Group
from violet.store import Store
from violet.web import create_app


def message(group='a@chatroom',identifier=1,own=False):
    return {'group':group,'shard':'message0','local_id':identifier,'is_self':own,'create_time':identifier,
            'sender':'wxid_other','speaker':'成员','type':'文本','text':f'{group} message {identifier}'}


def test_config_masks_and_preserves_key(tmp_path):
    c=ConfigStore(tmp_path/'config.json')
    c.update({'api_key':'secret-key','poll_seconds':7})
    assert 'api_key' not in c.value.public()
    assert c.value.public()['api_key_configured']
    c.update({'api_key':'','poll_seconds':12})
    assert c.value.api_key.get_secret_value()=='secret-key'
    assert ConfigStore(c.path).value.poll_seconds==12


def test_config_bounds():
    for value in [{'poll_seconds':0},{'max_replies':8},{'unknown':'ignored'}]:
        with pytest.raises(ValidationError):Settings(**value)


def test_admin_identity_cannot_be_granted_by_nickname():
    rows=[{'username':'wxid_abc','alias':'OWNER','nick_name':'ExampleOwner','remark':''}]
    assert resolve_contact(rows,'owner',admin=True)['username']=='wxid_abc'
    with pytest.raises(ValueError):resolve_contact(rows,'ExampleOwner',admin=True)


def test_group_name_normalization_does_not_hide_collisions():
    rows=[{'username':'a@chatroom','alias':'','nick_name':'👌 示例群（测试）','remark':''}]
    assert resolve_contact(rows,'示例群(测试)')['username']=='a@chatroom'
    rows.append({'username':'b@chatroom','alias':'','nick_name':'👍 示例群（测试）','remark':''})
    with pytest.raises(ValueError):resolve_contact(rows,'示例群(测试)')


def test_queue_is_per_group_and_self_does_not_loop(tmp_path):
    s=Store(tmp_path/'state.db')
    for g in ['a@chatroom','b@chatroom']:
        s.seed(g,{})
        s.poll(g,[message(g,1),message(g,2,True)])
        s.poll(g,[message(g,1)])
        assert len(s.batch(g,10))==1
        assert s.groups()[g]['cursors']=={'message0':2}
    s.close()


def test_all_reply_parts_survive_restart_and_preserve_new_arrivals(tmp_path):
    path=tmp_path/'state.db'
    s=Store(path);g='a@chatroom';s.seed(g,{})
    s.poll(g,[message(g,1)])
    batch=s.batch(g,10)
    s.poll(g,[message(g,2)])
    s.reserve(g,batch,['第一条','第二条','第三条'],{'message0':2})
    first=s.outbox()[0]
    s.update_outgoing(first['id'],'sending')
    s.update_outgoing(first['id'],'sent',111)
    s.close()
    s=Store(path)
    assert [r['body'] for r in s.outbox()]==['第二条','第三条']
    assert s.batch(g,10)[0]['local_id']==2
    assert s.overview()['counts']['sent']==1
    s.close()


def test_same_text_and_local_id_in_two_groups_has_distinct_id(tmp_path):
    s=Store(tmp_path/'state.db')
    for g in ['a@chatroom','b@chatroom']:
        s.seed(g,{})
        m=message(g)
        s.poll(g,[m]);s.reserve(g,[m],['你好'],{})
    assert len({r['id'] for r in s.outbox()})==2
    s.close()


def test_claimed_admin_action_cannot_repeat(tmp_path):
    s=Store(tmp_path/'state.db')
    assert s.claim_action('id',{'send':'test'})
    assert not s.claim_action('id',{'send':'test'})
    assert s.action('id')['status']=='running'
    s.finish_action('id','已发送')
    assert s.action('id')['result']=='已发送'
    s.close()


def test_utf8_parts_and_reply_validation():
    result=parts('😂'*1600)
    assert ''.join(result)=='😂'*1600
    assert all(len(text.encode('utf-8'))<=1800 for text in result)
    with pytest.raises(ValidationError):Reply(should_reply=True,replies=['重复','重复'])
    with pytest.raises(ValidationError):Reply(should_reply=True,replies=[])


def test_web_model_does_not_accept_unsearched_claim():
    async def run():
        p=DeepSeek(Settings(api_key=SecretStr('key')),transport=httpx.MockTransport(lambda r:httpx.Response(200,json={'output':[{'type':'message','content':[{'type':'output_text','text':'编造搜索'}]}]})))
        try:
            with pytest.raises(DeepSeekError):await p.search('test')
        finally:await p.close()
    asyncio.run(run())


def test_provider_never_exposes_upstream_key_in_errors():
    async def run():
        p=DeepSeek(Settings(api_key=SecretStr('VERY_SECRET')),transport=httpx.MockTransport(lambda r:httpx.Response(401,text='VERY_SECRET bad credential')))
        try:
            with pytest.raises(DeepSeekError) as error:await p.chat([{'role':'user','content':'hi'}])
            assert 'VERY_SECRET' not in str(error.value)
        finally:await p.close()
    asyncio.run(run())


def test_json_call_uses_direct_api_and_parses_multiple_replies():
    calls=[]
    def transport(request):
        calls.append(request)
        assert request.url.host=='api.deepseek.com'
        body=json.loads(request.content)
        assert body['response_format']['type']=='json_object'
        assert body['thinking']['type']=='disabled'
        return httpx.Response(200,json={'choices':[{'message':{'content':'{"should_reply":true,"replies":["第一题","第二题"]}'}}],'usage':{'total_tokens':25}})
    async def run():
        p=DeepSeek(Settings(api_key=SecretStr('key')),transport=httpx.MockTransport(transport))
        try:
            result=await p.complete('JSON',{'message':'test'},Reply)
            assert result.replies==['第一题','第二题']
            assert p.usage=={'requests':1,'tokens':25}
        finally:await p.close()
    asyncio.run(run())
    assert len(calls)==1


def test_ordinary_member_cannot_trigger_admin_send(tmp_path):
    c=ConfigStore(tmp_path/'config.json');s=Store(tmp_path/'state.db')
    engine=Engine(c,s,tmp_path);engine.admin_ids={'wxid_owner'}
    assert asyncio.run(engine.admin({**message(),'text':'发消息 某人 | 你好','speaker':'管理员'})) is None
    s.close()


def test_group_payload_never_contains_other_group_history(tmp_path):
    c=ConfigStore(tmp_path/'config.json');c.update({'max_replies':3})
    s=Store(tmp_path/'state.db');g=Group(id='a@chatroom',name='甲群');s.seed(g.id,{})
    engine=Engine(c,s,tmp_path)
    engine.context={g.id:[message(g.id)],'b@chatroom':[message('b@chatroom')]}
    class Provider:
        async def complete(self,system,payload,schema):
            assert schema is Participation
            return Participation(participate=True,message_ids=[m['message_id'] for m in payload['new_messages']],kind='answer',reason='addressed')
        async def respond(self,system,payload,schema,allow_search=True):
            assert payload['group']=='甲群'
            assert all('b@chatroom' not in m['text'] for m in payload['recent_context'])
            return Reply(should_reply=True,replies=['一','二','三'])
    engine.provider=Provider()
    assert asyncio.run(engine.generate(g,[message(g.id)]))==['一','二','三']
    s.close()


def test_uncertain_delivery_is_not_automatically_repeated(tmp_path):
    c=ConfigStore(tmp_path/'config.json');s=Store(tmp_path/'state.db')
    s.reserve_direct('unknown','a@chatroom','a@chatroom','你好',{})
    s.update_outgoing('unknown','sending');s.update_outgoing('unknown','uncertain')
    assert not s.outbox()
    assert len(s.outbox(('uncertain',)))==1
    s.close()


def test_local_panel_blocks_cross_site_writes_and_masks_secret(tmp_path):
    config=ConfigStore(tmp_path/'config.json');config.update({'api_key':'SECRET'})
    app=create_app(config.path,tmp_path/'runtime')
    with TestClient(app) as client:
        assert client.get('/').status_code==200
        assert 'SECRET' not in client.get('/api/settings').text
        assert client.post('/api/monitor/stop').status_code==403
        token=client.get('/api/session').json()['token']
        headers={'x-violet-local':token}
        assert client.post('/api/monitor/stop',headers=headers).status_code==200
        assert client.post('/api/monitor/stop',headers={**headers,'Origin':'https://other.example'}).status_code==403
        assert client.get('/api/status',headers={'Host':'evil.example'}).status_code==400


def test_invalid_key_payload_is_not_echoed_back(tmp_path):
    app=create_app(tmp_path/'config.json',tmp_path/'runtime')
    with TestClient(app) as client:
        token=client.get('/api/session').json()['token']
        result=client.put('/api/settings',headers={'x-violet-local':token},json={'api_key':'VERY_SECRET','max_replies':200})
        assert result.status_code==400
        assert 'VERY_SECRET' not in result.text


def test_no_quoted_operation_trigger():
    assert not is_command('他说给某人发送消息')
    assert is_command('在群聊示例群(测试)发送自我介绍')


def test_monitor_delivers_all_parts_once_and_resumes_without_replay(tmp_path,monkeypatch):
    import violet.engine as module
    config=ConfigStore(tmp_path/'config.json')
    config.update({'api_key':'fake','groups':[{'id':'a@chatroom','name':'甲群'}],'reply_gap_seconds':0.5,'poll_seconds':2})
    store=Store(tmp_path/'state.db')
    sent=[]
    class Native:
        def send(self,text,identifier,target,allowed=None):sent.append((text,identifier,target))
        def alive(self):return True
        def close(self):pass
    class Bridge:
        account='test';self_id='wxid_self'
        contacts=[{'username':'a@chatroom','alias':'','nick_name':'甲群','remark':''}]
        def __init__(self,*args):pass
        def refresh(self):pass
        def resolve(self,value,admin=False):return self.contacts[0]
        def sender(self):return Native()
        def positions(self,group):return {}
        def snapshot(self,group,cursors,context):
            rows=[message(group,i) for i in range(1,4)]
            return [m for m in rows if m['local_id']>cursors.get('message0',0)],rows
        def verify(self,delivery):return {'server_id':100+len(sent)}
    class Provider:
        async def complete(self,system,payload,schema):
            assert schema is Participation
            return Participation(participate=True,message_ids=[m['message_id'] for m in payload['new_messages']],kind='answer',reason='addressed')
        usage={'requests':0,'tokens':0}
        def __init__(self,*args):pass
        async def close(self):pass
        async def respond(self,*args,**kwargs):return Reply(should_reply=True,replies=['一','二','三'])
    monkeypatch.setattr(module,'DeepSeek',Provider)
    async def run():
        engine=Engine(config,store,tmp_path,bridge_factory=Bridge)
        await engine.start()
        async def wait():
            while store.overview()['counts'].get('sent',0)<3:
                await asyncio.sleep(0.02)
        await asyncio.wait_for(wait(),timeout=4)
        await engine.stop()
        await engine.start()
        await asyncio.sleep(0.1)
        await engine.stop()
    asyncio.run(run())
    assert [r[0] for r in sent]==['一','二','三']
    assert len({r[1] for r in sent})==3
    assert all(r[2]=='a@chatroom' for r in sent)
    store.close()
