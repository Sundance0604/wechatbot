import asyncio
import hashlib
import json
from types import SimpleNamespace

import pytest

from violet.commands import parse
from violet.engine import Engine
from violet.settings import ConfigStore,Group
from violet.store import Store,key


def message(i=20,text='/command forward 3 -> ExampleContact',group='a@chatroom',sender='owner',**extra):
    return {'group':group,'shard':'db','local_id':i,'create_time':990+i,'sender':sender,'speaker':sender,
            'is_self':False,'type':'文本','text':text,**extra}


class Bridge:
    self_id='bot';names={'bot':'VioletBot'}
    def __init__(self):
        self.rows=[message(i,'内容'+str(i),sender='friend') for i in range(1,7)]
        self.rows+=[message(7,'其他群的私有内容',group='b@chatroom')]
        self.calls=[]
    def resolve(self,name):
        contacts={'examplecontact':'wxid_example','bob':'wxid_bob','wxid_example':'wxid_example','wxid_bob':'wxid_bob'}
        if name.casefold() not in contacts:raise ValueError('未找到唯一联系人')
        return {'username':contacts[name.casefold()]}
    def snapshot(self,group,cursors,limit):self.calls.append(group);return [],self.rows[-limit:]
    def positions(self,group):return {'db':6}
    def verify(self,delivery):return {'server_id':123}


@pytest.fixture
def setup(tmp_path):
    config=ConfigStore(tmp_path/'config.json');config.update({'groups':[{'id':'a@chatroom','name':'同学群','chat_style':'classmates'},
                                                                    {'id':'b@chatroom','name':'家族群','chat_style':'family','bot_name':'小助手','mentions':['小水']}]})
    store=Store(tmp_path/'state.db');engine=Engine(config,store,tmp_path);engine.bridge=Bridge();engine.admin_ids={'owner','owner2'};engine.intent_running=True
    for group in config.value.groups:store.seed(group.id,{})
    class Forbidden:
        async def complete(self,*args,**kwargs):raise AssertionError('explicit command must not call a model')
    engine.provider=Forbidden()
    yield engine,store
    store.close()


def batch_id(message):return hashlib.sha256((key(message)+':command').encode()).hexdigest()[:24]


def test_forward_is_local_scoped_and_each_item_is_queued_once(setup):
    engine,store=setup;source=message()
    result=asyncio.run(engine.admin(source))
    assert '已排队' in result
    outgoing=store.outbox();assert len(outgoing)==3
    assert all(d['group_id']=='wxid_example' for d in outgoing)
    assert '内容4' in outgoing[0]['body'] and '内容6' in outgoing[-1]['body']
    assert '其他群' not in json.dumps(outgoing,ensure_ascii=False)
    assert all(len(d['body'].encode())<=1800 for d in outgoing)
    assert asyncio.run(engine.admin(source))==result and len(store.outbox())==3
    assert engine.bridge.calls==['a@chatroom']


def test_multiple_destinations_are_resolved_before_any_queue_write(setup):
    engine,store=setup
    result=asyncio.run(engine.admin(message(text='/command send ["ExampleContact","不存在"] | 正文')))
    assert '未找到唯一' in result and not store.outbox()
    result=asyncio.run(engine.admin(message(21,'/command send ["ExampleContact","Bob"] | 你好 @VioletBot')))
    assert '已排队' in result and len(store.outbox())==2
    assert {d['group_id'] for d in store.outbox()}=={'wxid_example','wxid_bob'}
    assert all(d['body']=='你好 @VioletBot' for d in store.outbox())


def test_command_status_cancel_and_reopen_are_owner_and_group_scoped(setup,tmp_path):
    engine,store=setup;source=message();asyncio.run(engine.admin(source));identifier=batch_id(source)
    assert '没有找到' in asyncio.run(engine.admin(message(21,'/command status '+identifier,sender='owner2')))
    assert '没有找到' in asyncio.run(engine.admin(message(22,'/command cancel '+identifier,group='b@chatroom')))
    assert len(store.outbox())==3
    assert '取消3条' in asyncio.run(engine.admin(message(23,'/command cancel '+identifier)))
    assert not store.outbox()
    reopened=Store(tmp_path/'state.db')
    try:assert reopened.command_batch(identifier)['counts']['cancelled']==3
    finally:reopened.close()


def test_acknowledgement_and_cancellation_are_not_stuck_behind_batch(setup):
    engine,store=setup;source=message();asyncio.run(engine.admin(source));identifier=batch_id(source)
    control=message(21,'/command cancel '+identifier);store.poll('a@chatroom',[control])
    sent=[]
    async def run():
        loop=asyncio.get_running_loop()
        class Native:
            def send(self,text,identifier,target,allowed=None):
                sent.append((text,target));loop.call_soon_threadsafe(engine.done.set)
        engine.sender=Native()
        await asyncio.wait_for(engine.worker(),2)
    asyncio.run(run())
    assert len(sent)==1 and sent[0][1]=='a@chatroom'
    assert store.command_batch(identifier)['counts']['cancelled']==3


