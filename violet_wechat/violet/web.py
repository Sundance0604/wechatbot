import asyncio
from contextlib import asynccontextmanager
import secrets

from fastapi import FastAPI,HTTPException,Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse,JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel,Field,ValidationError
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .engine import Engine
from .model import DeepSeek,DeepSeekError,Reply
from .settings import ConfigStore,RUNTIME,ROOT,Settings
from .store import Store
from .agent import AgentManager
from .memory import MemoryConflict


class ChatRequest(BaseModel):
    messages: list[dict[str,str]] = Field(min_length=1,max_length=30)


class AgentRequest(BaseModel):
    model_config={'extra':'forbid'}
    workspace: str = Field(min_length=1,max_length=80)
    prompt: str = Field(min_length=1,max_length=20000)
    new_session: bool = False
    parent: str | None = None


class MemoryRequest(BaseModel):
    model_config={'extra':'forbid'}
    content: str = Field(max_length=16000)
    version: str = Field(pattern=r'^[a-f0-9]{64}$')


class MemoryDecision(BaseModel):
    model_config={'extra':'forbid'}
    accept: bool = Field(strict=True)


class ProfileRequest(BaseModel):
    model_config={'extra':'forbid'}
    content: str = Field(max_length=20000)
    version: str = Field(pattern=r'^[a-f0-9]{64}$')


