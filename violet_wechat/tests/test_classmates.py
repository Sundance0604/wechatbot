import asyncio
import json
from types import SimpleNamespace

import pytest

from violet.context import explicit_recipient
from violet.engine import Engine
from violet.model import AdminAction,AdminPlan,Participation,Reply
from violet.participation import ConversationPolicy
from violet.settings import ConfigStore
from violet.store import Store,key


def message(i=1,text='国还是匪',sender='owner',group='a@chatroom',**extra):
    return {'group':group,'shard':'db','local_id':i,'create_time':990+i,'sender':sender,'speaker':sender,
            'is_self':False,'type':'文本','text':text,**extra}


@pytest.fixture
def setup(tmp_path):
    config=ConfigStore(tmp_path/'config.json');config.update({'groups':[{'id':'a@chatroom','name':'同学群','chat_style':'classmates'},
                                                                    {'id':'b@chatroom','name':'其他群'}]})
    store=Store(tmp_path/'state.db');engine=Engine(config,store,tmp_path);clock=[1000.]
    engine.policy=ConversationPolicy(store,lambda:clock[0]);engine.intent_running=True
    for group in config.value.groups:store.seed(group.id,{})
    engine.bridge=SimpleNamespace(self_id='bot',names={'bot':'VioletBot'})
    yield engine,store,clock
    store.close()


class Provider:
    def __init__(self,participate=True):self.calls=[];self.participate=participate
    async def complete(self,system,payload,schema):
        self.calls.append(('decision',system,payload))
        return Participation(participate=self.participate,message_ids=[m['message_id'] for m in payload['new_messages']],
                             kind='casual',reason='useful' if self.participate else 'no_value')
    async def respond(self,system,payload,*args,**kwargs):
        self.calls.append(('reply',system,payload))
        return Reply(should_reply=True,replies=['这轮匪，发言太露了。'])


def remember_turn(engine,store,clock,source=None):
    source=source or message(text='@VioletBot 群里最嘴欠的是谁')
    store.reserve(source['group'],[source],['这轮先点你'],{},metadata={'kind':'chat','source_messages':[engine.chat_message(source)]})
    delivery=store.outbox()[0];store.update_outgoing(delivery['id'],'sent',123,confirmed_at=clock[0])
    with store.db:store.db.execute('UPDATE outgoing SET created=? WHERE id=?',(clock[0],delivery['id']))


def test_classmates_can_join_quoted_banter_without_repeating_disclaimers(setup):
    engine,_,_=setup;engine.provider=Provider()
    source=message(type='引用回复',reply_to={'sender':'other_friend','content':'你是匪'})
    result=asyncio.run(engine.generate(engine.config.value.groups[0],[source]))
    assert result==['这轮匪，发言太露了。']
    assert '不需要等@' in engine.provider.calls[0][1]
    assert '不反复叠甲' in engine.provider.calls[1][1]


def test_short_followup_continues_same_person_without_waiting_for_at_or_cooldown(setup):
    engine,store,clock=setup;remember_turn(engine,store,clock);engine.provider=Provider(participate=False)
    clock[0]+=20
    result=asyncio.run(engine.generate(engine.config.value.groups[0],[message(2,'必须说一个')]))
    assert result and [c[0] for c in engine.provider.calls]==['reply']
    assert engine.reply_plans['a@chatroom']['conversation_reply']
    assert not engine.reply_plans['a@chatroom']['direct_invitation']
    assert engine.provider.calls[0][2]['conversation_followups']


def test_followup_does_not_override_quiet_or_cross_group_or_sender(setup):
    engine,store,clock=setup;remember_turn(engine,store,clock);engine.provider=Provider(participate=False)
    engine.policy.quiet(engine.config.value.groups[0],'闭嘴')
    assert asyncio.run(engine.generate(engine.config.value.groups[0],[message(2,'必须说一个')]))==[]
    assert not engine.provider.calls
    assert asyncio.run(engine.generate(engine.config.value.groups[1],[message(3,'必须说一个',group='b@chatroom')]))==[]
    assert engine.provider.calls[0][0]=='decision'


