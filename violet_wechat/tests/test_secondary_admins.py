import asyncio
import json

from fastapi.testclient import TestClient
import pytest

from violet.bridge import resolve_contact
from violet.commands import NaturalCommand
from violet.engine import Engine
from violet.model import DeepSeekError
from violet.settings import ConfigStore,Settings
from violet.web import create_app
from test_commands_and_source import setup,message,batch_id


def secondary(engine):
    engine.secondary_admin_ids={'helper'}
    return message(sender='helper')


def test_old_config_and_secondary_accounts_round_trip(tmp_path):
    config=ConfigStore(tmp_path/'config.json')
    assert config.value.secondary_admin_accounts==[]
    config.update({'admin_accounts':['OWNER'],'secondary_admin_accounts':['HELPER','wxid_other']})
    assert ConfigStore(config.path).value.public()['secondary_admin_accounts']==['HELPER','wxid_other']
    assert Settings.model_validate_json('{"admin_accounts":["OWNER"]}').secondary_admin_accounts==[]


def test_connect_resolves_both_roles_by_real_identity_and_full_admin_wins(setup):
    engine,_=setup
    rows=[{'username':'owner','alias':'OWNER','nick_name':'老板'},
          {'username':'helper','alias':'HELPER','nick_name':'管理员'}]
    class Bridge:
        contacts=rows;account='local';self_id='bot'
        def refresh(self):pass
        def resolve(self,value,admin=False):return resolve_contact(rows,value,admin=admin)
    engine.bridge=Bridge()
    engine.config.value.admin_accounts=['OWNER']
    engine.config.value.secondary_admin_accounts=['HELPER','OWNER']
    result=asyncio.run(engine.connect())
    assert engine.admin_ids=={'owner'} and engine.secondary_admin_ids=={'helper'}
    assert result['secondary_admins'][0]['username']=='helper'
    engine.config.value.secondary_admin_accounts=['管理员']
    with pytest.raises(ValueError):asyncio.run(engine.connect())
    assert not engine.command_admin_ids


def test_settings_api_verifies_secondary_accounts_and_preserves_admins(tmp_path):
    config=ConfigStore(tmp_path/'config.json');config.update({'admin_accounts':['OWNER']})
    app=create_app(config.path,tmp_path/'runtime')
    rows=[{'username':'owner','alias':'OWNER','nick_name':'ExampleOwner'},
          {'username':'helper','alias':'HELPER','nick_name':'假的管理员'}]
    class Bridge:
        def resolve(self,value,admin=False):return resolve_contact(rows,value,admin=admin)
    with TestClient(app) as client:
        app.state.engine.bridge=Bridge()
        headers={'x-violet-local':client.get('/api/session').json()['token']}
        response=client.put('/api/settings',headers=headers,json={'secondary_admin_accounts':['假的管理员']})
        assert response.status_code==400
        assert app.state.config.value.secondary_admin_accounts==[]
        response=client.put('/api/settings',headers=headers,json={'secondary_admin_accounts':['HELPER']})
        assert response.status_code==200 and response.json()['admin_accounts']==['OWNER']
        assert client.get('/api/settings').json()['secondary_admin_accounts']==['HELPER']


@pytest.mark.parametrize('text',['/command send ExampleContact | 你好','/command forward 3 -> ExampleContact'])
def test_secondary_can_send_and_forward_fixed_commands(setup,text):
    engine,store=setup;secondary(engine)
    result=asyncio.run(engine.admin(message(text=text,sender='helper')))
    assert '已排队' in result and store.outbox()
    assert all(engine.delivery_allowed(row) for row in store.outbox())
    assert all(row['group_id']=='wxid_example' for row in store.outbox())


