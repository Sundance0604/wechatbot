import asyncio
import json
import threading

from fastapi.testclient import TestClient
import httpx
import pytest

from violet.engine import Engine
from violet.errors import SendCancelled
from violet.model import DeepSeek,DeepSeekError,Reply,AdminPlan,AdminAction,Participation
from violet.settings import ConfigStore,Settings
from violet.store import Store
from violet.web import create_app


def setup_engine(tmp_path):
    config=ConfigStore(tmp_path/'config.json')
    config.update({'api_key':'fake','groups':[{'id':'a@chatroom','name':'甲群'},{'id':'b@chatroom','name':'乙群'}]})
    store=Store(tmp_path/'state.db')
    engine=Engine(config,store,tmp_path)
    engine.intent_running=True
    for group in config.value.groups:store.seed(group.id,{})
    return engine,store


def test_stop_cancels_all_unsent_parts_and_latches(tmp_path):
    engine,store=setup_engine(tmp_path)
    store.reserve_direct('1','a@chatroom','a@chatroom','第一条',{})
    store.reserve_direct('2','a@chatroom','a@chatroom','第二条',{})
    asyncio.run(engine.stop())
    assert (tmp_path/'STOP').exists()
    assert not store.outbox()
    assert store.overview()['counts']['cancelled']==2
    assert not engine.intent_running
    assert any(e['kind']=='stop_requested' for e in store.overview()['events'])
    store.close()


def test_stop_during_database_read_never_reaches_native_sender(tmp_path):
    engine,store=setup_engine(tmp_path)
    entered,release=threading.Event(),threading.Event()
    sends=[]
    class Bridge:
        def positions(self,group):
            entered.set();release.wait(3);return {}
    class Native:
        def send(self,*args):sends.append(args)
    engine.bridge,engine.sender=Bridge(),Native()
    store.reserve_direct('1','a@chatroom','a@chatroom','不会发出',{})
    async def run():
        delivery=asyncio.create_task(engine.deliver(store.outbox()[0]))
        await asyncio.to_thread(entered.wait,2)
        await engine.stop()
        release.set()
        await delivery
    asyncio.run(run())
    assert not sends
    assert store.overview()['counts']['cancelled']==1
    store.close()


def test_stop_cancels_a_job_armed_on_the_native_worker(tmp_path):
    engine,store=setup_engine(tmp_path)
    armed,release=threading.Event(),threading.Event()
    class Bridge:
        def positions(self,group):return {}
    class Native:
        def send(self,*args):
            armed.set();release.wait(3)
            raise SendCancelled('cancelled before invocation')
        def block(self):pass
        def cancel_pending(self):release.set()
    engine.bridge,engine.sender=Bridge(),Native()
    store.reserve_direct('1','a@chatroom','a@chatroom','不会发出',{})
    async def run():
        engine.task=asyncio.create_task(engine.deliver(store.outbox()[0]))
        assert await asyncio.to_thread(armed.wait,2)
        await engine.stop()
        assert engine.task.done()
    asyncio.run(run())
    assert store.overview()['counts']['cancelled']==1
    assert not store.outbox(('uncertain',))
    store.close()


def test_disabling_one_group_cancels_its_preflight_without_stopping_other_group(tmp_path):
    engine,store=setup_engine(tmp_path)
    entered,release=threading.Event(),threading.Event();sends=[]
    class Bridge:
        def positions(self,group):entered.set();release.wait(3);return {}
    class Native:
        def send(self,*args):sends.append(args)
    engine.bridge,engine.sender=Bridge(),Native()
    store.reserve_direct('1','a@chatroom','a@chatroom','不会发出',{})
    store.reserve_direct('2','b@chatroom','b@chatroom','保留给乙群',{})
    async def run():
        task=asyncio.create_task(engine.deliver(store.outbox()[0]))
        await asyncio.to_thread(entered.wait,2)
        disabled=asyncio.create_task(engine.enable_group('a@chatroom',False))
        await asyncio.sleep(0)
        assert not engine.config.value.groups[0].enabled
        release.set();await task;await disabled
    asyncio.run(run())
    assert not sends
    assert engine.config.value.groups[1].enabled
    assert engine.intent_running
    assert [r['id'] for r in store.outbox()]==['2']
    store.close()


