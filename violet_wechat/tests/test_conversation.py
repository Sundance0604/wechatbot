import asyncio
from types import SimpleNamespace

from fastapi.testclient import TestClient
import pytest

from violet.context import order
from violet.engine import Engine
from violet.model import Participation,Reply,SendReview
from violet.participation import ConversationPolicy
from violet.settings import ConfigStore,Group
from violet.store import Store,key
from violet.web import create_app


def message(i=1,text='请回答这个问题',group='a@chatroom',sender='member',**extra):
    return {'group':group,'shard':'db','local_id':i,'create_time':990+i,'sender':sender,
            'speaker':sender,'is_self':False,'type':'文本','text':text,**extra}


class Provider:
    def __init__(self,participate=True,kind='answer',replies=None):
        self.participate,self.kind=participate,kind
        self.replies=replies or ['实际回答']
        self.calls=[];self.review_send=False;self.on_reply=None;self.on_review=None
    async def complete(self,system,payload,schema):
        self.calls.append((schema.__name__,payload))
        if schema is Participation:
            return Participation(participate=self.participate,message_ids=[m['message_id'] for m in payload['new_messages']],
                                 kind=self.kind,reason='addressed' if self.participate else 'other_conversation')
        assert schema is SendReview
        if self.on_review:self.on_review()
        return SendReview(send=self.review_send,reason='still_relevant' if self.review_send else 'outdated')
    async def respond(self,system,payload,*args,**kwargs):
        self.calls.append(('Reply',payload))
        if self.on_reply:self.on_reply()
        return Reply(should_reply=True,replies=self.replies)


@pytest.fixture
def setup(tmp_path):
    config=ConfigStore(tmp_path/'config.json')
    config.update({'groups':[{'id':'a@chatroom','name':'甲群'},{'id':'b@chatroom','name':'乙群'}]})
    store=Store(tmp_path/'state.db')
    engine=Engine(config,store,tmp_path);clock=[1000.]
    engine.policy=ConversationPolicy(store,lambda:clock[0]);engine.intent_running=True
    for group in config.value.groups:store.seed(group.id,{})
    engine.provider=Provider();engine.bridge=SimpleNamespace(self_id='bot',names={})
    yield engine,store,clock
    store.close()


def queued(engine,store,texts=None,source=None):
    source=source or message()
    group=next(g for g in engine.config.value.groups if g.id==source['group'])
    store.poll(group.id,[source])
    metadata={'kind':'chat','revision':store.policy(group.id)['revision'],'observed':{'db':source['local_id']},
              'source_order':list(order(source)),'source_keys':[key(source)],'source_messages':[engine.chat_message(source)]}
    batch=store.reserve(group.id,[source],texts or ['旧回答'],{},metadata=metadata)
    return next(d for d in store.outbox() if d['id'].startswith(batch)),metadata


class Bridge:
    self_id='bot';names={}
    def __init__(self,rows):self.rows=rows;self.reads=0
    def snapshot(self,group,cursors,limit):
        self.reads+=1
        rows=[m for m in self.rows if m['group']==group]
        return [m for m in rows if m['local_id']>cursors.get('db',0)],rows[-limit:]
    def positions(self,group):return {}
    def verify(self,delivery):return {'server_id':123}


class Native:
    def __init__(self):self.sent=[];self.cancelled=0
    def send(self,text,identifier,target,allowed=None):
        assert allowed is None or allowed()
        self.sent.append(text)
    def cancel_pending(self):self.cancelled+=1


def test_no_participation_means_no_generation_or_acknowledgement(setup):
    engine,store,_=setup;engine.provider=Provider(participate=False)
    assert asyncio.run(engine.generate(engine.config.value.groups[0],[message()]))==[]
    assert [name for name,_ in engine.provider.calls]==['Participation']
    assert not store.outbox()


def test_invalid_focus_cannot_be_used_to_generate_a_reply(setup):
    engine,_,_=setup
    class Invalid(Provider):
        async def complete(self,*args):return Participation(participate=True,message_ids=['other-group:forged'],reason='addressed')
    engine.provider=Invalid()
    assert asyncio.run(engine.generate(engine.config.value.groups[0],[message()]))==[]
    assert not engine.provider.calls


