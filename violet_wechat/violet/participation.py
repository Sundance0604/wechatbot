"""Deterministic conversation gates, separate from generating any reply text."""
import re
import time
import unicodedata

from .store import key


LOCAL_SILENCE = re.compile(r'(?:这句|这条|这段|这轮)(?:话)?(?:先)?(?:别|不要|不用)(?:接|回|回复|回答)(?:了)?[，。!！?？\s]*$')
QUIET_REQUEST = re.compile(r'^(?:(?:你|机器人|请|麻烦|能不能|可以|先|现在|给我|就|别再|不要再|暂时|保持|闭上你的嘴)[，,\s]*)*'
    r'(?:闭嘴|不接(?:了)?|别接(?:了)?|不用接(?:了)?|不要接(?:了)?|不回(?:复)?(?:了)?|安静(?:一下|一会|一点|点|一会儿|几分钟)?|别说话|不要说话|别回(?:复)?(?:了|我)?|不要回复(?:了)?|停止回复|暂停闲聊|别插话|不要插话|别抢答|不要抢答|不用回复(?:了)?|少说(?:点|两句)?|别(?:再)?(?:叭叭|刷屏)|不要刷屏|别吵|太吵了)[了吧啊呀吗。！!？?，,\s]*$')
CORRECTION = re.compile(r'没(?:有)?(?:人)?(?:问|叫|找)你|不是(?:在)?(?:问|叫|找)你|让你别(?:接|回)|别(?:再)?抢答|不要(?:再)?抢答|你(?:又)?(?:回复|插嘴|抢答)什么|这话题跟你有关吗|这(?:句|条)不是(?:在)?(?:问|叫|找)你|你(?:的)?(?:话|废话)太多|怎么这么吵')


def textual(message):
    return not message.get('is_self') and message.get('type') in {'文本','引用回复'}


def explicitly_addressed(message, settings, self_id=''):
    text=message['text']
    names=set(settings.mentions+[settings.bot_name])
    if any(re.search(r'@'+re.escape(name)+r'(?=$|[\s，,：:。！!？?])',text,re.I) for name in names):
        return True
    if any(re.match(r'^'+re.escape(name)+r'[\s，,：:]',text.strip(),re.I) for name in names):return True
    reference=message.get('reply_to') or {}
    return bool(self_id and reference.get('sender')==self_id)


def addressed_elsewhere(message, settings, group, self_id='', names=(),include_quote=True):
    if explicitly_addressed(message,settings,self_id):return False
    reference=message.get('reply_to') or {}
    if include_quote and self_id and reference.get('sender') and reference['sender']!=self_id:return True
    text=unicodedata.normalize('NFKC',message['text']).strip()
    if re.match(r'^@[^\s，,：:]+[\s，,：:]',text):return True
    handles=set(group.other_names)|{n for n in names if isinstance(n,str) and 2<=len(n)<=30}
    excluded={n.casefold() for n in [*settings.mentions,settings.bot_name,'群友','成员','微信用户']}
    handles={n for n in handles if n.casefold() not in excluded}
    return any(re.match(r'^'+re.escape(unicodedata.normalize('NFKC',name))+r'[，,：:\s]',text,re.I) for name in handles)


def quiet_request(message, settings, group, self_id='', names=()):
    if not textual(message) or addressed_elsewhere(message,settings,group,self_id,names):return False
    text=message['text'].strip()
    if text.startswith(('他说','她说','例如','比如','引用','转发','“','"','「')):return False
    if LOCAL_SILENCE.search(text):return True
    for name in sorted(set(settings.mentions+[settings.bot_name]),key=len,reverse=True):
        text=re.sub(r'^@?'+re.escape(name)+r'[\s，,：:]*','',text,flags=re.I)
    return bool(QUIET_REQUEST.fullmatch(text) or CORRECTION.search(text))


def silence_narration(text):
    return bool(re.search(r'(?:我(?:就|先|这次|这轮)?(?:不接|不抢答|不回复|安静(?:了|待着))|(?:这句|这条|这轮).{0,14}(?:不接了|不抢答了)|收到.{0,12}(?:别接|不接|安静))',text))


class ConversationPolicy:
    def __init__(self,store,clock=time.time):
        self.store,self.clock=store,clock

    def quiet(self,group,reason):
        state=self.store.set_quiet(group.id,self.clock()+group.quiet_seconds,reason)
        self.store.cancel_chat(group.id,'本群进入安静状态，旧闲聊回复已取消')
        return state

    def wake(self,group):
        # Revision changes also invalidate replies computed before the explicit wake.
        state=self.store.set_quiet(group.id,0,'本机面板或管理员恢复')
        self.store.cancel_chat(group.id,'安静状态已改变，旧闲聊回复已取消')
        return state

    def observe(self,group,messages,settings,self_id='',names=()):
        changed=False
        for message in messages:
            if message.get('group')!=group.id or not textual(message) or not self.store.policy_observed(message,self.clock()):continue
            # Historical messages must not re-arm a pause after restart or replay.
            if self.clock()-message['create_time']>group.quiet_seconds or message['create_time']>self.clock()+30:continue
            if quiet_request(message,settings,group,self_id,names):
                self.quiet(group,'群友要求安静或纠正插话')
                self.store.event('quiet','本群闲聊进入安静状态，不发送确认文字',group.id)
                changed=True
        return changed

    def capacity(self,group,batch_id='',characters=0,direct=False,conversation=False):
        now=self.clock();state=self.store.policy(group.id)
        usage=self.store.chat_usage(group.id,now-group.budget_window_seconds)
        remaining_messages=max(0,group.message_budget-usage['messages'])
        remaining_chars=max(0,group.character_budget-usage['characters'])
        cooling=bool(usage['last_at'] and now-usage['last_at']<group.cooldown_seconds and usage['last_batch']!=batch_id)
        quiet=state['quiet_until']>now
        reason='quiet' if quiet and not direct else '' if direct or conversation else 'cooldown' if cooling else 'budget' if not remaining_messages or remaining_chars<characters or not remaining_chars else ''
        return {**state,'allowed':not reason,'blocked_by':reason,'remaining_messages':remaining_messages,
                'remaining_characters':remaining_chars,'used_messages':usage['messages'],'used_characters':usage['characters'],
                'cooldown_remaining':max(0,group.cooldown_seconds-(now-usage['last_at'])) if usage['last_at'] else 0,
                'quiet_remaining':max(0,state['quiet_until']-now)}

    def eligible(self,group,messages,settings,self_id='',names=()):
        return [m for m in messages if textual(m) and not LOCAL_SILENCE.search(m['text'])
                and not quiet_request(m,settings,group,self_id,names)
                and (group.is_private or not addressed_elsewhere(m,settings,group,self_id,names,include_quote=group.chat_style!='classmates'))]
