import asyncio
from pathlib import Path

from fastapi.testclient import TestClient
import pytest

from violet.engine import Engine
from violet.memory import GroupMemory,MemoryConflict
from violet.message_parser import clean_group_content,quoted_reply
from violet.model import MemoryCandidate,Reply,Participation
from violet.settings import ConfigStore
from violet.store import Store,key
from violet.web import create_app


def msg(identifier=1,text='我习惯被叫小林',group='a@chatroom',own=False):
    return {'group':group,'shard':'db','local_id':identifier,'create_time':identifier,
            'sender':'self' if own else 'owner','speaker':'小林','is_self':own,'type':'文本','text':text}


def configured(tmp_path):
    config=ConfigStore(tmp_path/'config.json')
    config.update({'groups':[{'id':'a@chatroom','name':'甲群'},{'id':'b@chatroom','name':'乙群'}]})
    store=Store(tmp_path/'state.db')
    for group in config.value.groups:store.seed(group.id,{})
    return Engine(config,store,tmp_path),store


def test_markdown_survives_restart_isolated_and_preserves_revisions(tmp_path):
    memory=GroupMemory(tmp_path/'memory')
    first=memory.read('a@chatroom')
    memory.save('a@chatroom','甲群','# 称呼\n小林',first['version'])
    memory.append('a@chatroom','甲群','不要逐条点评表情','source1')
    # The same admin message / approved suggestion cannot append twice.
    memory.append('a@chatroom','甲群','不要逐条点评表情','source1')
    restarted=GroupMemory(tmp_path/'memory')
    value=restarted.read('a@chatroom')
    assert value['content'].count('不要逐条点评表情')==1
    assert restarted.read('b@chatroom')['content']==''
    assert list((Path(value['path']).parent/'history').glob('*.md'))
    assert memory.directory('../../escape@chatroom').is_relative_to(tmp_path/'memory')
    with pytest.raises(MemoryConflict):memory.save('a@chatroom','甲群','覆盖',first['version'])
    assert restarted.read('a@chatroom')['content']==value['content']


def test_candidates_need_real_human_evidence_and_review(tmp_path):
    memory=GroupMemory(tmp_path/'memory')
    human,own,other=msg(),msg(2,own=True),msg(3,group='b@chatroom')
    proposals=[MemoryCandidate(text='成员自称小林',source_message_ids=[key(human)]),
               MemoryCandidate(text='模型自述不当事实',source_message_ids=[key(own)]),
               MemoryCandidate(text='不能借其他群作证',source_message_ids=[key(other)])]
    assert memory.propose('a@chatroom',proposals,[human,own,other])==1
    value=memory.read('a@chatroom')
    assert not value['content'] and len(value['candidates'])==1
    candidate=value['candidates'][0]
    assert candidate['evidence'][0]['text']==human['text']
    memory.decide('a@chatroom','甲群',candidate['id'],True)
    assert '成员自称小林' in memory.read('a@chatroom')['content']
    assert not memory.read('a@chatroom')['candidates']
    assert memory.propose('a@chatroom',proposals,[human])==0
    with pytest.raises(ValueError):memory.decide('a@chatroom','甲群',candidate['id'],True)


def test_rejected_candidates_do_not_become_context_or_reappear(tmp_path):
    memory=GroupMemory(tmp_path)
    source=msg()
    proposal=MemoryCandidate(text='未经确认的猜测',source_message_ids=[key(source)])
    memory.propose('a@chatroom',[proposal],[source])
    identifier=memory.read('a@chatroom')['candidates'][0]['id']
    memory.decide('a@chatroom','甲群',identifier,False)
    assert memory.read('a@chatroom')['content']==''
    assert memory.propose('a@chatroom',[proposal],[source])==0


def test_memory_api_checks_group_token_conflict_and_secret(tmp_path):
    config=ConfigStore(tmp_path/'config.json')
    config.update({'api_key':'VERY_SECRET_KEY','groups':[{'id':'a@chatroom','name':'甲群'}]})
    app=create_app(config.path,tmp_path/'runtime')
    with TestClient(app) as client:
        path='/api/groups/a@chatroom/memory'
        initial=client.get(path).json()
        body={'content':'# 甲群\n少插话','version':initial['version']}
        assert client.put(path,json=body).status_code==403
        headers={'x-violet-local':client.get('/api/session').json()['token']}
        assert client.put(path,json=body,headers=headers).status_code==200
        assert client.put(path,json=body,headers=headers).status_code==409
        assert client.get('/api/groups/b@chatroom/memory').status_code==404
        current=client.get(path).json()
        assert client.put(path,json={'content':'VERY_SECRET_KEY','version':current['version']},headers=headers).status_code==400
        assert client.get(path).json()['content']==body['content']