def test_casual_is_one_short_message_and_silence_narration_is_removed(setup):
    engine,_,_=setup;group=engine.config.value.groups[0];group.casual_max_chars=20
    engine.provider=Provider(kind='casual',replies=['啊'*100,'多余的第二条'])
    assert asyncio.run(engine.generate(group,[message()]))==['啊'*20]
    engine.provider=Provider(replies=['收到，我不接了。'])
    assert asyncio.run(engine.generate(group,[message(2)]))==[]


def test_group_message_and_character_budgets_survive_restart_without_double_counting(setup,tmp_path):
    engine,store,clock=setup;group=engine.config.value.groups[0];group.message_budget=1;group.character_budget=50
    delivery,_=queued(engine,store,['字'*50])
    store.update_outgoing(delivery['id'],'sent',123,confirmed_at=clock[0])
    store.update_outgoing(delivery['id'],'sent',123,confirmed_at=clock[0])
    assert not engine.policy.capacity(group)['allowed']
    assert engine.policy.capacity(group)['used_messages']==1
    assert engine.policy.capacity(engine.config.value.groups[1])['allowed']
    reopened=Store(tmp_path/'state.db')
    try:
        policy=ConversationPolicy(reopened,lambda:clock[0])
        assert policy.capacity(group)['used_characters']==50
        clock[0]+=group.budget_window_seconds+1
        assert policy.capacity(group)['allowed']
    finally:reopened.close()


def test_cooldown_is_per_group_and_shared_by_parts_of_one_answer(setup):
    engine,store,clock=setup;group=engine.config.value.groups[0]
    delivery,_=queued(engine,store,['一','二'])
    store.update_outgoing(delivery['id'],'sent',123,confirmed_at=clock[0])
    batch=store.chat_meta(delivery['id'])['batch_id']
    assert engine.policy.capacity(group,batch_id=batch)['allowed']
    assert engine.policy.capacity(group)['blocked_by']=='cooldown'
    assert engine.policy.capacity(engine.config.value.groups[1])['allowed']
    clock[0]+=group.cooldown_seconds
    assert engine.policy.capacity(group)['allowed']


def test_control_delivery_is_not_muted_or_charged_as_chitchat(setup):
    engine,store,clock=setup;group=engine.config.value.groups[0]
    engine.policy.quiet(group,'群友要求安静')
    store.reserve_direct('admin_action','target',group.id,'管理员明确要求的消息',{})
    delivery=store.delivery('admin_action')
    assert engine.delivery_allowed(delivery)
    store.update_outgoing('admin_action','sent',123,confirmed_at=clock[0])
    assert engine.policy.capacity(group)['used_messages']==0


@pytest.mark.parametrize('text',['friend_c，原神好玩还是鸣潮好玩','@其他群友 你好','friend_b，这周末回来吗'])
def test_named_other_person_is_dropped_before_model_call(setup,text):
    engine,_,_=setup;group=engine.config.value.groups[0];group.other_names=['friend_c','friend_b']
    assert asyncio.run(engine.generate(group,[message(text=text)]))==[]
    assert not engine.provider.calls


def test_quote_to_someone_else_is_not_an_invitation_but_quote_to_bot_can_be(setup):
    engine,_,_=setup;group=engine.config.value.groups[0]
    quoted=message(text='你说得对',type='引用回复',reply_to={'sender':'other','content':'原话'})
    assert asyncio.run(engine.generate(group,[quoted]))==[]
    assert not engine.provider.calls
    quoted['reply_to']['sender']='bot'
    assert asyncio.run(engine.generate(group,[quoted]))==['实际回答']


def test_human_dialogue_reaches_decision_stage_with_context_but_no_reply_when_not_invited(setup):
    engine,_,_=setup;group=engine.config.value.groups[0]
    engine.context[group.id]=[message(i,str(i),sender='甲' if i%2 else '乙') for i in range(1,5)]
    engine.provider=Provider(participate=False)
    assert asyncio.run(engine.generate(group,[message(5,'那我们周末再约',sender='甲')]))==[]
    assert len(engine.provider.calls[0][1]['recent_context'])==4
    assert len(engine.provider.calls)==1


@pytest.mark.parametrize('text',['你先安静一下','@GPT小姐 别说话','让你别接，为什么还回复','我都没问你，你回复什么','别抢答了'])
def test_quiet_requests_persist_without_an_acknowledgement(setup,tmp_path,text):
    engine,store,clock=setup;group=engine.config.value.groups[0];source=message(text=text)
    assert asyncio.run(engine.generate(group,[source]))==[]
    assert engine.policy.capacity(group)['blocked_by']=='quiet'
    assert not engine.provider.calls
    until=store.policy(group.id)['quiet_until']
    clock[0]+=5
    asyncio.run(engine.observe_conversation(group,[source]))
    assert store.policy(group.id)['quiet_until']==until
    second=Store(tmp_path/'state.db')
    try:assert ConversationPolicy(second,lambda:clock[0]).capacity(group)['blocked_by']=='quiet'
    finally:second.close()
    clock[0]=until+1
    assert engine.policy.capacity(group)['allowed']


