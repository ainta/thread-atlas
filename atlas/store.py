import hashlib
import json
import sqlite3
import threading
import time
from contextlib import contextmanager


def ident(*parts):
    return hashlib.sha256(json.dumps(parts, ensure_ascii=False).encode()).hexdigest()[:32]


SCHEMA = """
CREATE TABLE IF NOT EXISTS groups(id TEXT PRIMARY KEY,name TEXT NOT NULL UNIQUE);
CREATE TABLE IF NOT EXISTS sessions(
 id TEXT PRIMARY KEY,provider TEXT,account TEXT,source TEXT,cwd TEXT,url TEXT,title TEXT,
 group_id TEXT REFERENCES groups(id),position REAL DEFAULT 0,hidden INTEGER DEFAULT 0,
 state TEXT DEFAULT 'idle',latest TEXT DEFAULT '',latest_at REAL DEFAULT 0,seen INTEGER DEFAULT 0,
 live_at REAL DEFAULT 0,bridge TEXT DEFAULT '',writable INTEGER DEFAULT 0,synced_at REAL DEFAULT 0);
CREATE TABLE IF NOT EXISTS messages(
 seq INTEGER PRIMARY KEY AUTOINCREMENT,session TEXT REFERENCES sessions(id),source_id TEXT,
 role TEXT,body TEXT,at REAL,alert INTEGER, UNIQUE(session,source_id));
CREATE INDEX IF NOT EXISTS message_thread ON messages(session,at);
CREATE INDEX IF NOT EXISTS alerts ON messages(alert,seq);
CREATE TABLE IF NOT EXISTS files(path TEXT PRIMARY KEY,inode INTEGER,offset INTEGER,meta TEXT);
CREATE TABLE IF NOT EXISTS deliveries(
 id TEXT PRIMARY KEY,session TEXT REFERENCES sessions(id),body TEXT,status TEXT,
 detail TEXT DEFAULT '',created REAL,updated REAL,tab TEXT DEFAULT '');
CREATE INDEX IF NOT EXISTS pending ON deliveries(session,status,created);
CREATE TABLE IF NOT EXISTS prefs(key TEXT PRIMARY KEY,value TEXT);
"""