def test_generic_at_points_to_latest_unanswered_question_instead_of_old_topic(setup):
    engine,store,clock=setup;group=engine.config.value.groups[0]
    old=message(1,'甲是谁');remember_turn(engine,store,clock,old)
    pending=message(2,'我是谁',sender='friend_b')
    engine.context[group.id]=[old,pending]
    engine.provider=Provider()
    assert asyncio.run(engine.generate(group,[message(4,'问你@VioletBot')]))
    refs=engine.provider.calls[0][2]['explicit_question_references']
    assert len(refs)==1 and refs[0]['sender']=='friend_b' and refs[0]['text']=='我是谁'
    assert any(s['sender']=='friend_b' for s in engine.reply_plans[group.id]['source_messages'])


def test_greeting_does_not_pick_up_old_unanswered_question(setup):
    engine,_,_=setup;group=engine.config.value.groups[0]
    engine.context[group.id]=[message(1,'某个旧问题是谁')];engine.provider=Provider()
    assert asyncio.run(engine.generate(group,[message(3,'在吗@VioletBot')]))
    assert not engine.provider.calls[0][2]['explicit_question_references']


@pytest.mark.parametrize('text,expected',[('给contacta发消息','contacta'),('给 contacta 发消息','contacta'),
                                         ('发消息 ContactA | 你好','ContactA'),('我让你给contacta 发','contacta')])
def test_recipient_is_taken_from_owner_words(text,expected):
    assert explicit_recipient(text)==expected


def test_assistant_mention_is_removed_anywhere_in_request_but_preserved_in_body(setup):
    engine,_,_=setup
    assert engine.strip_mention('给contacta发消息@VioletBot')=='给contacta发消息'
    assert engine.strip_mention('@VioletBot 给contacta发消息')=='给contacta发消息'
    assert engine.strip_mention('发消息 ContactA | 告诉他 @VioletBot 是机器人')=='发消息 ContactA | 告诉他 @VioletBot 是机器人'


class Contacts:
    self_id='bot';names={'bot':'VioletBot'}
    def resolve(self,query):
        values={'contacta':'friend_super','friend_super':'friend_super','violetbot':'bot','bot':'bot','bob':'friend_bob','friend_bob':'friend_bob'}
        if query.casefold() not in values:raise ValueError('没有唯一联系人')
        return {'username':values[query.casefold()]}


def test_clarification_keeps_original_recipient_even_if_model_mistakes_self_alias(setup):
    engine,store,_=setup;engine.bridge=Contacts();engine.admin_ids={'owner'};calls=[]
    class Planner:
        async def complete(self,system,payload,schema):
            assert '@VioletBot' not in payload['owner_message']
            if 'pending_context' not in payload:
                assert payload['explicit_recipient']['username']=='friend_super'
                return AdminPlan(clarification='发什么内容？')
            assert payload['pending_recipient']['username']=='friend_super'
            return AdminPlan(context_action='continue',actions=[AdminAction(kind='send_message',target='VioletBot',text='你好')])
    async def perform(action,*args):calls.append(action.target);return '发送确认'
    engine.provider=Planner();engine.perform=perform
    assert asyncio.run(engine.admin(message(text='给contacta发消息@VioletBot')))=='发什么内容？'
    assert asyncio.run(engine.admin(message(2,'violetbot是你啊，随便发点，私聊')))=='发送确认'
    assert calls==['friend_super']


def test_model_cannot_retarget_a_vague_request_to_assistant_self(setup):
    engine,_,_=setup;engine.bridge=Contacts();engine.admin_ids={'owner'}
    class Planner:
        async def complete(self,*args):return AdminPlan(actions=[AdminAction(kind='send_message',target='VioletBot',text='错目标')])
    async def forbidden(*args):raise AssertionError('must not send')
    engine.provider=Planner();engine.perform=forbidden
    assert '没有发送' in asyncio.run(engine.admin(message(text='给某人发消息')))


def test_another_admin_cannot_borrow_a_pending_recipient(setup):
    engine,_,_=setup;engine.bridge=Contacts();engine.admin_ids={'owner','other_owner'}
    class Planner:
        async def complete(self,*args):return AdminPlan(clarification='正文？')
    engine.provider=Planner()
    asyncio.run(engine.admin(message(text='给contacta发消息')))
    assert asyncio.run(engine.admin(message(2,'随便发点',sender='other_owner'))) is None
