import hashlib
import json
import sqlite3
import threading
import time


def key(message):
    return f"{message['group']}:{message['shard']}:{message['local_id']}"


class Store:
    def __init__(self,path):
        path.parent.mkdir(parents=True,exist_ok=True)
        self.lock = threading.RLock()
        self.db = sqlite3.connect(path,check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript('''
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS groups(id TEXT PRIMARY KEY,cursors TEXT NOT NULL,last_poll REAL,paused INTEGER DEFAULT 0);
            CREATE TABLE IF NOT EXISTS inbox(key TEXT PRIMARY KEY,group_id TEXT,body TEXT,created REAL);
            CREATE TABLE IF NOT EXISTS outgoing(id TEXT PRIMARY KEY,group_id TEXT,source_group TEXT,body TEXT,
                baseline TEXT,status TEXT,server_id TEXT,created REAL,error TEXT);
            CREATE TABLE IF NOT EXISTS actions(id TEXT PRIMARY KEY,body TEXT,result TEXT,status TEXT);
            CREATE TABLE IF NOT EXISTS admin_turns(key TEXT PRIMARY KEY,plan TEXT NOT NULL,result TEXT);
            CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY AUTOINCREMENT,time REAL,kind TEXT,group_id TEXT,message TEXT);
            CREATE TABLE IF NOT EXISTS agent_jobs(id TEXT PRIMARY KEY,workspace_id TEXT,owner TEXT,source_group TEXT,
                prompt TEXT,status TEXT,result TEXT,error TEXT,created REAL,updated REAL,thread_id TEXT,turn_id TEXT,
                session_key TEXT,notification INTEGER DEFAULT 0);
            CREATE TABLE IF NOT EXISTS agent_events(id INTEGER PRIMARY KEY AUTOINCREMENT,job_id TEXT,time REAL,kind TEXT,body TEXT);
            CREATE TABLE IF NOT EXISTS agent_sessions(id TEXT PRIMARY KEY,workspace_id TEXT,owner TEXT,source_group TEXT,thread_id TEXT);
            CREATE TABLE IF NOT EXISTS agent_approvals(id TEXT PRIMARY KEY,job_id TEXT,kind TEXT,details TEXT,status TEXT,created REAL);
            CREATE TABLE IF NOT EXISTS agent_workspace_snapshots(job_id TEXT PRIMARY KEY,body TEXT);
            CREATE TABLE IF NOT EXISTS agent_context_snapshots(job_id TEXT PRIMARY KEY,body TEXT);
            CREATE TABLE IF NOT EXISTS command_batches(id TEXT PRIMARY KEY,source_group TEXT,owner TEXT,
                description TEXT,created REAL);
            CREATE TABLE IF NOT EXISTS command_items(id TEXT PRIMARY KEY,batch_id TEXT);
            CREATE TABLE IF NOT EXISTS conversation_policy(group_id TEXT PRIMARY KEY,quiet_until REAL DEFAULT 0,
                revision INTEGER DEFAULT 0,reason TEXT DEFAULT '');
            CREATE TABLE IF NOT EXISTS policy_seen(key TEXT PRIMARY KEY,seen REAL);
            CREATE TABLE IF NOT EXISTS chat_delivery_meta(id TEXT PRIMARY KEY,body TEXT);
            CREATE TABLE IF NOT EXISTS chat_sent(id TEXT PRIMARY KEY,group_id TEXT,batch_id TEXT,confirmed REAL,characters INTEGER);
            CREATE INDEX IF NOT EXISTS chat_sent_group_time ON chat_sent(group_id,confirmed);
        ''')

    def close(self):
        self.db.close()

    def event(self,kind,message,group=''):
        with self.lock,self.db:
            self.db.execute('INSERT INTO events(time,kind,group_id,message) VALUES(?,?,?,?)',(time.time(),kind,group,message))

    def groups(self):
        with self.lock:
            return {r['id']:{**dict(r),'cursors':json.loads(r['cursors'])} for r in self.db.execute('SELECT * FROM groups')}

    def seed(self,group,cursors):
        with self.lock,self.db:
            self.db.execute('INSERT OR IGNORE INTO groups(id,cursors) VALUES(?,?)',(group,json.dumps(cursors)))

    def poll(self,group,rows):
        with self.lock,self.db:
            cursor = json.loads(self.db.execute('SELECT cursors FROM groups WHERE id=?',(group,)).fetchone()[0])
            for row in rows:
                shard = row['shard']
                if row['local_id']<=cursor.get(shard,0):
                    continue
                cursor[shard] = row['local_id']
                if not row['is_self']:
                    self.db.execute('INSERT OR IGNORE INTO inbox VALUES(?,?,?,?)',
                        (key(row),group,json.dumps(row,ensure_ascii=False),row['create_time']))
            self.db.execute('UPDATE groups SET cursors=?,last_poll=? WHERE id=?',(json.dumps(cursor),time.time(),group))

    def batch(self,group,limit):
        with self.lock:
            return [json.loads(r[0]) for r in self.db.execute('SELECT body FROM inbox WHERE group_id=? ORDER BY created,key LIMIT ?',(group,limit))]

    def consume(self,batch):
        with self.lock,self.db:
            self.db.executemany('DELETE FROM inbox WHERE key=?',[(key(m),) for m in batch])

    def reserve(self,group,batch,parts,baselines,metadata=None):
        """Consume inputs and persist every reply part in the same transaction."""
        source = '\n'.join(key(m) for m in batch)
        batch_id = hashlib.sha256((group+'\n'+source+'\n'+json.dumps(parts,ensure_ascii=False)).encode()).hexdigest()[:24]
        with self.lock,self.db:
            for index,text in enumerate(parts):
                self.db.execute('INSERT OR IGNORE INTO outgoing VALUES(?,?,?,?,?,?,?,?,?)',
                    (f'{batch_id}_{index}',group,group,text,json.dumps(baselines),'prepared',None,time.time()+index/1000,None))
                info = {**(metadata or {'kind':'chat'}),'batch_id':batch_id}
                self.db.execute('INSERT OR IGNORE INTO chat_delivery_meta VALUES(?,?)',
                    (f'{batch_id}_{index}',json.dumps(info,ensure_ascii=False)))
            self.db.executemany('DELETE FROM inbox WHERE key=?',[(key(m),) for m in batch])
        return batch_id

    def chat_meta(self,identifier):
        with self.lock:
            row=self.db.execute('SELECT body FROM chat_delivery_meta WHERE id=?',(identifier,)).fetchone()
            return json.loads(row[0]) if row else None

    def update_chat_meta(self,identifier,body):
        with self.lock,self.db:
            self.db.execute('UPDATE chat_delivery_meta SET body=? WHERE id=?',(json.dumps(body,ensure_ascii=False),identifier))

    def policy(self,group):
        with self.lock:
            row=self.db.execute('SELECT * FROM conversation_policy WHERE group_id=?',(group,)).fetchone()
            return dict(row) if row else {'group_id':group,'quiet_until':0,'revision':0,'reason':''}

    def set_quiet(self,group,until,reason):
        with self.lock,self.db:
            self.db.execute('INSERT INTO conversation_policy VALUES(?,?,1,?) ON CONFLICT(group_id) DO UPDATE SET '
                'quiet_until=excluded.quiet_until,revision=revision+1,reason=excluded.reason',(group,until,reason))
            return self.policy(group)

    def revise_policy(self,group,reason):
        with self.lock,self.db:
            self.db.execute('INSERT INTO conversation_policy VALUES(?,0,1,?) ON CONFLICT(group_id) DO UPDATE SET '
                            'revision=revision+1,reason=excluded.reason',(group,reason))

    def policy_observed(self,message,now,scope=''):
        with self.lock,self.db:
            inserted=self.db.execute('INSERT OR IGNORE INTO policy_seen VALUES(?,?)',(scope+key(message),now)).rowcount==1
            self.db.execute('DELETE FROM policy_seen WHERE seen<?',(now-86400,))
            return inserted

    def cancel_chat(self,group,reason,batch_id=None):
        with self.lock,self.db:
            ids=[r['id'] for r in self.db.execute("SELECT id FROM outgoing WHERE source_group=? AND status='prepared'",(group,))]
            affected=0
            for identifier in ids:
                meta=self.chat_meta(identifier)
                if meta and meta.get('kind')=='chat' and (batch_id is None or meta.get('batch_id')==batch_id):
                    affected+=self.db.execute("UPDATE outgoing SET status='cancelled',error=? WHERE id=? AND status='prepared'",(reason,identifier)).rowcount
            return affected

    def chat_usage(self,group,since):
        with self.lock:
            row=self.db.execute('SELECT count(*),COALESCE(sum(characters),0) FROM chat_sent WHERE group_id=? AND confirmed>?',(group,since)).fetchone()
            latest=self.db.execute('SELECT confirmed,batch_id FROM chat_sent WHERE group_id=? ORDER BY confirmed DESC LIMIT 1',(group,)).fetchone()
            return {'messages':row[0],'characters':row[1],'last_at':latest[0] if latest else 0,'last_batch':latest[1] if latest else ''}

    def recent_chat_turns(self,group,since):
        with self.lock:
            rows=self.db.execute("SELECT o.body,o.created,m.body AS metadata FROM outgoing o JOIN chat_delivery_meta m ON m.id=o.id "
                "WHERE o.source_group=? AND o.status='sent' AND o.created>=? ORDER BY o.created DESC LIMIT 100",(group,since)).fetchall()
            return [{'reply':r['body'],'created':r['created'],'metadata':json.loads(r['metadata'])} for r in rows]

    def reserve_direct(self,identifier,target,source,text,baseline):
        with self.lock,self.db:
            self.db.execute('INSERT OR IGNORE INTO outgoing VALUES(?,?,?,?,?,?,?,?,?)',
                (identifier,target,source,text,json.dumps(baseline),'prepared',None,time.time(),None))

    def outbox(self,statuses=('prepared',)):
        with self.lock:
            marks = ','.join('?' for _ in statuses)
            return [dict(r) for r in self.db.execute(f"SELECT * FROM outgoing WHERE status IN ({marks}) ORDER BY CASE WHEN id LIKE 'command_%' THEN 1 ELSE 0 END,created",statuses)]

    def reserve_command(self,identifier,source,owner,description,items):
        with self.lock,self.db:
            existing=self.command_batch(identifier)
            if existing:return existing
            self.db.execute('INSERT INTO command_batches VALUES(?,?,?,?,?)',(identifier,source,owner,description,time.time()))
            for index,item in enumerate(items):
                delivery='command_'+identifier+'_'+str(index)
                self.db.execute('INSERT INTO outgoing VALUES(?,?,?,?,?,?,?,?,?)',
                    (delivery,item['target'],source,item['text'],json.dumps(item['baseline']),'prepared',None,time.time()+index/1000,None))
                self.db.execute('INSERT INTO command_items VALUES(?,?)',(delivery,identifier))
            return self.command_batch(identifier)

    def command_batch(self,identifier):
        with self.lock:
            row=self.db.execute('SELECT * FROM command_batches WHERE id=?',(identifier,)).fetchone()
            if not row:return None
            counts={r[0]:r[1] for r in self.db.execute('SELECT o.status,count(*) FROM outgoing o JOIN command_items i ON i.id=o.id WHERE i.batch_id=? GROUP BY o.status',(identifier,))}
            return {**dict(row),'counts':counts,'total':sum(counts.values())}

    def cancel_command(self,identifier):
        with self.lock,self.db:
            return self.db.execute("UPDATE outgoing SET status='cancelled',error='管理员取消批量任务' WHERE status='prepared' AND id IN (SELECT id FROM command_items WHERE batch_id=?)",(identifier,)).rowcount

    def delivery(self,identifier):
        with self.lock:
            row = self.db.execute('SELECT * FROM outgoing WHERE id=?',(identifier,)).fetchone()
            return dict(row) if row else None

    def job(self,identifier):
        with self.lock:
            row = self.db.execute('SELECT * FROM agent_jobs WHERE id=?',(identifier,)).fetchone()
            if not row:return None
            snapshot=self.db.execute('SELECT body FROM agent_workspace_snapshots WHERE job_id=?',(identifier,)).fetchone()
            return {**dict(row),'workspace_config':json.loads(snapshot[0]) if snapshot else None}

    def save_job_context(self,identifier,body):
        with self.lock,self.db:
            self.db.execute('INSERT OR IGNORE INTO agent_context_snapshots VALUES(?,?)',(identifier,json.dumps(body,ensure_ascii=False)))

    def job_context(self,identifier):
        with self.lock:
            row=self.db.execute('SELECT body FROM agent_context_snapshots WHERE job_id=?',(identifier,)).fetchone()
            return json.loads(row[0]) if row else None

    def jobs(self,limit=100):
        with self.lock:
            return [dict(row) for row in self.db.execute('SELECT * FROM agent_jobs ORDER BY created DESC LIMIT ?',(limit,))]

    def recover_agent_jobs(self):
        with self.lock,self.db:
            self.db.execute("UPDATE agent_jobs SET status='interrupted',error='服务已重启；任务未自动重做。',updated=? WHERE status IN ('queued','running','awaiting_approval')",(time.time(),))

    def create_job(self,identifier,workspace,owner,source,prompt,session_key,workspace_config=None):
        with self.lock,self.db:
            created=self.db.execute('INSERT OR IGNORE INTO agent_jobs VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                (identifier,workspace,owner,source,prompt,'queued','','',time.time(),time.time(),None,None,session_key,0)).rowcount==1
            if created and workspace_config is not None:
                self.db.execute('INSERT INTO agent_workspace_snapshots VALUES(?,?)',(identifier,json.dumps(workspace_config,ensure_ascii=False)))
            return created

    def update_job(self,identifier,**fields):
        allowed={'status','result','error','thread_id','turn_id','notification'}
        if not fields or set(fields)-allowed:
            raise ValueError('Invalid job fields')
        fields['updated']=time.time()
        with self.lock,self.db:
            self.db.execute('UPDATE agent_jobs SET '+','.join(k+'=?' for k in fields)+' WHERE id=?',(*fields.values(),identifier))

    def agent_event(self,job,kind,body):
        with self.lock,self.db:
            self.db.execute('INSERT INTO agent_events(job_id,time,kind,body) VALUES(?,?,?,?)',(job,time.time(),kind,body))

    def job_events(self,job,limit=200):
        with self.lock:
            return [dict(r) for r in self.db.execute('SELECT * FROM agent_events WHERE job_id=? ORDER BY id DESC LIMIT ?',(job,limit))][::-1]

    def session_thread(self,identifier):
        with self.lock:
            row=self.db.execute('SELECT thread_id FROM agent_sessions WHERE id=?',(identifier,)).fetchone()
            return row[0] if row else None

    def save_session(self,identifier,workspace,owner,source,thread):
        with self.lock,self.db:
            self.db.execute('INSERT OR REPLACE INTO agent_sessions VALUES(?,?,?,?,?)',(identifier,workspace,owner,source,thread))

    def approval(self,identifier,job,kind,details):
        with self.lock,self.db:
            self.db.execute('INSERT INTO agent_approvals VALUES(?,?,?,?,?,?)',
                (identifier,job,kind,json.dumps(details,ensure_ascii=False),'pending',time.time()))

    def approvals(self,job=None):
        with self.lock:
            rows=self.db.execute('SELECT * FROM agent_approvals WHERE job_id=? ORDER BY created',(job,)) if job else self.db.execute("SELECT * FROM agent_approvals WHERE status='pending' ORDER BY created")
            return [{**dict(r),'details':json.loads(r['details'])} for r in rows]

    def finish_approval(self,identifier,status):
        with self.lock,self.db:
            self.db.execute('UPDATE agent_approvals SET status=? WHERE id=?',(status,identifier))

    def update_outgoing(self,identifier,status,server_id=None,error=None,confirmed_at=None):
        with self.lock,self.db:
            self.db.execute('UPDATE outgoing SET status=?,server_id=?,error=? WHERE id=?',(status,str(server_id) if server_id else None,error,identifier))
            if status=='sent':
                meta=self.chat_meta(identifier)
                if meta and meta.get('kind')=='chat' and not meta.get('direct_invitation') and not meta.get('conversation_reply'):
                    row=self.db.execute('SELECT source_group,body FROM outgoing WHERE id=?',(identifier,)).fetchone()
                    self.db.execute('INSERT OR IGNORE INTO chat_sent VALUES(?,?,?,?,?)',
                        (identifier,row['source_group'],meta['batch_id'],time.time() if confirmed_at is None else confirmed_at,len(row['body'])))

    def baseline(self,identifier,value):
        with self.lock,self.db:
            self.db.execute('UPDATE outgoing SET baseline=? WHERE id=?',(json.dumps(value),identifier))

    def cancel_prepared(self,source=None,reason='监听已停止，未发送的回复已取消'):
        with self.lock,self.db:
            sql = "UPDATE outgoing SET status='cancelled',error=? WHERE status='prepared'"
            values = [reason]
            if source is not None:
                sql += ' AND source_group=?'
                values.append(source)
            return self.db.execute(sql,values).rowcount

    def cancel_agent_sends(self,job):
        with self.lock,self.db:
            return self.db.execute("UPDATE outgoing SET status='cancelled',error='所属Agent任务已取消' WHERE status='prepared' AND id GLOB ?",
                                   ('agent_send_'+job+'_*',)).rowcount

    def cancel_delivery_if_prepared(self,identifier):
        with self.lock,self.db:
            return self.db.execute("UPDATE outgoing SET status='cancelled',error='等待发送超时，已取消' WHERE id=? AND status='prepared'",(identifier,)).rowcount==1

    def action(self,identifier):
        with self.lock:
            row = self.db.execute('SELECT * FROM actions WHERE id=?',(identifier,)).fetchone()
            return dict(row) if row else None

    def admin_turn(self,identifier):
        with self.lock:
            row = self.db.execute('SELECT plan,result FROM admin_turns WHERE key=?',(identifier,)).fetchone()
            return {'plan':json.loads(row['plan']),'result':row['result']} if row else None

    def save_admin_plan(self,identifier,plan):
        with self.lock,self.db:
            self.db.execute('INSERT OR IGNORE INTO admin_turns(key,plan) VALUES(?,?)',
                            (identifier,json.dumps(plan,ensure_ascii=False)))

    def finish_admin_turn(self,identifier,result):
        with self.lock,self.db:
            self.db.execute('UPDATE admin_turns SET result=? WHERE key=? AND result IS NULL',(result or '',identifier))

    def cancel_admin_turns(self,group=None):
        with self.lock,self.db:
            query = 'UPDATE admin_turns SET result=? WHERE result IS NULL'
            args = ['监听或操作上下文已改变，操作未执行，请重新提供完整指令。']
            if group is not None:
                query += ' AND substr(key,1,length(?)+1)=?'
                args.extend([group,group+':'])
            self.db.execute(query,args)

    def claim_action(self,identifier,body):
        with self.lock,self.db:
            return self.db.execute('INSERT OR IGNORE INTO actions VALUES(?,?,?,?)',
                (identifier,json.dumps(body,ensure_ascii=False),'','running')).rowcount==1

    def finish_action(self,identifier,result):
        with self.lock,self.db:
            self.db.execute('UPDATE actions SET result=?,status=? WHERE id=?',(result,'completed',identifier))

    def pause(self,group,paused):
        with self.lock,self.db:
            self.db.execute('UPDATE groups SET paused=? WHERE id=?',(int(paused),group))

    def overview(self):
        with self.lock:
            counts = {r[0]:r[1] for r in self.db.execute('SELECT status,count(*) FROM outgoing GROUP BY status')}
            queues = {r[0]:r[1] for r in self.db.execute('SELECT group_id,count(*) FROM inbox GROUP BY group_id')}
            sent = {r[0]:r[1] for r in self.db.execute("SELECT source_group,count(*) FROM outgoing WHERE status='sent' GROUP BY source_group")}
            events = [dict(r) for r in self.db.execute('SELECT * FROM events ORDER BY id DESC LIMIT 100')]
            deliveries = [dict(r) for r in self.db.execute('SELECT id,group_id,source_group,body,status,server_id,created,error FROM outgoing ORDER BY created DESC LIMIT 100')]
            return {'counts':counts,'queues':queues,'sent_by_group':sent,'events':events,'deliveries':deliveries,'groups':self.groups()}
