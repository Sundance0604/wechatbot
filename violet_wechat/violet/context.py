"""Bounded conversation context; pending operations belong to one verified owner."""
import json
import time

from .store import key


class CommandContexts:
    def __init__(self, ttl=300, clock=time.monotonic):
        self.ttl, self.clock = ttl, clock
        self.pending = {}
        self.revision = 0

    def get(self, message):
        now = self.clock()
        for scope, entry in list(self.pending.items()):
            if entry['expires'] <= now:
                del self.pending[scope]
        entry = self.pending.get((message['group'], message['sender']))
        if entry and message['create_time'] - entry['source_time'] > self.ttl:
            self.clear(message['group'], message['sender'])
            return None
        return entry

    def remember(self, message, text, clarification, previous=None,recipient=None):
        turns = list(previous['turns']) if previous else []
        turns.extend([{'role':'user','text':text}, {'role':'assistant','text':clarification}])
        # Preserve the original request and latest clarification, not an AI summary.
        while len(turns)>8 or len(json.dumps(turns,ensure_ascii=False))>12000:
            if len(turns)<=3:
                raise ValueError('待补充指令过长，请重新提供接收者和完整正文。')
            del turns[1:3]
        self.get(message)
        scope = (message['group'], message['sender'])
        if scope not in self.pending and len(self.pending)>=128:
            oldest = min(self.pending,key=lambda k:self.pending[k]['expires'])
            del self.pending[oldest]
        self.pending[scope] = {'turns':turns,
            'expires':previous['expires'] if previous else self.clock()+self.ttl,
            'source_time':previous['source_time'] if previous else message['create_time'],
            'recipient':recipient or (previous.get('recipient') if previous else None)}

    def clear(self, group=None, owner=None):
        self.revision += 1
        removed = False
        for scope in list(self.pending):
            if (group is None or scope[0]==group) and (owner is None or scope[1]==owner):
                del self.pending[scope]
                removed = True
        return removed


def explicit_recipient(text):
    """Read a single recipient from the owner's words, never from an AI clarification."""
    import re
    head=text.split('|',1)[0].strip()
    patterns=[r'^(?:请|麻烦|帮我|你帮我|我让你|我叫你|我说的?是)?\s*(?:给|向)\s*(.+?)\s*(?:发(?:送)?(?:一条)?(?:消息|信息|私信)?|私聊)(?:\s|$|[，,。])',
              r'^(?:请|麻烦|帮我)?\s*(?:发消息|发送消息|私聊)\s+(.+?)\s*$',
              r'^(?:改|改成|换成|换)?(?:发)?给\s*(.+?)\s*(?:发(?:消息)?|私聊)?$']
    for pattern in patterns:
        match=re.search(pattern,head,re.I)
        if match:
            value=match[1].strip(' \t\r\n“”\"\'')
            value=re.sub(r'^(?:联系人|群聊)\s*','',value)
            if value and len(value)<=150:return value
    return ''


def followup_text(text,previous):
    import re
    text=text.strip()
    if re.match(r'^(?:那你|你刚|你怎么|你为什么|为什么|为啥|怎么又|必须|不是|不对|我让你|再说|继续|还有呢|然后呢|凭什么|问你|什么意思|能不能)',text):return True
    if text in ['？','?','然后','所以呢','具体呢']:return True
    return bool(re.search(r'(?:是谁|是什么|哪个好)[？?]?$' ,text) and
                re.search(r'(?:是谁|是什么|哪个好)[？?]?$',previous))

def order(message):
    return (message['create_time'],message.get('sort_seq') or 0,
            message['local_id'],message['shard'])


def recent_context(group, history, batch, clean, limit, budget=12000):
    """Use earlier, unique messages only; reserve the full batch for new_messages."""
    if not batch:
        return []
    cutoff = min(order(m) for m in batch)
    seen = {key(m) for m in batch}
    result, used = [], 0
    for message in sorted(history,key=order,reverse=True):
        identifier = key(message)
        if message['group']!=group or identifier in seen or order(message)>=cutoff:
            continue
        seen.add(identifier)
        value = clean(message)
        size = len(json.dumps(value,ensure_ascii=False))
        if used+size>budget or len(result)>=limit:
            break
        result.append(value)
        used += size
    return list(reversed(result))
