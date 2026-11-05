import asyncio
import hashlib
import sqlite3
import threading
import time
from types import SimpleNamespace

from fastapi.testclient import TestClient
import pytest
from pydantic import ValidationError

from violet.bridge import Bridge,resolve_contact
from violet.engine import Engine
from violet.model import Reply
from violet.participation import ConversationPolicy
from violet.settings import ConfigStore,Group
from violet.store import Store
from violet.web import create_app


PERSON='wxid_friend'
GROUP='sample@chatroom'


def message(i=1,text='你好',sender=PERSON,source=PERSON,own=False):
    return {'group':source,'shard':'db','local_id':i,'create_time':int(time.time())+i,
            'sender':sender,'speaker':'测试联系人','is_self':own,'type':'文本','text':text,'sort_seq':i}


@pytest.fixture
def private_engine(tmp_path):
    config=ConfigStore(tmp_path/'config.json')
    config.update({'groups':[{'id':PERSON,'name':'测试联系人','mode':'mention'},
                             {'id':GROUP,'name':'测试群'}]})
    store=Store(tmp_path/'state.db');engine=Engine(config,store,tmp_path)
    for group in config.value.groups:store.seed(group.id,{})
    class LocalBridge:
        self_id='bot';names={'bot':'GPT小姐',PERSON:'测试联系人'}
        def snapshot(self,identifier,cursors,limit):return [],engine.context.get(identifier,[])
        def positions(self,identifier):return {}
        def resolve(self,identifier,admin=False):return {'username':identifier}
    engine.bridge=LocalBridge();engine.intent_running=True
    class Provider:
        payloads=[]
        async def complete(self,*args):raise AssertionError('private chat must not invoke ambient participation/planner')
        async def respond(self,system,payload,*args,**kwargs):
            self.payloads.append((system,payload));return Reply(should_reply=True,replies=['私聊答复'])
    engine.provider=Provider();engine.provider.payloads=[]
    yield engine,store,config.value.groups[0]
    store.close()


def test_private_ids_and_legacy_group_config_need_no_migration():
    assert Group(id=PERSON,name='联系人').is_private
    assert not Group(id=GROUP,name='群聊').is_private
    for invalid in ['../data','@panel','x y','x\x00y','x/y']:
        with pytest.raises(ValidationError):Group(id=invalid,name='非法')


def test_private_text_is_direct_without_at_even_in_legacy_mention_mode(private_engine):
    engine,store,person=private_engine
    engine.memory.save(GROUP,'群聊','不能带入私聊的群背景')
    engine.memory.save(PERSON,'联系人','这位联系人喜欢简短答案')
    engine.context[GROUP]=[message(text='另一群的秘密',source=GROUP)]
    engine.context[PERSON]=[message(0,'私聊先前的问题')]
    # Force an exhausted proactive budget; direct personal replies still work.
    store.db.execute('INSERT INTO chat_sent VALUES(?,?,?,?,?)',('old',PERSON,'old',time.time(),10000));store.db.commit()
    assert asyncio.run(engine.generate(person,[message(text='@其他人 这句话是什么意思')]))==['私聊答复']
    system,payload=engine.provider.payloads[-1]
    assert '一对一' in system and payload['conversation_type']=='person'
    assert '这位联系人' in payload['group_memory_markdown']
    assert '另一群的秘密' not in str(payload) and '不能带入私聊的群背景' not in str(payload)
    assert engine.reply_plans[PERSON]['direct_invitation']


def test_private_quiet_pause_self_media_and_stop_are_respected(private_engine):
    engine,store,person=private_engine
    assert asyncio.run(engine.generate(person,[message(text='闭嘴')]))==[]
    assert asyncio.run(engine.generate(person,[message(2,'普通新消息')]))==[]
    assert not engine.provider.payloads
    assert asyncio.run(engine.generate(person,[message(3,'@GPT小姐 继续这个问题')]))==['私聊答复']
    assert store.policy(PERSON)['quiet_until']>time.time()
    engine.policy.wake(person);store.pause(PERSON,True)
    assert asyncio.run(engine.generate(person,[message(4,'暂停时不应自动回复')]))==[]
    store.pause(PERSON,False)
    assert asyncio.run(engine.generate(person,[message(5,'自己发出的',sender='bot',own=True)]))==[]
    assert asyncio.run(engine.generate(person,[{**message(6),'type':'图片'}]))==[]
    engine.request_stop()
    assert asyncio.run(engine.generate(person,[message(7,'停止后')]))==[]


@pytest.mark.parametrize('role',['member','secondary','admin'])
def test_private_role_controls_commands_and_agent(private_engine,role):
    engine,store,person=private_engine
    if role=='secondary':engine.secondary_admin_ids={PERSON}
    if role=='admin':engine.admin_ids={PERSON}
    sent=[]
    class Manager:
        async def submit(self,*args,**kwargs):sent.append(kwargs);return {'id':'qa-job'}
    engine.agent_manager=Manager()
    result=asyncio.run(engine.admin(message(text='/agent project 分析刚才')))
    if role=='admin':
        assert 'qa-job' in result and sent[0]['source']==PERSON
        assert sent[0]['source_context']['conversation_type']=='person'
    elif role=='secondary':assert '仅可执行 /command' in result and not sent
    else:assert result is None and not sent
    result=asyncio.run(engine.admin(message(2,'/command send wxid_target | 测试正文')))
    assert ('已排队' in (result or ''))==(role!='member')


