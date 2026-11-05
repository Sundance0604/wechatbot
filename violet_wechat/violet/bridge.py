"""Read-only message access and the validated version-specific native text sender."""
import hashlib
import json
from pathlib import Path
import re
import threading
import unicodedata

import psutil

from .message_parser import clean_group_content,type_label,decode_payload,quoted_reply


def normalized(value):
    value = re.sub(r'\s+','',unicodedata.normalize('NFKC',value).casefold())
    return re.sub(r'^\W+','',value)


def resolve_contact(contacts,query,admin=False):
    query = query.strip().strip('“”\"\'')
    if not query:
        raise ValueError('请填写微信号或联系人。')
    fields = ['username','alias'] if admin else ['username','alias','nick_name','remark']
    exact = [r for r in contacts if query.casefold() in {str(r.get(k) or '').strip().casefold() for k in fields}]
    if not exact and not admin:
        exact = [r for r in contacts if normalized(query) in {normalized(str(r.get(k) or '')) for k in fields if r.get(k)}]
    if len(exact)!=1:
        raise ValueError('未找到唯一联系人，请使用准确微信号或群标识。' if admin else '未找到唯一会话，请填写完整群名、备注或微信号。')
    if admin and exact[0]['username'].endswith('@chatroom'):
        raise ValueError('管理员必须是个人微信账号。')
    return exact[0]


class Bridge:
    def __init__(self,settings,runtime):
        # Imported lazily so model/panel/tests do not require a running WeChat client.
        from wechatauto import WeChatDB
        class DB(WeChatDB):
            def _find_weixin_pids(self):
                return [p.pid for p in psutil.process_iter(['name']) if (p.info['name'] or '').lower()=='weixin.exe']
        self.db = DB(account=settings.wechat_account or None,db_dir=settings.wechat_data_dir or None)
        self.account = self.db.account
        self.self_id = settings.self_id or self.account.rsplit('_',1)[0]
        self.lock = threading.RLock()
        self.runtime = runtime
        self.refresh()

    def refresh(self):
        with self.lock:
            conn = self.db._contact_conn()
            try:
                self.contacts = [dict(r) for r in conn.execute('SELECT username,alias,nick_name,remark FROM contact WHERE delete_flag=0')]
                self.names = {r['username']:r['remark'] or r['nick_name'] or r['username'] for r in self.contacts}
            finally:
                conn.close()
        return self.contacts

    def resolve(self,query,admin=False):
        if not admin and query.casefold() in ['filehelper','文件传输助手']:
            return {'username':'filehelper','nick_name':'文件传输助手','alias':'','remark':''}
        return resolve_contact(self.contacts,query,admin)

    def snapshot(self,group,cursors,context_limit=30):
        table = 'Msg_'+hashlib.md5(group.encode()).hexdigest()
        fresh,context = [],[]
        with self.lock:
            for rel in self.db._message_dbs():
                conn = self.db._open(rel)
                try:
                    if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",(table,)).fetchone():
                        continue
                    identities = {r['rowid']:r['user_name'] for r in conn.execute('SELECT rowid,user_name FROM Name2Id')}
                    if self.self_id not in identities.values():
                        raise RuntimeError('无法核验当前微信账号身份，请检查设置中的当前账号。')
                    def parse(row):
                        row = dict(row)
                        sender = identities.get(row.get('real_sender_id'),'')
                        reference = quoted_reply(decode_payload(row.get('message_content')) or decode_payload(row.get('compress_content')))
                        return {'group':group,'shard':str(rel),'local_id':row['local_id'],'create_time':row['create_time'],
                                'sort_seq':row.get('sort_seq',0),'sender':sender,'speaker':self.names.get(sender,'群友'),
                                'is_self':sender==self.self_id,'type':'引用回复' if reference else type_label(row['local_type']),
                                'reply_to':reference,
                                'text':reference['text'] if reference else clean_group_content(row,self.names,group)[:8000],
                                'server_id':row.get('server_id'),'status':row.get('status')}
                    fresh.extend(parse(row) for row in conn.execute(f'SELECT * FROM "{table}" WHERE local_id>? ORDER BY local_id LIMIT 300',(cursors.get(str(rel),0),)))
                    context.extend(parse(row) for row in conn.execute(f'SELECT * FROM "{table}" ORDER BY local_id DESC LIMIT ?', (context_limit,)))
                finally:
                    conn.close()
        fresh.sort(key=lambda r:(r['shard'],r['local_id']))
        context.sort(key=lambda r:(r['create_time'],r['sort_seq'],r['local_id']))
        return fresh,context[-context_limit:]

    def positions(self,group):
        table = 'Msg_'+hashlib.md5(group.encode()).hexdigest()
        result = {}
        with self.lock:
            for rel in self.db._message_dbs():
                conn = self.db._open(rel)
                try:
                    if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",(table,)).fetchone():
                        result[str(rel)] = conn.execute(f'SELECT COALESCE(MAX(local_id),0) FROM "{table}"').fetchone()[0]
                finally:
                    conn.close()
        return result

    def verify(self,delivery):
        rows,_ = self.snapshot(delivery['group_id'],json.loads(delivery['baseline']))
        return next((r for r in rows if r['is_self'] and r['text']==delivery['body'] and r['server_id']),None)

    def sender(self):
        from .native_sender import Sender
        from .recovery import _recover
        path = self.runtime/'native_profile.json'
        current = False
        if path.exists():
            spec = json.loads(path.read_text(encoding='utf-8'))
            try:
                process = psutil.Process(spec['pid'])
                current = process.name().lower()=='weixin.exe' and process.create_time()==spec['process_creation_time']
            except psutil.Error:
                pass
        if not current:
            _recover(self.runtime)
        return Sender(path)
