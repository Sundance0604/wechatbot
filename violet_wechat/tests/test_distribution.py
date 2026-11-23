import json
import os
from pathlib import Path
import subprocess
import sys

from violet.diagnostics import self_test
from violet.settings import ROOT


def test_new_users_start_with_generic_identity_and_no_personal_background(tmp_path):
    from violet.profile import OwnerProfile
    from violet.settings import ConfigStore, Settings
    fresh=ConfigStore(tmp_path/'config.local.json').value
    example=Settings.model_validate_json((ROOT/'config.example.json').read_text(encoding='utf-8'))
    for settings in [fresh,example]:
        assert settings.bot_name=='VioletBot' and settings.mentions==['VioletBot']
        assert settings.persona==''
        assert settings.self_id==settings.wechat_account==settings.wechat_data_dir==''
        assert settings.groups==settings.workspaces==settings.admin_accounts==settings.secondary_admin_accounts==[]
        assert not settings.api_key.get_secret_value()
        assert not settings.agent.codex_path and not settings.agent.deepseek_path
    assert OwnerProfile(tmp_path/'runtime').read()['content']==''


def test_offline_components_include_every_supported_native_profile():
    result=self_test()
    assert result['status']=='passed',result
    assert set(result['checks']['native_profiles']['value'])=={'4.2.0.11','4.2.0.13','4.2.0.52','4.2.0.53'}
    assert result['checks']['database_reader']['ok']


def test_cli_selftest_writes_isolated_report_without_private_configuration(tmp_path):
    environment={**os.environ,'PYTHONPATH':str(ROOT)}
    result=subprocess.run([sys.executable,'-m','violet','--self-test','--data-dir',str(tmp_path)],
                          env=environment,capture_output=True,text=True,timeout=20)
    assert result.returncode==0,result.stderr
    report=json.loads((tmp_path/'runtime/selftest.json').read_text(encoding='utf-8'))
    assert report['status']=='passed'
    assert not (tmp_path/'config.local.json').exists()
    assert not (tmp_path/'runtime/state.sqlite3').exists()


def test_quit_command_uses_the_selected_data_directory(tmp_path):
    environment={**os.environ,'PYTHONPATH':str(ROOT)}
    result=subprocess.run([sys.executable,'-m','violet','--quit','--data-dir',str(tmp_path)],
                          env=environment,capture_output=True,text=True,timeout=10)
    assert result.returncode==0
    assert all((tmp_path/'runtime'/name).exists() for name in ['QUIT','STOP','AGENT_STOP'])