def test_private_delivery_still_requires_selected_source_and_global_running(private_engine):
    engine,store,person=private_engine
    store.reserve_direct('private_test',PERSON,PERSON,'待发',{})
    row=store.outbox()[0]
    assert engine.delivery_allowed(row)
    person.enabled=False;assert not engine.delivery_allowed(row)
    person.enabled=True;engine.intent_running=False;assert not engine.delivery_allowed(row)


def test_bridge_preserves_private_body_and_real_identity_and_skips_no_history(tmp_path):
    path=tmp_path/'messages.db';table='Msg_'+hashlib.md5(PERSON.encode()).hexdigest()
    with sqlite3.connect(path) as c:
        c.execute('CREATE TABLE Name2Id(user_name TEXT)')
        c.executemany('INSERT INTO Name2Id VALUES(?)',[(PERSON,),('bot',),('wxid_stranger',)])
        c.execute(f'CREATE TABLE "{table}"(local_id,create_time,sort_seq,real_sender_id,local_type,message_content,compress_content,server_id,status)')
        c.executemany(f'INSERT INTO "{table}" VALUES(?,?,?,?,?,?,?,?,?)',[
            (1,1,1,1,1,'wxid_example: 正文要原样保留',None,101,2),(2,2,2,2,1,'自己发的',None,102,2)])
    class DB:
        def _message_dbs(self):return ['message/message_0.db']
        def _open(self,rel):
            c=sqlite3.connect(path);c.row_factory=sqlite3.Row;return c
    bridge=Bridge.__new__(Bridge);bridge.db=DB();bridge.self_id='bot';bridge.lock=threading.RLock();bridge.names={PERSON:'好友','bot':'自己'}
    rows,_=bridge.snapshot(PERSON,{})
    assert rows[0]['text']=='wxid_example: 正文要原样保留' and rows[0]['sender']==PERSON
    assert not rows[0]['is_self'] and rows[1]['is_self']
    assert bridge.positions('wxid_no_history')=={} and bridge.snapshot('wxid_no_history',{})==([],[])
    with sqlite3.connect(path) as c:c.execute(f'UPDATE "{table}" SET real_sender_id=3 WHERE local_id=1')
    with pytest.raises(RuntimeError,match='真实发送者'):bridge.snapshot(PERSON,{})


class Contacts:
    account='example_account';self_id='bot'
    contacts=[{'username':PERSON,'alias':'PRIMARY','nick_name':'测试联系人','remark':''},
              {'username':'wxid_other','alias':'OTHER','nick_name':'其他联系人','remark':''},
              {'username':'bot','alias':'SELF','nick_name':'机器人','remark':''},
              {'username':'gh_service','alias':'','nick_name':'公众号','remark':''},
              {'username':'filehelper','alias':'','nick_name':'文件传输助手','remark':''},
              {'username':GROUP,'alias':'','nick_name':'测试群','remark':''}]
    def __init__(self,*args):pass
    def refresh(self):pass
    def resolve(self,query,admin=False):return resolve_contact(self.contacts,query,admin)


def test_contact_selection_and_per_contact_roles_remove_alias_grants(tmp_path):
    config=ConfigStore(tmp_path/'config.json')
    config.update({'groups':[{'id':PERSON,'name':'测试联系人'},{'id':GROUP,'name':'群聊'}],
                   'admin_accounts':['PRIMARY'],'secondary_admin_accounts':['OTHER']})
    app=create_app(config.path,tmp_path/'runtime',bridge_factory=Contacts)
    with TestClient(app) as client:
        headers={'x-violet-local':client.get('/api/session').json()['token']}
        assert [r['username'] for r in client.get('/api/contacts').json()]==[GROUP]
        assert {r['username'] for r in client.get('/api/contacts?kind=person').json()}=={PERSON,'wxid_other'}
        assert len(client.get('/api/contacts?kind=all').json())==3
        assert client.get('/api/contacts?kind=unknown').status_code==422
        endpoint='/api/contacts/'+PERSON+'/role'
        assert client.post(endpoint,json={'role':'admin'}).status_code==403
        response=client.post(endpoint,headers=headers,json={'role':'secondary'})
        assert response.status_code==200
        assert response.json()['settings']['admin_accounts']==[]
        assert set(response.json()['settings']['secondary_admin_accounts'])=={'OTHER',PERSON}
        assert PERSON not in app.state.engine.admin_ids and PERSON in app.state.engine.secondary_admin_ids
        response=client.post(endpoint,headers=headers,json={'role':'member'})
        assert response.status_code==200 and PERSON not in app.state.engine.command_admin_ids
        assert response.json()['settings']['secondary_admin_accounts']==['OTHER']
        response=client.post(endpoint,headers=headers,json={'role':'admin'})
        assert response.status_code==200 and PERSON in app.state.engine.admin_ids
        assert client.post('/api/contacts/'+GROUP+'/role',headers=headers,json={'role':'admin'}).status_code==404
        assert client.post(endpoint,headers=headers,json={'role':'superuser'}).status_code==400
        assert client.put('/api/settings',headers=headers,json={'groups':[{'id':'bot','name':'自己'}]}).status_code==400
