import json
import os
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, SecretStr

ROOT = Path(__file__).resolve().parent.parent
HOME = (Path(os.environ['VIOLET_HOME']).expanduser().resolve() if os.environ.get('VIOLET_HOME') else
        ROOT if (ROOT/'config.local.json').exists() else
        Path(os.environ.get('LOCALAPPDATA',str(Path.home()/'.local/share')))/'Violet')
RUNTIME = HOME/'runtime'
CONFIG = HOME/'config.local.json'
PERSONA = ('你是群里的GPT小姐，一个自然、有主见的AI聊天搭子。大家知道你是AI。'
           '接住大家刚说的话，可以幽默、接梗，别用客服腔。不要冒充微信账号持有人，不编造亲身经历。'
           '话少一点，不逐条讲评，不猜别人的心理，不反复自我介绍。明确向你提出的多个问题可以分别简短回答。')
FAMILY_PERSONA = ('这是账号主人的家族群。亲切、自然、简洁，称呼依照本群明确提供的信息，不猜辈分或私人关系。'
                  '尊重长辈，不使用同学群互损、阴阳怪气或国/匪黑话；不替主人作私人承诺。')


def group_persona(settings,group):
    return group.persona or (FAMILY_PERSONA if group.chat_style=='family' else settings.persona)


class Strict(BaseModel):
    model_config = ConfigDict(extra='forbid')


class Group(Strict):
    id: str = Field(pattern=r'^\S{1,90}@chatroom$')
    name: str = Field(min_length=1,max_length=150)
    enabled: bool = True
    mode: Literal['active','mention'] = 'active'
    chat_style: Literal['balanced','classmates','family'] = 'balanced'
    bot_name: str = Field(default='',max_length=30)
    mentions: list[str] = Field(default_factory=list,max_length=10)
    agent_context_messages: int = Field(default=50,ge=0,le=200)
    persona: str = Field(default='',max_length=6000)
    cooldown_seconds: float = Field(default=45,ge=0,le=3600)
    budget_window_seconds: int = Field(default=300,ge=30,le=3600)
    message_budget: int = Field(default=4,ge=1,le=30)
    character_budget: int = Field(default=600,ge=50,le=10000)
    quiet_seconds: int = Field(default=120,ge=10,le=3600)
    casual_max_chars: int = Field(default=80,ge=10,le=300)
    other_names: list[str] = Field(default_factory=list,max_length=100)


class Workspace(Strict):
    id: str = Field(pattern=r'^[a-zA-Z0-9_-]{1,48}$')
    name: str = Field(min_length=1,max_length=80)
    path: str = Field(min_length=1,max_length=800)
    enabled: bool = True
    access: Literal['read-only','workspace-write'] = 'workspace-write'
    allow_commands: bool = True
    allow_network: bool = False
    allow_send_messages: bool = True


class AgentSettings(Strict):
    enabled: bool = True
    codex_path: str = Field(default='',max_length=800)
    model: str = Field(default='',max_length=100)
    timeout_seconds: int = Field(default=900,ge=30,le=7200)
    max_concurrency: int = Field(default=2,ge=1,le=4)


class Settings(Strict):
    api_key: SecretStr = SecretStr('')
    model: str = Field(default='deepseek-flash',min_length=1,max_length=100)
    search_model: str = Field(default='deepseek-v4-pro',min_length=1,max_length=100)
    poll_seconds: float = Field(default=10,ge=2,le=3600)
    max_replies: int = Field(default=3,ge=1,le=5)
    reply_gap_seconds: float = Field(default=1.5,ge=0.5,le=15)
    context_messages: int = Field(default=30,ge=5,le=100)
    batch_messages: int = Field(default=30,ge=1,le=100)
    request_timeout: int = Field(default=90,ge=10,le=300)
    temperature: float = Field(default=0.8,ge=0,le=2)
    bot_name: str = Field(default='GPT小姐',min_length=1,max_length=30)
    mentions: list[str] = Field(default_factory=lambda:['VioletBot','GPT小姐'],max_length=10)
    persona: str = Field(default=PERSONA,max_length=8000)
    admin_accounts: list[str] = Field(default_factory=list,max_length=30)
    secondary_admin_accounts: list[str] = Field(default_factory=list,max_length=30)
    allow_admin_send: bool = True
    allow_local_tools: bool = True
    allow_web_search: bool = True
    groups: list[Group] = Field(default_factory=list,max_length=30)
    wechat_account: str = Field(default='',max_length=100)
    wechat_data_dir: str = Field(default='',max_length=600)
    self_id: str = Field(default='',max_length=100)
    agent: AgentSettings = Field(default_factory=AgentSettings)
    workspaces: list[Workspace] = Field(default_factory=list,max_length=30)

    def public(self):
        value = self.model_dump(mode='json',exclude={'api_key'})
        value['api_key_configured'] = bool(self.api_key.get_secret_value())
        return value

    def validate_workspaces(self):
        if len({w.id for w in self.workspaces})!=len(self.workspaces) or len({w.name.casefold() for w in self.workspaces})!=len(self.workspaces):
            raise ValueError('工作区标识和名称不能重复。')
        for workspace in self.workspaces:
            path = Path(workspace.path).expanduser()
            if not path.is_absolute() or not path.is_dir():
                raise ValueError('工作区必须选择已存在的绝对目录。')
            workspace.path = str(path.resolve())
        return self


class ConfigStore:
    def __init__(self,path=CONFIG):
        self.path = Path(path)
        self.value = Settings.model_validate_json(self.path.read_text(encoding='utf-8')) if self.path.exists() else Settings()
        key = os.environ.get('VIOLET_DEEPSEEK_API_KEY')
        if key:
            self.value.api_key = SecretStr(key)

    def save(self,value):
        self.path.parent.mkdir(parents=True,exist_ok=True)
        data = value.model_dump(mode='json')
        data['api_key'] = value.api_key.get_secret_value()
        temporary = self.path.with_suffix('.tmp')
        temporary.write_text(json.dumps(data,ensure_ascii=False,indent=2),encoding='utf-8')
        os.replace(temporary,self.path)
        self.value = value

    def update(self,patch):
        patch = dict(patch)
        patch.pop('api_key_configured',None)
        data = self.value.model_dump()
        if patch.get('api_key') == '':
            patch.pop('api_key')
        data.update(patch)
        value = Settings.model_validate(data)
        value.validate_workspaces()
        if len({g.id for g in value.groups}) != len(value.groups):
            raise ValueError('同一个群不能重复添加。')
        self.save(value)
        return value