def test_old_settings_reload_cannot_override_explicit_stop(tmp_path):
    engine,store=setup_engine(tmp_path)
    async def forbidden_connect():raise AssertionError('must not reconnect')
    engine.connect=forbidden_connect
    async def run():
        revision=engine.command_revision
        await engine.stop()
        assert revision!=engine.command_revision
        await engine.start(user_requested=False)
    asyncio.run(run())
    assert not engine.running and not engine.intent_running
    store.close()


def test_stop_file_is_observed_independently_of_poll_interval(tmp_path):
    engine,store=setup_engine(tmp_path)
    store.reserve_direct('1','a@chatroom','a@chatroom','不会发出',{})
    async def run():
        task=asyncio.create_task(engine.watch_stop())
        (tmp_path/'STOP').touch()
        await asyncio.wait_for(task,timeout=1)
    asyncio.run(run())
    assert not engine.intent_running and not store.outbox()
    store.close()


def test_stop_while_start_is_connecting_cannot_start_later(tmp_path):
    engine,store=setup_engine(tmp_path)
    entered,release=asyncio.Event(),asyncio.Event()
    async def connect():entered.set();await release.wait()
    class Bridge:
        def resolve(self,value):return {}
        def positions(self,group):return {}
    engine.bridge=Bridge();engine.connect=connect
    async def run():
        starting=asyncio.create_task(engine.start())
        await entered.wait()
        await engine.stop()
        release.set();await starting
    asyncio.run(run())
    assert engine.task is None and not engine.intent_running
    store.close()


def test_group_switch_is_saved_immediately_and_cancels_prepared_messages(tmp_path):
    config=ConfigStore(tmp_path/'config.json')
    config.update({'groups':[{'id':'a@chatroom','name':'甲群'}]})
    app=create_app(config.path,tmp_path/'runtime')
    app.state.store.reserve_direct('1','a@chatroom','a@chatroom','不会发出',{})
    with TestClient(app) as client:
        token=client.get('/api/session').json()['token']
        response=client.post('/api/groups/a@chatroom/enabled',json={'enabled':False},headers={'x-violet-local':token})
        assert response.status_code==200
        assert not ConfigStore(config.path).value.groups[0].enabled
        assert not app.state.store.outbox()


def test_natural_language_search_calls_real_tool_and_uses_its_result():
    calls=[];queries=[]
    def transport(request):
        body=json.loads(request.content);calls.append(body)
        if len(calls)==1:
            assert body['tools'][0]['function']['name']=='web_search'
            return httpx.Response(200,json={'choices':[{'message':{'role':'assistant','content':None,'tool_calls':[
                {'id':'call_1','type':'function','function':{'name':'web_search','arguments':'{"query":"西工大教授 冯蕴文 研究成果 官网"}'}}]}}]})
        assert body['messages'][-1]=={'role':'tool','tool_call_id':'call_1','content':'查到的公开资料 https://example.edu/faculty'}
        return httpx.Response(200,json={'choices':[{'message':{'content':'{"should_reply":true,"replies":["依据检索资料回答：https://example.edu/faculty"]}'}}]})
    async def run():
        provider=DeepSeek(Settings(api_key='fake'),transport=httpx.MockTransport(transport))
        async def search(query):queries.append(query);return '查到的公开资料 https://example.edu/faculty'
        provider.search=search
        try:
            result=await provider.respond('按真实工具结果回答',{'text':'西工大教授冯蕴文是谁，研究成果怎么样，搜索一下'},Reply)
            assert 'example.edu' in result.replies[0]
        finally:await provider.close()
    asyncio.run(run())
    assert len(queries)==1 and len(calls)==2


def test_chat_tools_cannot_execute_arbitrary_local_functions():
    def transport(request):
        return httpx.Response(200,json={'choices':[{'message':{'tool_calls':[{'id':'bad','function':{'name':'run_shell','arguments':'{"query":"delete"}'}}]}}]})
    async def run():
        p=DeepSeek(Settings(api_key='fake'),transport=httpx.MockTransport(transport))
        try:
            with pytest.raises(DeepSeekError):await p.respond('test',{},Reply)
        finally:await p.close()
    asyncio.run(run())