def create_app(config_path=None,runtime=None,bridge_factory=None):
    runtime = runtime or RUNTIME
    runtime.mkdir(parents=True,exist_ok=True)
    config = ConfigStore(config_path) if config_path else ConfigStore()
    store = Store(runtime/'state.sqlite3')
    options = {'bridge_factory':bridge_factory} if bridge_factory else {}
    engine = Engine(config,store,runtime,**options)
    agents=AgentManager(config,store,runtime,send_tool=engine.agent_send_message,
                        find_tool=engine.agent_find_contacts,on_done=engine.agent_finished,on_cancel=engine.agent_cancelled)
    engine.agent_manager=agents
    token = secrets.token_urlsafe(32)

    @asynccontextmanager
    async def lifespan(app):
        yield
        await engine.stop()
        await agents.close()
        store.close()

    app = FastAPI(title='Violet · 微信 AI 工作台',docs_url=None,redoc_url=None,openapi_url=None,lifespan=lifespan)
    app.state.engine,app.state.config,app.state.store,app.state.agents = engine,config,store,agents
    app.add_middleware(TrustedHostMiddleware,allowed_hosts=['127.0.0.1','localhost','testserver'])

    @app.middleware('http')
    async def local_only(request,call_next):
        if request.method not in ['GET','HEAD','OPTIONS']:
            origin = request.headers.get('origin')
            expected = str(request.base_url).rstrip('/')
            if (origin and origin!=expected) or request.headers.get('x-violet-local')!=token:
                return JSONResponse({'detail':'请从本机 Violet 控制面板发起操作。'},status_code=403)
        try:
            response = await call_next(request)
        except (ValueError,DeepSeekError) as exc:
            return JSONResponse({'detail':str(exc)},status_code=400)
        except Exception as exc:
            store.event('error','操作失败：'+type(exc).__name__)
            return JSONResponse({'detail':'操作未完成，请检查微信连接或本机配置。'},status_code=500)
        response.headers['Cache-Control'] = 'no-store'
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['Content-Security-Policy'] = "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'"
        return response

    @app.exception_handler(RequestValidationError)
    async def invalid_request(request,exc):
        return JSONResponse({'detail':'输入格式有误，请检查字段和数值范围。'},status_code=422)

    @app.get('/api/session')
    async def session():
        return {'token':token}

    @app.get('/api/settings')
    async def settings():
        return config.value.public()

    @app.get('/api/profile')
    async def owner_profile():
        return engine.profile.read()

    @app.put('/api/profile')
    async def save_profile(payload:ProfileRequest):
        secret=config.value.api_key.get_secret_value()
        if secret and secret in payload.content:raise HTTPException(400,'不要将 API Key 写入主人资料。')
        try:value=engine.profile.save(payload.content,payload.version)
        except MemoryConflict as exc:raise HTTPException(409,str(exc)) from None
        for group in config.value.groups:
            await engine.invalidate_chat_context(group.id,'主人资料已更新，取消旧资料下的闲聊回复')
        store.event('profile_updated','主人资料已在本机更新')
        return value

    @app.put('/api/settings')
    async def save_settings(request:Request):
        patch = await request.json()
        raw = config.value.model_dump()
        patch.pop('api_key_configured',None)
        if patch.get('api_key')=='':
            patch.pop('api_key')
        raw.update(patch)
        try:
            candidate = Settings.model_validate(raw)
            candidate.validate_workspaces()
            if len({g.id for g in candidate.groups})!=len(candidate.groups):
                raise ValueError('群聊重复')
        except (ValidationError,ValueError):
            raise HTTPException(400,'设置有误，请检查频率、条数和群聊配置。') from None
        if engine.bridge and (candidate.wechat_account,candidate.wechat_data_dir)==(config.value.wechat_account,config.value.wechat_data_dir):
            for value in candidate.admin_accounts+candidate.secondary_admin_accounts:
                engine.bridge.resolve(value,admin=True)
        resume = engine.running
        revision = engine.command_revision
        if (candidate.workspaces,candidate.agent,candidate.admin_accounts)!=(config.value.workspaces,config.value.agent,config.value.admin_accounts):
            await agents.cancel_all('工作区或权限设置已更改')
        await engine.stop(user_requested=False)
        account_changed = (candidate.wechat_account,candidate.wechat_data_dir,candidate.self_id)!=(config.value.wechat_account,config.value.wechat_data_dir,config.value.self_id)
        config.save(candidate)
        if account_changed:
            engine.bridge = None
        store.event('settings','设置已保存'+('并应用到监听' if resume else ''))
        if resume and revision==engine.command_revision:
            await engine.start(user_requested=False)
        return candidate.public()

    @app.get('/api/status')
    async def status():
        return engine.status()

    @app.post('/api/connect')
    async def connect():
        try:
            return await engine.connect()
        except Exception as exc:
            if isinstance(exc,ValueError):
                raise
            raise HTTPException(400,'微信读取连接失败，请确认已登录、账号设置正确。') from None

    @app.post('/api/native/recover')
    async def recover():
        from .recovery import recover
        resume = engine.running
        revision = engine.command_revision
        await engine.stop(user_requested=False)
        try:
            result = await recover(runtime)
        except RuntimeError as exc:
            raise HTTPException(400,str(exc)) from None
        if resume and revision==engine.command_revision:
            await engine.start(user_requested=False)
        return result

    @app.get('/api/contacts')
    async def contacts(q:str='',kind:str='group'):
        if engine.bridge is None:
            await engine.connect()
        rows = engine.bridge.contacts
        if kind=='group':
            rows = [r for r in rows if r['username'].endswith('@chatroom')]
        elif kind=='person':
            rows = [r for r in rows if not r['username'].endswith('@chatroom')]
        if q.strip():
            rows = [r for r in rows if q.casefold() in ' '.join(str(v or '') for v in r.values()).casefold()]
        return rows[:200]

    @app.post('/api/monitor/start')
    async def start():
        await engine.start()
        return engine.status()

    @app.post('/api/monitor/stop')
    async def stop():
        await engine.stop()
        return engine.status()

    @app.post('/api/groups/{identifier}/enabled')
    async def enable_group(identifier:str,request:Request):
        body = await request.json()
        if not isinstance(body.get('enabled'),bool):
            raise HTTPException(400,'请提供明确的开启或关闭状态。')
        await engine.enable_group(identifier,body['enabled'])
        return {'settings':config.value.public(),'status':engine.status()}

    @app.post('/api/groups/{identifier}/quiet')
    async def quiet_group(identifier:str,request:Request):
        body=await request.json()
        if not isinstance(body.get('quiet'),bool):raise HTTPException(400,'请明确选择安静或恢复。')
        await engine.quiet_group(identifier,body['quiet'])
        return engine.status()

    def memory_group(identifier):
        group = next((g for g in config.value.groups if g.id==identifier),None)
        if group is None:
            raise HTTPException(404,'该群不在当前群聊配置中。')
        return group

    @app.get('/api/groups/{identifier}/memory')
    async def group_memory(identifier:str):
        group = memory_group(identifier)
        return engine.memory.read(identifier,group.name)

    @app.put('/api/groups/{identifier}/memory')
    async def save_group_memory(identifier:str,payload:MemoryRequest):
        group = memory_group(identifier)
        secret = config.value.api_key.get_secret_value()
        if secret and secret in payload.content:
            raise HTTPException(400,'不要将 API Key 写入群聊记忆。')
        try:
            value = engine.memory.save(identifier,group.name,payload.content,payload.version)
        except MemoryConflict as exc:
            raise HTTPException(409,str(exc)) from None
        await engine.invalidate_chat_context(identifier,'本群长期记忆已更新，取消旧背景下的闲聊回复')
        store.event('memory_updated','本群长期记忆已更新',identifier)
        return value

    @app.post('/api/groups/{identifier}/memory/candidates/{candidate}')
    async def decide_memory(identifier:str,candidate:str,payload:MemoryDecision):
        group = memory_group(identifier)
        secret = config.value.api_key.get_secret_value()
        entry = next((v for v in engine.memory.read(identifier)['candidates'] if v['id']==candidate),None)
        if payload.accept and entry and secret and secret in entry['text']:
            raise HTTPException(400,'不要将 API Key 写入群聊记忆。')
        value = engine.memory.decide(identifier,group.name,candidate,payload.accept)
        if payload.accept:await engine.invalidate_chat_context(identifier,'本群确认了新记忆，取消旧背景下的闲聊回复')
        store.event('memory_updated','已确认一条长期记忆' if payload.accept else '已忽略一条记忆建议',identifier)
        return value

    @app.post('/api/model/test')
    async def model_test():
        provider = DeepSeek(config.value)
        try:
            result = await provider.complete('连接测试。仅返回JSON {"should_reply":true,"replies":["连接成功"]}。',{'test':True},Reply)
            return {'ok':True,'message':result.replies[0],'model':config.value.model}
        finally:
            await provider.close()

    @app.get('/api/agent/jobs')
    async def jobs():
        return agents.status()

    @app.post('/api/agent/jobs')
    async def create_job(payload:AgentRequest):
        return await agents.submit(payload.workspace,payload.prompt,new_session=payload.new_session,parent=payload.parent)

    @app.get('/api/agent/jobs/{identifier}')
    async def job_detail(identifier:str):
        job=store.job(identifier)
        if not job:raise HTTPException(404,'任务不存在。')
        return {'job':job,'events':store.job_events(identifier),'approvals':store.approvals(identifier),'source_context':store.job_context(identifier)}

    @app.post('/api/agent/jobs/{identifier}/cancel')
    async def cancel_job(identifier:str):
        return await agents.cancel(identifier)

    @app.post('/api/agent/stop')
    async def stop_agents():
        (runtime/'AGENT_STOP').touch()
        await agents.cancel_all()
        return agents.status()

    @app.post('/api/agent/probe')
    async def probe_agent():
        return await agents.probe()

    @app.post('/api/agent/approvals/{identifier}')
    async def approve_agent(identifier:str,request:Request):
        value=await request.json()
        if not isinstance(value.get('accept'),bool) or not isinstance(value.get('text',''),str):
            raise HTTPException(400,'请提供明确的确认结果。')
        await agents.decide(identifier,value['accept'],value.get('text',''))
        return {'ok':True}

    @app.post('/api/chat')
    async def chat(payload:ChatRequest):
        if any(m.get('role') not in ['user','assistant'] or not m.get('content') or len(m['content'])>12000 for m in payload.messages):
            raise HTTPException(400,'请输入有效的聊天文字。')
        provider = DeepSeek(config.value)
        try:
            return {'reply':await provider.chat([{'role':m['role'],'content':m['content']} for m in payload.messages],engine.profile.read()['content'])}
        finally:
            await provider.close()

    @app.post('/api/search')
    async def search(request:Request):
        value = await request.json()
        query = value.get('query','')
        if not isinstance(query,str) or not 1<=len(query)<=1500:
            raise HTTPException(400,'请输入查询内容。')
        if not config.value.allow_web_search:
            raise HTTPException(400,'联网查询已关闭。')
        provider = DeepSeek(config.value)
        try:
            return {'reply':await provider.search(query)}
        finally:
            await provider.close()

    @app.post('/api/deliveries/{identifier}/check')
    async def check(identifier:str):
        if not engine.bridge:
            await engine.connect()
        delivery = next((r for r in store.outbox(('sending','uncertain')) if r['id']==identifier),None)
        if not delivery:
            raise HTTPException(404,'没有待核对的记录。')
        found = await asyncio.to_thread(engine.bridge.verify,delivery)
        if found:
            store.update_outgoing(identifier,'sent',found['server_id'])
        return {'confirmed':bool(found)}

    @app.post('/api/deliveries/{identifier}/dismiss')
    async def dismiss(identifier:str):
        if engine.running:
            raise HTTPException(400,'请先停止监听，再标记人工核对结果。')
        if not any(r['id']==identifier for r in store.outbox(('uncertain',))):
            raise HTTPException(404,'没有待处理的记录。')
        store.update_outgoing(identifier,'cancelled',error='用户已人工处理；不重发')
        store.event('resolved','已人工处理一条未确认记录')
        return {'ok':True}

    static = ROOT/'violet/static'
    app.mount('/assets',StaticFiles(directory=static),name='assets')

    @app.get('/')
    async def index():
        return FileResponse(static/'index.html')

    return app