class Store:
    def __init__(self, path):
        self.path = path
        self.local = threading.local()
        self.db.executescript(SCHEMA)
        self.db.execute("INSERT OR IGNORE INTO prefs VALUES('baseline',?)",(str(time.time()),))
        self.baseline = float(self.pref("baseline"))

    @property
    def db(self):
        if not hasattr(self.local,"db"):
            db=sqlite3.connect(self.path, isolation_level=None,timeout=20)
            db.row_factory=sqlite3.Row
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("PRAGMA foreign_keys=ON")
            self.local.db=db
        return self.local.db

    @contextmanager
    def tx(self):
        self.db.execute("BEGIN IMMEDIATE")
        try:
            yield
            self.db.execute("COMMIT")
        except BaseException:
            self.db.execute("ROLLBACK")
            raise

    def pref(self,key,default=""):
        row=self.db.execute("SELECT value FROM prefs WHERE key=?",(key,)).fetchone()
        return row[0] if row else default

    def group(self,name):
        name=str(name or "Unsorted").strip()[:160] or "Unsorted"
        key=ident("project",name)
        self.db.execute("INSERT OR IGNORE INTO groups VALUES(?,?)",(key,name))
        return key

    def session(self,provider,account,source,group,title="",cwd="",url=""):
        key=ident(provider,account,source)
        self.db.execute("""INSERT INTO sessions(id,provider,account,source,cwd,url,title,group_id,synced_at)
          VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET synced_at=excluded.synced_at,
          title=CASE WHEN excluded.title!='' THEN excluded.title ELSE sessions.title END,
          url=CASE WHEN excluded.url!='' THEN excluded.url ELSE sessions.url END""",
          (key,provider,account,source,cwd,url,title[:240],self.group(group),time.time()))
        return key

    def session_row(self,key):
        row=self.db.execute("SELECT * FROM sessions WHERE id=?",(key,)).fetchone()
        if not row: raise ValueError("Unknown session")
        return dict(row)

    def message(self,sid,source,role,body,at,notify=True):
        if role not in ('assistant','user') or not isinstance(body,str) or not body.strip():return
        body=body.strip()[:200000]
        at=float(at or 0)
        alert=int(role=="assistant" and notify and at>=self.baseline)
        row=self.db.execute("""INSERT OR IGNORE INTO messages(session,source_id,role,body,at,alert)
            SELECT id,?,?,?,?,CASE WHEN hidden THEN 0 ELSE ? END FROM sessions WHERE id=?""",
            (source,role,body,at,alert,sid))
        if not row.rowcount:return
        if role=="user":
            self.db.execute("UPDATE sessions SET title=? WHERE id=? AND title=''",(" ".join(body.split())[:95],sid))
        else:
            self.db.execute("UPDATE sessions SET latest=?, latest_at=? WHERE id=? AND latest_at<=?",
                            (body[:2200],at,sid,at))

    def edit(self,key,change):
        with self.tx():
            self.session_row(key)
            if "hidden" in change:
                self.db.execute("UPDATE sessions SET hidden=? WHERE id=?",(int(bool(change["hidden"])),key))
            if change.get("read"):
                self.db.execute("UPDATE sessions SET seen=COALESCE((SELECT max(seq) FROM messages WHERE session=?),0) WHERE id=?",(key,key))
            if "title" in change:
                name=str(change["title"]).strip()[:240]
                if name:self.db.execute("UPDATE sessions SET title=? WHERE id=?",(name,key))
            if "group" in change:
                group=change["group"]
                if not self.db.execute("SELECT id FROM groups WHERE id=?",(group,)).fetchone():raise ValueError("Unknown project")
                ids=[r[0] for r in self.db.execute("SELECT id FROM sessions WHERE group_id=? AND id!=? ORDER BY position,title,id",(group,key))]
                before=change.get("before")
                ids.insert(ids.index(before) if before in ids else len(ids),key)
                self.db.executemany("UPDATE sessions SET group_id=?,position=? WHERE id=?",[(group,n,i) for n,i in enumerate(ids)])

    def history(self,key):
        self.session_row(key)
        return [dict(r) for r in self.db.execute("""SELECT role,body,at FROM
          (SELECT role,body,at,seq FROM messages WHERE session=? ORDER BY at DESC,seq DESC LIMIT 200)
          ORDER BY at,seq""",(key,))]

    def state(self):
        now=time.time()
        items=[dict(r) for r in self.db.execute("""SELECT s.id,s.provider,s.account,s.title,s.group_id,s.hidden,s.state,
         s.latest,s.latest_at,s.url,s.live_at,s.synced_at,s.bridge,s.writable,
         EXISTS(SELECT 1 FROM messages m WHERE m.session=s.id AND m.seq>s.seen AND m.alert=1) unread
         FROM sessions s ORDER BY s.group_id,s.position,s.title,s.id""")]
        for i in items:
            i['web_live']=bool(i.pop('bridge') and i['live_at']>=now-35)
        return {"sessions":items,"groups":[dict(r) for r in self.db.execute("SELECT * FROM groups ORDER BY name")],
                "inbox":[dict(r) for r in self.db.execute("""SELECT m.seq,m.session,substr(m.body,1,600) body,m.at,m.seq>s.seen AND m.alert unread
                FROM messages m JOIN sessions s ON s.id=m.session WHERE s.hidden=0 AND m.role='assistant'
                ORDER BY m.at DESC,m.seq DESC LIMIT 150""")],
                "deliveries":[dict(r) for r in self.db.execute("SELECT id,session,status,detail,created,updated FROM deliveries ORDER BY created DESC LIMIT 100")]}