def test_old_or_other_group_silence_does_not_rearm_a_group_pause(setup):
    engine,store,_=setup;group=engine.config.value.groups[0]
    asyncio.run(engine.observe_conversation(group,[message(text='你先安静一下',create_time=1),message(2,'你先安静一下',group='b@chatroom')]))
    assert engine.policy.capacity(group)['allowed']


def test_quiet_during_generation_discards_the_completed_answer(setup):
    engine,store,_=setup;group=engine.config.value.groups[0]
    engine.provider.on_reply=lambda:engine.policy.quiet(group,'生成期间要求安静')
    assert asyncio.run(engine.generate(group,[message()]))==[]
    assert not store.outbox()


def test_quiet_during_decision_prevents_body_generation(setup):
    engine,store,_=setup;group=engine.config.value.groups[0]
    class Racy(Provider):
        async def complete(self,*args):
            result=await super().complete(*args)
            engine.policy.quiet(group,'参与判定期间安静')
            return result
    engine.provider=Racy()
    assert asyncio.run(engine.generate(group,[message()]))==[]
    assert [name for name,_ in engine.provider.calls]==['Participation']


def test_character_budget_is_checked_again_at_actual_delivery(setup):
    engine,store,_=setup;group=engine.config.value.groups[0];group.character_budget=50
    delivery,_=queued(engine,store,['字'*60]);engine.sender=Native()
    asyncio.run(engine.deliver(delivery))
    assert not engine.sender.sent and store.delivery(delivery['id'])['status']=='cancelled'


def test_legacy_chat_queue_without_generation_context_is_not_sent(setup):
    engine,store,_=setup
    store.reserve('a@chatroom',[message()],['没有来源信息的旧回复'],{})
    engine.sender=Native();delivery=store.outbox()[0]
    asyncio.run(engine.deliver(delivery))
    assert not engine.sender.sent and store.delivery(delivery['id'])['status']=='cancelled'


def test_profile_file_changed_outside_panel_also_invalidates_old_answer(setup):
    from violet.memory import GroupMemory
    engine,store,_=setup
    old=engine.profile.save('原来的公开背景')
    delivery,_=queued(engine,store)
    meta=store.chat_meta(delivery['id']);meta['profile_version']=old['version'];store.update_chat_meta(delivery['id'],meta)
    engine.profile.save('更新后的公开背景',old['version'])
    engine.sender=Native()
    asyncio.run(engine.deliver(delivery))
    assert not engine.sender.sent and store.delivery(delivery['id'])['status']=='cancelled'


def test_latest_correction_cancels_all_parts_without_calling_sender(setup):
    engine,store,_=setup;delivery,_=queued(engine,store,['旧第一条','旧第二条'])
    engine.bridge=Bridge([message(),message(2,'不是问你，你回复什么')]);engine.sender=Native()
    asyncio.run(engine.deliver(delivery))
    assert not engine.sender.sent and not store.outbox()
    assert all(d['status']=='cancelled' for d in store.overview()['deliveries'])
    assert not engine.provider.calls


def test_relevance_review_cancels_outdated_answer_and_preserves_new_input(setup):
    engine,store,_=setup;delivery,_=queued(engine,store)
    engine.bridge=Bridge([message(),message(2,'问题已经解决了')]);engine.sender=Native()
    asyncio.run(engine.deliver(delivery))
    assert not engine.sender.sent
    assert [name for name,_ in engine.provider.calls]==['SendReview']
    assert store.batch('a@chatroom',10)[0]['local_id']==2


def test_still_relevant_answer_is_sent_only_after_two_fresh_reads(setup):
    engine,store,_=setup;delivery,_=queued(engine,store)
    engine.bridge=Bridge([message(),message(2,'请继续解释')]);engine.sender=Native();engine.provider.review_send=True
    asyncio.run(engine.deliver(delivery))
    assert engine.sender.sent==['旧回答'] and engine.bridge.reads>=2
    assert store.delivery(delivery['id'])['status']=='sent'