def test_disabled_search_is_not_advertised_or_executed():
    def transport(request):
        assert 'tools' not in json.loads(request.content)
        return httpx.Response(200,json={'choices':[{'message':{'tool_calls':[{'id':'bad','function':{'name':'web_search','arguments':'{"query":"test"}'}}]}}]})
    async def run():
        p=DeepSeek(Settings(api_key='fake'),transport=httpx.MockTransport(transport))
        try:
            with pytest.raises(DeepSeekError):await p.respond('test',{},Reply,allow_search=False)
        finally:await p.close()
    asyncio.run(run())


def test_admin_natural_search_suffix_uses_search_instead_of_claiming_no_permission(tmp_path):
    engine,store=setup_engine(tmp_path)
    engine.admin_ids={'owner'};queries=[]
    class Provider:
        async def complete(self,system,payload,schema):
            assert '搜索一下' in payload['owner_message']
            return AdminPlan(actions=[AdminAction(kind='web_search',target='教授 研究成果 官方资料')])
        async def search(self,query):
            queries.append(query)
            return '根据实际检索资料回答 https://example.edu/faculty'
    engine.provider=Provider()
    message={'group':'a@chatroom','shard':'db','local_id':1,'create_time':1,'sender':'owner','speaker':'ExampleOwner',
             'type':'文本','text':'这位教授是谁，研究成果怎么样，搜索一下','is_self':False}
    result=asyncio.run(engine.generate(engine.config.value.groups[0],[message]))
    assert queries==['教授 研究成果 官方资料']
    assert 'https://' in result[0]
    store.close()


@pytest.mark.parametrize('stop_during',['generation','multipart'])
def test_supervised_monitor_stops_without_late_or_remaining_parts(tmp_path,monkeypatch,stop_during):
    import violet.engine as module
    engine,store=setup_engine(tmp_path)
    generated=asyncio.Event();sent=[];holder={}
    class Native:
        def send(self,text,identifier,target,allowed=None):
            if allowed and not allowed():raise SendCancelled('blocked')
            sent.append(text)
            if stop_during=='multipart':holder['loop'].call_soon_threadsafe(engine.request_stop)
        def alive(self):return True
        def close(self):pass
    class Bridge:
        account='test';self_id='wxid_self'
        contacts=[]
        def __init__(self,*args):pass
        def refresh(self):pass
        def resolve(self,*args,**kwargs):return {}
        def positions(self,group):return {}
        def sender(self):return Native()
        def snapshot(self,group,cursors,context):
            rows=[] if group!='a@chatroom' or cursors.get('db',0)>=1 else [
                {'group':group,'shard':'db','local_id':1,'create_time':1,'sender':'other','speaker':'成员','type':'文本','text':'问题','is_self':False}]
            return rows,rows
        def verify(self,delivery):return {'server_id':123}
    class Provider:
        async def complete(self,system,payload,schema):
            assert schema is Participation
            return Participation(participate=True,message_ids=[m['message_id'] for m in payload['new_messages']],kind='answer',reason='addressed')
        usage={'requests':0,'tokens':0}
        def __init__(self,*args):pass
        async def close(self):pass
        async def respond(self,*args,**kwargs):
            generated.set()
            if stop_during=='generation':await asyncio.Event().wait()
            return Reply(should_reply=True,replies=['第一条','第二条','第三条'])
    monkeypatch.setattr(module,'DeepSeek',Provider)
    engine.bridge_factory=Bridge
    async def run():
        holder['loop']=asyncio.get_running_loop()
        await engine.start()
        await asyncio.wait_for(generated.wait(),2)
        if stop_during=='generation':await asyncio.wait_for(engine.stop(),2)
        else:await asyncio.wait_for(engine.task,2)
    asyncio.run(run())
    assert sent==([] if stop_during=='generation' else ['第一条'])
    assert not engine.running and not store.outbox()
    if stop_during=='multipart':assert store.overview()['counts']['cancelled']==2
    store.close()
