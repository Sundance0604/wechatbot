"""WeChat-only commands: fixed syntax and a bounded natural-language extraction schema."""
import json
import re
from dataclasses import dataclass,field
from typing import Literal

from pydantic import Field

from .settings import Strict


HELP = ('/command forward 30 -> ExampleContact\n'
        '/command forward 10 -> ["联系人A", "群名B"]\n'
        '/command send ["联系人A", "群名B"] | 正文\n'
        '/command status 任务编号\n/command cancel 任务编号\n'
        '也支持：/command 把本会话最近30条消息转给 ExampleContact\n'
        '/command 给 ExampleContact 发消息，内容是：今晚八点见\n'
        'forward 转发本会话最近的消息为带时间、发言人的文字；图片、语音仅有类型标签。')


class NaturalCommand(Strict):
    kind: Literal['send','forward','status','cancel','help','clarify']
    targets: list[str] = Field(default_factory=list,max_length=20)
    count: int = Field(default=0,strict=True,ge=0,le=50)
    text: str = Field(default='',max_length=20000)
    identifier: str = Field(default='',max_length=24)
    clarification: str = Field(default='',max_length=300)

    def command(self):
        # Round-trip through the fixed parser so neither model output nor extra fields expand capabilities.
        if self.kind=='clarify':
            raise ValueError('请补充完整的 /command 指令：'+(self.clarification or '明确操作、收件人和正文；转发需指定本群条数，查询/取消需指定任务编号。'))
        if self.clarification:raise ValueError('指令信息不完整，请重新提供完整的 /command 指令。')
        if self.kind=='help':
            if self.targets or self.count or self.text or self.identifier:raise ValueError('命令字段不匹配，未执行。')
            return Command('help')
        if self.kind in ['send','forward']:
            if self.identifier or (self.kind=='send' and self.count) or (self.kind=='forward' and self.text):
                raise ValueError('命令字段不匹配，未执行。')
            destination=json.dumps(self.targets,ensure_ascii=False)
            suffix=f'{destination} | {self.text}' if self.kind=='send' else f'{self.count} -> {destination}'
        else:
            if self.targets or self.count or self.text:raise ValueError('命令字段不匹配，未执行。')
            suffix=self.identifier
        return parse('/command '+self.kind+' '+suffix)


def fixed_syntax(text):
    body=re.sub(r'^/command(?:\s+|$)','',text,count=1).strip()
    return bool(re.match(r'^(?:send|forward|status|cancel|help)(?:\s|$)',body,re.I)
                or re.match(r'^(?:转发\s+\d+\s*(?:->|→)|发送\s+[^|]+\||(?:状态|取消)\s+[a-f0-9]{24}$)',body,re.I))


@dataclass
class Command:
    kind:str
    targets:list[str]=field(default_factory=list)
    count:int=0
    text:str=''
    identifier:str=''


def targets(value):
    value=value.strip()
    try:
        parsed=json.loads(value) if value.startswith(('[','"')) else value
    except json.JSONDecodeError:
        raise ValueError('多个目标请使用 JSON 数组，例如 ["联系人A", "群名B"]。') from None
    parsed=[parsed] if isinstance(parsed,str) else parsed
    if not isinstance(parsed,list) or not 1<=len(parsed)<=20 or any(not isinstance(v,str) or not v.strip() or len(v)>150 for v in parsed):
        raise ValueError('请提供1–20个明确的联系人或群名。')
    return list(dict.fromkeys(v.strip() for v in parsed))


def parse(text):
    body=re.sub(r'^/command(?:\s+|$)','',text,count=1).strip()
    if not body or body in ['help','帮助']:return Command('help')
    match=re.fullmatch(r'(?:forward|转发)\s+(\d+)\s*(?:->|→)\s*([\s\S]+)',body,re.I)
    if match:
        count=int(match[1])
        if not 1<=count<=50:raise ValueError('一次可转发本会话最近1–50条消息。')
        return Command('forward',targets(match[2]),count=count)
    match=re.fullmatch(r'(?:send|发送)\s+([^|]+)\|([\s\S]+)',body,re.I)
    if match:
        content=match[2].strip()
        if not content or '\x00' in content or len(content)>20000:raise ValueError('正文需要1–20000字。')
        return Command('send',targets(match[1]),text=content)
    match=re.fullmatch(r'(status|cancel|状态|取消)\s+([a-f0-9]{24})',body,re.I)
    if match:return Command('status' if match[1].lower() in ['status','状态'] else 'cancel',identifier=match[2].lower())
    raise ValueError('命令格式不正确。\n'+HELP)