def test_new_message_during_review_discards_old_answer_even_if_review_was_positive(setup):
    engine,store,_=setup;delivery,_=queued(engine,store)
    engine.bridge=Bridge([message(),message(2,'补充一个条件')]);engine.sender=Native();engine.provider.review_send=True
    engine.provider.on_review=lambda:engine.bridge.rows.append(message(3,'刚才条件已经改了'))
    asyncio.run(engine.deliver(delivery))
    assert not engine.sender.sent and store.delivery(delivery['id'])['status']=='cancelled'


def test_new_correction_after_first_part_prevents_remaining_parts(setup):
    engine,store,_=setup;delivery,_=queued(engine,store,['第一条','第二条'])
    engine.bridge=Bridge([message()]);engine.sender=Native()
    asyncio.run(engine.deliver(delivery))
    assert engine.sender.sent==['第一条']
    engine.bridge.rows.append(message(2,'你先别说话'))
    asyncio.run(engine.deliver(store.outbox()[0]))
    assert engine.sender.sent==['第一条'] and not store.outbox()


def test_quiet_while_reading_final_positions_prevents_native_invocation(setup):
    engine,store,_=setup;delivery,_=queued(engine,store);group=engine.config.value.groups[0]
    class RacyBridge(Bridge):
        def positions(self,group_id):engine.policy.quiet(group,'最后一次读取时要求安静');return {}
    engine.bridge=RacyBridge([message()]);engine.sender=Native()
    asyncio.run(engine.deliver(delivery))
    assert not engine.sender.sent and store.delivery(delivery['id'])['status']=='cancelled'


def test_fast_watcher_cancels_armed_chat_independently_of_poll_interval(setup):
    engine,store,_=setup;delivery,_=queued(engine,store)
    engine.config.value.poll_seconds=3600
    engine.bridge=Bridge([message(),message(2,'你先安静一下')]);engine.sender=Native()
    engine.active_source='a@chatroom';engine.active_delivery_id=delivery['id']
    async def run():
        watcher=asyncio.create_task(engine.watch_conversation())
        async with asyncio.timeout(1):
            while not engine.sender.cancelled:await asyncio.sleep(.01)
        engine.done.set();await watcher
    asyncio.run(run())
    assert engine.sender.cancelled and not engine.sender.sent
    assert not store.outbox()


def test_quiet_api_is_local_and_only_affects_selected_group(tmp_path):
    config=ConfigStore(tmp_path/'config.json');config.update({'groups':[{'id':'a@chatroom','name':'甲群'},{'id':'b@chatroom','name':'乙群'}]})
    app=create_app(config.path,tmp_path/'runtime')
    with TestClient(app) as client:
        path='/api/groups/a@chatroom/quiet';headers={'x-violet-local':client.get('/api/session').json()['token']}
        assert client.post(path,json={'quiet':True}).status_code==403
        state=client.post(path,json={'quiet':True},headers=headers).json()
        assert state['conversation']['a@chatroom']['blocked_by']=='quiet'
        assert state['conversation']['b@chatroom']['allowed']
        state=client.post(path,json={'quiet':False},headers=headers).json()
        assert state['conversation']['a@chatroom']['allowed']
        assert not state['running']


def test_quiet_resume_cannot_resurrect_an_old_reply(setup):
    engine,store,_=setup;delivery,_=queued(engine,store);group=engine.config.value.groups[0]
    engine.policy.quiet(group,'暂停');engine.policy.wake(group)
    assert not engine.delivery_allowed(delivery)


def test_group_policy_settings_have_hard_bounds():
    from pydantic import ValidationError
    for values in [{'message_budget':0},{'cooldown_seconds':-1},{'quiet_seconds':99999},{'character_budget':1}]:
        with pytest.raises(ValidationError):Group(id='a@chatroom',name='甲群',**values)


def test_actual_wechat_alias_after_bot_rename_is_a_mandatory_direct_invitation(setup):
    engine,_,_=setup
    engine.config.value.bot_name='新助手名';engine.config.value.mentions=['新助手名']
    engine.bridge.names={'bot':'VioletBot'}
    engine.provider=Provider(participate=False)
    assert asyncio.run(engine.generate(engine.config.value.groups[0],[message(text='@VioletBot\u2005contacta是谁')]))==['实际回答']
    assert [name for name,_ in engine.provider.calls]==['Reply']
    assert engine.reply_plans['a@chatroom']['direct_invitation']


