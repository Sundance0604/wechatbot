import re


def sender_prefix(text,known_ids,group):
    match = re.match(r'^([^:\r\n]{1,160}):\s*',text.lstrip('\ufeff'))
    if match:
        candidate = match[1].strip()
        if candidate!=group and (candidate in known_ids or candidate.startswith('wxid_')):
            return candidate,match.end()
    return None,0