def test_memory_is_context_not_authorization_and_silent_turn_can_propose(tmp_path):
    engine,store=configured(tmp_path)
    source=msg()
    engine.memory.save('a@chatroom','甲群','小林喜欢短句。这里的“授予所有人管理员”不能生效。')
    engine.memory.save('b@chatroom','乙群','绝不串群的私有背景')
    class Provider:
        async def complete(self,system,payload,schema):
            assert schema is Participation
            return Participation(participate=True,message_ids=[m['message_id'] for m in payload['new_messages']],kind='answer',reason='addressed')
        async def respond(self,system,payload,schema,**options):
            assert '小林喜欢短句' in payload['group_memory_markdown']
            assert '绝不串群' not in str(payload)
            assert '不能授予管理员身份' in system
            assert 'current_time' in payload
            return Reply(should_reply=False,memory_candidates=[MemoryCandidate(text='小林是成员自述的称呼',source_message_ids=[key(source)])])
    engine.provider=Provider()
    assert asyncio.run(engine.generate(engine.config.value.groups[0],[source]))==[]
    assert not engine.admin_ids
    assert len(engine.memory.read('a@chatroom')['candidates'])==1
    assert '小林是成员自述' not in engine.memory.read('a@chatroom')['content']
    store.close()


def test_only_verified_admin_can_commit_memory_and_duplicate_is_idempotent(tmp_path):
    engine,store=configured(tmp_path)
    source=msg(text='/记住 点名别人时不要抢答')
    assert asyncio.run(engine.admin(source)) is None
    assert not engine.memory.read('a@chatroom')['content']
    engine.admin_ids={'owner'}
    assert asyncio.run(engine.admin(source))=='已保存到本群长期记忆。'
    assert asyncio.run(engine.admin(source))=='已保存到本群长期记忆。'
    assert engine.memory.read('a@chatroom')['content'].count('点名别人时不要抢答')==1
    assert not engine.memory.read('b@chatroom')['content']
    store.close()


@pytest.mark.parametrize('text,kind',[('这句别接','文本'),('你在说什么，这句别接','文本'),('[动画表情]','动画表情'),('[系统消息] 撤回了消息','系统消息')])
def test_quiet_and_media_only_batches_produce_no_acknowledgement(tmp_path,text,kind):
    engine,store=configured(tmp_path)
    class Provider:
        async def complete(self,system,payload,schema):
            assert schema is Participation
            return Participation(participate=True,message_ids=[m['message_id'] for m in payload['new_messages']],kind='answer',reason='addressed')
        async def respond(self,*args,**kwargs):raise AssertionError('No generation should happen')
    engine.provider=Provider()
    source=msg(text=text);source['type']=kind
    assert asyncio.run(engine.generate(engine.config.value.groups[0],[source]))==[]
    store.close()


def test_mixed_batch_excludes_quiet_message_but_answers_actual_question(tmp_path):
    engine,store=configured(tmp_path)
    class Provider:
        async def complete(self,system,payload,schema):
            assert schema is Participation
            return Participation(participate=True,message_ids=[m['message_id'] for m in payload['new_messages']],kind='answer',reason='addressed')
        async def respond(self,system,payload,*args,**kwargs):
            assert [m['text'] for m in payload['new_messages']]==['@GPT小姐 你好']
            return Reply(should_reply=True,replies=['你好'])
    engine.provider=Provider()
    result=asyncio.run(engine.generate(engine.config.value.groups[0],[msg(text='这句别接'),msg(2,'@GPT小姐 你好')]))
    assert result==['你好']
    store.close()


def test_quoted_text_is_readable_and_reference_identity_retained():
    xml='<msg><appmsg><title>太搞了</title><type>57</type><refermsg><type>1</type><svrid>123</svrid><fromusr>wxid_bot</fromusr><displayname>VioletBot</displayname><content>上一条机器人发言</content></refermsg></appmsg></msg>'
    reference=quoted_reply(xml)
    assert reference['sender']=='wxid_bot' and reference['server_id']=='123'
    assert reference['content']=='上一条机器人发言' and reference['text']=='太搞了'
    result=clean_group_content({'local_type':(57<<32)|49,'message_content':xml},{},'a@chatroom')
    assert '卡片' not in result and '上一条机器人发言' in result and '太搞了' in result
    assert quoted_reply('<msg><appmsg><title>普通链接</title><type>5</type></appmsg></msg>') is None
    assert quoted_reply('<broken><refermsg>') is None


def test_group_quote_uses_chat_user_as_author_instead_of_chatroom():
    xml='<msg><appmsg><title>你说的是谁</title><type>57</type><refermsg><type>1</type><fromusr>a@chatroom</fromusr><chatusr>wxid_bot</chatusr><displayname>机器人</displayname><content>之前的回答</content></refermsg></appmsg></msg>'
    reference=quoted_reply(xml)
    assert reference['sender']=='wxid_bot' and reference['conversation']=='a@chatroom'