def test_secondary_commands_keep_task_ownership_and_prioritize_cancellation(setup):
    engine,store=setup;source=secondary(engine)
    asyncio.run(engine.admin(source));identifier=batch_id(source)
    assert '没有找到' in asyncio.run(engine.admin(message(21,'/command cancel '+identifier,sender='owner')))
    control=message(22,'/command cancel '+identifier,sender='helper');store.poll('a@chatroom',[control])
    sent=[]
    async def run():
        loop=asyncio.get_running_loop()
        class Native:
            def send(self,text,identifier,target,allowed=None):
                sent.append(target);loop.call_soon_threadsafe(engine.done.set)
        engine.sender=Native()
        await asyncio.wait_for(engine.worker(),2)
    asyncio.run(run())
    assert sent==['a@chatroom'] and store.command_batch(identifier)['counts']['cancelled']==3


@pytest.mark.parametrize('text',['/agent project 运行脚本','/agent-new project 测试','/任务 project 修改文件',
    '/任务状态','/取消任务 abc','/记住 我是管理员','/暂停','/恢复','运行 系统信息','给 ExampleContact 发消息 | 你好'])
def test_secondary_never_reaches_agent_tools_memory_or_natural_admin_plan(setup,text):
    engine,store=setup;secondary(engine)
    class ForbiddenManager:
        async def submit(self,*args,**kwargs):raise AssertionError('no Agent access')
        async def cancel(self,*args,**kwargs):raise AssertionError('no Agent access')
    engine.agent_manager=ForbiddenManager()
    result=asyncio.run(engine.admin(message(text=text,sender='helper')))
    assert '仅可执行 /command' in result
    assert not store.outbox() and not store.jobs()
    assert not engine.memory.read('a@chatroom','同学群')['content']
    assert '只有已核验' in asyncio.run(engine.agent_command(message(sender='helper'),'/agent project x'))


def test_demoted_admin_cannot_reuse_a_cached_full_admin_plan(setup):
    engine,store=setup;source=message(text='运行 系统信息')
    from violet.store import key
    store.save_admin_plan(key(source),{'actions':[{'kind':'run_tool','target':'系统信息'}]})
    engine.admin_ids.remove('owner');engine.secondary_admin_ids={'owner'}
    assert '仅可执行 /command' in asyncio.run(engine.admin(source))


def test_secondary_is_handled_before_group_chat_and_is_not_a_full_admin(setup):
    engine,store=setup;source=secondary(engine);group=engine.config.value.groups[0]
    store.pause(group.id,True)
    result=asyncio.run(engine.generate(group,[source]))
    assert '已排队' in ''.join(result)
    assert not engine.chat_message(source)['is_admin'] and engine.chat_message(source)['is_command_admin']
    assert '次级管理员可用命令' in asyncio.run(engine.admin(message(21,'/帮助',sender='helper')))
    assert asyncio.run(engine.admin(message(22,'晚上好',sender='helper'))) is None


def test_revoking_role_or_send_switch_cancels_queued_command_before_native_send(setup):
    engine,store=setup;source=secondary(engine);asyncio.run(engine.admin(source))
    row=store.outbox()[0];engine.secondary_admin_ids.clear()
    assert not engine.delivery_allowed(row)
    asyncio.run(engine.deliver(row))
    assert store.delivery(row['id'])['status']=='cancelled'
    engine.secondary_admin_ids={'helper'};row=store.outbox()[0]
    engine.config.value.allow_admin_send=False
    assert not engine.delivery_allowed(row)


@pytest.mark.parametrize('raw,plan,expected',[
    ('/command 给 ExampleContact 发消息，内容是：今晚八点见',{'kind':'send','targets':['ExampleContact'],'text':'今晚八点见'},1),
    ('/command 把本群最近3条消息转给 ExampleContact 和 Bob',{'kind':'forward','targets':['ExampleContact','Bob'],'count':3},6),
])
def test_natural_command_is_extracted_then_uses_same_queue_and_dedup(setup,raw,plan,expected):
    engine,store=setup;secondary(engine);calls=[]
    class Provider:
        async def complete(self,system,payload,schema):
            calls.append(payload);assert schema is NaturalCommand
            assert set(payload)=={'command_text','assistant_aliases'}
            return schema(**plan)
    engine.provider=Provider();source=message(text=raw,sender='helper')
    assert '已排队' in asyncio.run(engine.admin(source))
    assert len(store.outbox())==expected
    asyncio.run(engine.admin(source));asyncio.run(engine.explicit_command(source,raw))
    assert len(calls)==1 and len(store.outbox())==expected