def test_command_cannot_bypass_admin_or_stop_or_item_limit(setup):
    engine,store=setup
    assert asyncio.run(engine.admin(message(sender='friend'))) is None
    engine.intent_running=False
    assert '监听已停止' in asyncio.run(engine.admin(message()))
    assert not store.outbox()
    engine.intent_running=True;engine.bridge.rows=[message(i,'长文字'*700) for i in range(1,51)]
    result=asyncio.run(engine.admin(message(99,'/command forward 50 -> ["ExampleContact","Bob"]')))
    assert '超过100条' in result and not store.outbox()


@pytest.mark.parametrize('text',['/command forward 0 -> A','/command forward 51 -> A','/command send [] | x','/command rm -rf any','/command send A | \x00'])
def test_invalid_commands_never_become_shell_or_model_requests(text):
    with pytest.raises(ValueError):parse(text)


def test_agent_snapshot_has_only_source_history_before_trigger_and_matching_style(setup):
    engine,store=setup
    engine.memory.save('a@chatroom','同学群','同学群里的国匪梗')
    engine.bridge.rows=[message(1,'家族群最近的事情',group='b@chatroom'),message(2,'稍后的消息',group='b@chatroom'),
                        message(1,'同学群不能带过去',group='a@chatroom')]
    trigger=message(2,'/agent project 解释刚才',group='b@chatroom')
    snapshot=asyncio.run(engine.agent_source_context(trigger))
    assert snapshot['group_id']=='b@chatroom' and snapshot['group_name']=='家族群'
    assert snapshot['message_count']==1 and snapshot['messages'][0]['text']=='家族群最近的事情'
    assert not snapshot['has_group_memory'] and snapshot['group_memory_markdown']==''
    assert snapshot['bot_name']=='小助手' and '亲切' in snapshot['persona']
    assert '同学群不能带过去' not in json.dumps(snapshot,ensure_ascii=False)
    settings=engine.conversation_settings(engine.config.value.groups[1])
    assert '小水' in settings.mentions


def test_agent_snapshot_limit_zero_and_secret_redaction(setup):
    engine,_=setup;engine.config.value.groups[0].agent_context_messages=0
    assert asyncio.run(engine.agent_source_context(message()))['messages']==[]
    assert not engine.bridge.calls
    engine.config.value.groups[0].agent_context_messages=2
    from pydantic import SecretStr
    engine.config.value.api_key=SecretStr('TEST_SECRET')
    engine.bridge.rows=[message(1,'TEST_SECRET'),message(2,'正常内容')]
    snapshot=asyncio.run(engine.agent_source_context(message()))
    assert snapshot['message_count']==2 and 'TEST_SECRET' not in json.dumps(snapshot,ensure_ascii=False)


def test_family_chat_uses_its_own_alias_persona_and_memory(setup):
    from violet.model import Reply
    engine,_=setup;engine.memory.save('a@chatroom','同学群','同学群专属的私人梗')
    class Provider:
        async def respond(self,system,payload,*args,**kwargs):
            assert '亲切' in system and '不使用同学群互损' in system
            assert '同学群专属的私人梗' not in str(payload)
            assert payload['group']=='家族群'
            return Reply(should_reply=True,replies=['我是家里的AI小助手。'])
    engine.provider=Provider()
    result=asyncio.run(engine.generate(engine.config.value.groups[1],[message(text='@小水 你是谁',group='b@chatroom')]))
    assert result==['我是家里的AI小助手。']


def test_agent_command_passes_recent_context_to_manager(setup):
    engine,_=setup;captured={}
    class Manager:
        async def submit(self,*args,**kwargs):captured.update(kwargs);return {'id':'job123'}
    engine.agent_manager=Manager()
    source=message(text='/agent project 解释刚才')
    result=asyncio.run(engine.admin(source))
    assert 'job123' in result
    assert captured['source_context']['group_id']=='a@chatroom'
    assert captured['source_context']['message_count']==6
    assert captured['source_context']['requester_id']=='owner'


def test_agent_context_keeps_newest_messages_with_a_fixed_size_budget(setup):
    engine,_=setup;engine.config.value.groups[0].agent_context_messages=200
    engine.bridge.rows=[message(i,'长文本'*500) for i in range(1,200)]
    snapshot=asyncio.run(engine.agent_source_context(message(300,'/agent project 总结刚才')))
    assert 0<snapshot['message_count']<200
    assert snapshot['messages'][-1]['message_id'].endswith(':199')
    assert len(json.dumps(snapshot['messages'],ensure_ascii=False))<25000
