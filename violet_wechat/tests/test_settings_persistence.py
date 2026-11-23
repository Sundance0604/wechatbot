from fastapi.testclient import TestClient

from violet.settings import ConfigStore
from violet.web import create_app


def test_partial_agent_updates_preserve_enabled_state_and_program_path(tmp_path):
    config=ConfigStore(tmp_path/'config.json')
    config.update({'agent':{'deepseek_enabled':True,'deepseek_path':'D:/apps/harness.exe'}})
    config.update({'agent':{'timeout_seconds':1200}})
    saved=ConfigStore(config.path).value
    assert saved.agent.deepseek_enabled and saved.agent.deepseek_path=='D:/apps/harness.exe'
    assert saved.agent.timeout_seconds==1200


def test_stale_panel_cannot_overwrite_newer_agent_configuration(tmp_path):
    path=tmp_path/'config.json';app=create_app(path,tmp_path/'runtime')
    with TestClient(app) as client:
        headers={'x-violet-local':client.get('/api/session').json()['token']}
        stale=client.get('/api/settings').json()
        enabled=client.put('/api/settings',headers=headers,json={'agent':{
            'deepseek_enabled':True,'deepseek_path':'D:/apps/harness.exe'}})
        assert enabled.status_code==200
        stale['workspaces']=[{'id':'new','name':'新增项目','path':str(tmp_path)}]
        result=client.put('/api/settings',headers=headers,json=stale)
        assert result.status_code==409
        current=client.get('/api/settings').json()
        assert current['agent']['deepseek_enabled']
        assert current['agent']['deepseek_path']=='D:/apps/harness.exe'
        assert current['workspaces']==[]
        assert current['settings_revision']==enabled.json()['settings_revision']
        # Refreshing the page and adding a project preserves the global engine.
        current['workspaces']=stale['workspaces']
        result=client.put('/api/settings',headers=headers,json=current)
        assert result.status_code==200
        assert result.json()['agent']['deepseek_enabled']
        assert result.json()['workspaces'][0]['id']=='new'
        # A partial API update must not reset other fields to schema defaults.
        result=client.put('/api/settings',headers=headers,json={'agent':{'timeout_seconds':1200}})
        assert result.status_code==200
        assert result.json()['agent']['deepseek_path']=='D:/apps/harness.exe'
    persisted=ConfigStore(path).value
    assert persisted.agent.deepseek_enabled and persisted.agent.timeout_seconds==1200
    assert persisted.workspaces[0].id=='new'


def test_old_unversioned_full_page_is_refused_but_scoped_updates_work(tmp_path):
    app=create_app(tmp_path/'config.json',tmp_path/'runtime')
    with TestClient(app) as client:
        headers={'x-violet-local':client.get('/api/session').json()['token']}
        legacy=client.get('/api/settings').json();legacy.pop('settings_revision')
        assert client.put('/api/settings',headers=headers,json=legacy).status_code==409
        assert client.put('/api/settings',headers=headers,json={'agent':{'deepseek_enabled':True}}).status_code==200


def test_monitor_toggle_rejects_stale_revision_and_returns_current_revision(tmp_path):
    config=ConfigStore(tmp_path/'config.json')
    config.update({'groups':[{'id':'demo@chatroom','name':'演练群'}]})
    app=create_app(config.path,tmp_path/'runtime')
    with TestClient(app) as client:
        headers={'x-violet-local':client.get('/api/session').json()['token']}
        stale=client.get('/api/settings').json()['settings_revision']
        client.put('/api/settings',headers=headers,json={'agent':{'deepseek_enabled':True}}).raise_for_status()
        path='/api/groups/demo@chatroom/enabled'
        assert client.post(path,headers=headers,json={'enabled':False,'settings_revision':stale}).status_code==409
        current=client.get('/api/settings').json()['settings_revision']
        result=client.post(path,headers=headers,json={'enabled':False,'settings_revision':current})
        assert result.status_code==200
        assert result.json()['settings']['settings_revision']!=current
        assert result.json()['settings']['agent']['deepseek_enabled']