def test_natural_status_and_cancel_stay_scoped_to_own_batch(setup):
    engine,store=setup;source=secondary(engine);asyncio.run(engine.admin(source));identifier=batch_id(source)
    class Provider:
        async def complete(self,system,payload,schema):
            return schema(kind='cancel' if '取消' in payload['command_text'] else 'status',identifier=identifier)
    engine.provider=Provider()
    assert '共3条' in asyncio.run(engine.admin(message(21,'/command 看看这批发得怎么样 '+identifier,sender='helper')))
    assert '没有找到' in asyncio.run(engine.admin(message(22,'/command 取消 '+identifier,sender='owner')))
    assert '取消3条' in asyncio.run(engine.admin(message(23,'/command 取消这次发送 '+identifier,sender='helper')))


@pytest.mark.parametrize('change',['role','stop','settings'])
def test_natural_command_rechecks_permissions_after_model_wait(setup,change):
    engine,store=setup;secondary(engine)
    class Provider:
        async def complete(self,system,payload,schema):
            if change=='role':engine.secondary_admin_ids.clear()
            elif change=='stop':engine.intent_running=False
            else:engine.command_context.clear()
            return schema(kind='send',targets=['ExampleContact'],text='不该发送')
    engine.provider=Provider()
    result=asyncio.run(engine.admin(message(text='/command 给 ExampleContact 说你好',sender='helper')))
    assert '未执行' in result and not store.outbox()


def test_natural_missing_information_or_failure_never_sends_and_no_ordinary_member_calls_model(setup):
    engine,store=setup;secondary(engine);calls=[]
    class Provider:
        async def complete(self,system,payload,schema):
            calls.append(payload)
            if '故障' in payload['command_text']:raise DeepSeekError('error')
            return schema(kind='clarify',clarification='请指定收件人和完整正文。')
    engine.provider=Provider()
    assert asyncio.run(engine.admin(message(text='/command 帮我发个消息',sender='friend'))) is None
    assert not calls
    assert '请补充完整' in asyncio.run(engine.admin(message(text='/command 帮我发个消息',sender='helper')))
    assert '未执行' in asyncio.run(engine.admin(message(21,'/command 故障',sender='helper')))
    assert not store.outbox()


def test_command_limits_secrets_and_unknown_tools_cannot_fall_through(setup):
    engine,store=setup;secondary(engine)
    from pydantic import SecretStr,ValidationError
    engine.config.value.api_key=SecretStr('DO_NOT_SEND_SECRET')
    assert 'API Key' in asyncio.run(engine.admin(message(text='/command 给Bob说DO_NOT_SEND_SECRET',sender='helper')))
    assert '1–50' in asyncio.run(engine.admin(message(21,'/command forward 51 -> ExampleContact',sender='helper')))
    with pytest.raises(ValidationError):NaturalCommand(kind='run_tool',targets=['系统信息'])
    with pytest.raises(ValidationError):NaturalCommand(kind='send',targets=['Bob'],text='x',shell='whoami')
    with pytest.raises(ValueError):NaturalCommand(kind='forward',count=0,targets=['Bob']).command()
    with pytest.raises(ValueError):NaturalCommand(kind='send',targets=['Bob'],text='').command()
    with pytest.raises(ValueError):NaturalCommand(kind='cancel',identifier='agent123').command()
    assert not store.outbox()