@pytest.mark.parametrize('command',['不接','闭嘴','这句别接','@VioletBot\u2005别回了'])
def test_quiet_commands_mute_for_two_minutes_without_reply_but_new_at_still_works(setup,command):
    engine,store,clock=setup;group=engine.config.value.groups[0]
    engine.config.value.mentions=['VioletBot','GPT小姐']
    assert asyncio.run(engine.generate(group,[message(text=command)]))==[]
    assert store.policy(group.id)['quiet_until']==clock[0]+120
    assert not engine.provider.calls
    assert asyncio.run(engine.generate(group,[message(2,'普通群友的聊天')]))==[]
    assert asyncio.run(engine.generate(group,[message(3,'@VioletBot\u2005你是谁')]))==['实际回答']
    assert engine.policy.capacity(group)['blocked_by']=='quiet'
    clock[0]+=121
    assert engine.policy.capacity(group)['allowed']


def test_direct_invitation_bypasses_cooldown_and_budget_without_charging_proactive_quota(setup):
    engine,store,clock=setup;group=engine.config.value.groups[0];group.message_budget=1
    old,_=queued(engine,store)
    store.update_outgoing(old['id'],'sent',123,confirmed_at=clock[0])
    assert not engine.policy.capacity(group)['allowed']
    result=asyncio.run(engine.generate(group,[message(2,'@GPT小姐 请回答')]))
    assert result==['实际回答']
    metadata=engine.reply_plans[group.id]
    store.reserve(group.id,[message(2)],result,{},metadata=metadata)
    fresh=store.outbox()[0];store.update_outgoing(fresh['id'],'sent',124,confirmed_at=clock[0])
    assert engine.policy.capacity(group)['used_messages']==1


def test_new_at_after_quiet_in_same_batch_replies_but_later_quiet_revokes_old_at(setup):
    engine,_,_=setup;group=engine.config.value.groups[0]
    assert asyncio.run(engine.generate(group,[message(text='闭嘴'),message(2,'@GPT小姐 你好')]))==['实际回答']
    engine.provider.calls=[]
    assert asyncio.run(engine.generate(group,[message(3,'@GPT小姐 你好'),message(4,'闭嘴')]))==[]
    assert not engine.provider.calls


def test_direct_invitation_has_fallback_if_model_declines_or_fails(setup):
    from violet.model import DeepSeekError
    engine,_,_=setup;group=engine.config.value.groups[0]
    class Declined(Provider):
        async def respond(self,*args,**kwargs):return Reply(should_reply=False)
    engine.provider=Declined()
    assert asyncio.run(engine.generate(group,[message(text='@GPT小姐 你好')]))
    class Failed(Provider):
        async def respond(self,*args,**kwargs):raise DeepSeekError('模型暂时不可用')
    engine.provider=Failed()
    assert '没答出来' in asyncio.run(engine.generate(group,[message(2,'@GPT小姐 你好')]))[0]


def test_direct_reply_during_quiet_is_not_vetoed_by_unrelated_new_chatter(setup):
    engine,store,_=setup;group=engine.config.value.groups[0]
    engine.policy.quiet(group,'安静中')
    delivery,_=queued(engine,store,source=message(text='@GPT小姐 你是谁'))
    meta=store.chat_meta(delivery['id']);meta['direct_invitation']=True;store.update_chat_meta(delivery['id'],meta)
    engine.bridge=Bridge([message(text='@GPT小姐 你是谁'),message(2,'其他人在聊别的')]);engine.sender=Native()
    asyncio.run(engine.deliver(delivery))
    assert engine.sender.sent==['旧回答'] and not engine.provider.calls
    assert engine.policy.capacity(group)['used_messages']==0


def test_at_never_bypasses_global_stop_or_disabled_group(setup):
    engine,store,_=setup
    engine.policy.quiet(engine.config.value.groups[0],'安静中')
    delivery,_=queued(engine,store)
    meta=store.chat_meta(delivery['id']);meta['direct_invitation']=True;store.update_chat_meta(delivery['id'],meta)
    assert engine.delivery_allowed(delivery)
    store.reserve_direct('control','target','a@chatroom','管理员操作',{})
    engine.intent_running=False
    assert not engine.delivery_allowed(store.delivery('control'))
    assert not engine.delivery_allowed(delivery)
    engine.intent_running=True;engine.config.value.groups[0].enabled=False
    assert not engine.delivery_allowed(delivery)
