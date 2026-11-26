import difflib
import hashlib
import json
import re
import sqlite3
import threading
import time
from contextlib import contextmanager


def ident(*parts):
    return hashlib.sha256(json.dumps(parts, ensure_ascii=False).encode()).hexdigest()[:32]


def fingerprint(text):
    """Reduce a message to what survives rendering, so Markdown source and page text compare equal."""
    text=re.sub(r'\[([^\]]*)\]\([^)]*\)',r'\1',text[:20000])
    text=re.sub(r'(?m)^\s*(?:[-*+]|\d+[.)])\s+','',text)
    return ''.join(ch for ch in text.casefold() if ch.isalnum())


def similar(a,b,threshold=.9):
    """True when two fingerprints differ only by the small additions a rendered page makes."""
    a,b=a[:6000],b[:6000]
    if min(len(a),len(b))<threshold*max(len(a),len(b)):return False
    matcher=difflib.SequenceMatcher(None,a,b,autojunk=False)
    return matcher.real_quick_ratio()>=threshold and matcher.quick_ratio()>=threshold and matcher.ratio()>=threshold


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
CREATE TABLE IF NOT EXISTS aliases(session TEXT,alias TEXT,seq INTEGER REFERENCES messages(seq),PRIMARY KEY(session,alias));
CREATE INDEX IF NOT EXISTS message_thread ON messages(session,at);
CREATE INDEX IF NOT EXISTS alias_target ON aliases(session,seq);
CREATE INDEX IF NOT EXISTS alerts ON messages(alert,seq);
CREATE INDEX IF NOT EXISTS inbox ON messages(role,at);
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

    def message(self,sid,source,role,body,at,notify=True,track_latest=True):
        """Store one message. Returns True when it was new.

        `track_latest` orders the session's latest reply by timestamp. Batches whose timestamps are missing or come
        from an older snapshot pass False and settle the latest reply by conversation order in `finish_batch`.
        """
        if role not in ('assistant','user') or not isinstance(body,str) or not body.strip():return False
        body=body.strip()[:200000]
        at=float(at or 0)
        alert=int(role=="assistant" and notify and at>=self.baseline)
        row=self.db.execute("""INSERT OR IGNORE INTO messages(session,source_id,role,body,at,alert)
            SELECT id,?,?,?,?,CASE WHEN hidden THEN 0 ELSE ? END FROM sessions WHERE id=?""",
            (source,role,body,at,alert,sid))
        if not row.rowcount:return False
        if role=="user":
            self.db.execute("UPDATE sessions SET title=? WHERE id=? AND title=''",(" ".join(body.split())[:95],sid))
        elif track_latest:
            self.db.execute("UPDATE sessions SET latest=?, latest_at=? WHERE id=? AND latest_at<=?",
                            (body[:2200],at,sid,at))
        return True

    def merge_message(self,sid,key,role,body,at,initial,seen,notify,estimated=False):
        """Align matching export/DOM observations without collapsing repeated utterances.

        `seen` is one dict per session and batch. It carries which stored messages are taken and where the last match was.
        """
        if role not in ('assistant','user') or not isinstance(body,str) or not body.strip():return
        body=body.strip()[:200000]
        matched=seen.setdefault('matched',set())
        alias=self.db.execute("SELECT a.seq,m.body,m.role,m.at FROM aliases a JOIN messages m ON m.seq=a.seq WHERE a.session=? AND a.alias=?",(sid,key)).fetchone()
        if alias:
            if not initial and alias['role']==role and alias['body']!=body:
                self.db.execute("UPDATE messages SET body=? WHERE seq=?",(body,alias['seq']))
                if role=='assistant':
                    self.db.execute("UPDATE sessions SET latest=? WHERE id=? AND latest_at=?",(body[:2200],sid,alias['at']))
            found,added=alias[0],False
        else:
            row=self.db.execute("SELECT seq FROM messages WHERE session=? AND source_id=?",(sid,key)).fetchone()
            found=row[0] if row else self.align(sid,role,body,seen) if initial else None
            added=False
            if found is None:
                added=self.message(sid,key,role,body,at,notify,track_latest=not initial)
                row=self.db.execute("SELECT seq FROM messages WHERE session=? AND source_id=?",(sid,key)).fetchone()
                found=row[0] if row else None
            if found is not None:
                self.db.execute("INSERT OR IGNORE INTO aliases VALUES(?,?,?)",(sid,key,found))
        if found is None:return
        matched.add(found);seen['last']=found
        if initial and at and not estimated:
            # A page read on connect stores history without times. A later export fills them in where it agrees.
            self.db.execute("UPDATE messages SET at=? WHERE seq=? AND at=0",(float(at),found))
        if role=='assistant':seen['tail']=(found,added)

    def finish_batch(self,sid,seen):
        """Settle the latest reply after a page read or an export, by conversation order rather than timestamps.

        The batch's final reply becomes the latest only when the batch is the first view of the session or that reply
        was new. A snapshot whose final reply is already known adds nothing after it, so it cannot replace a newer one.
        """
        tail=seen.get('tail')
        if not tail:return
        seq,added=tail
        row=self.db.execute("SELECT body,at FROM messages WHERE seq=?",(seq,)).fetchone()
        if row:
            self.db.execute("UPDATE sessions SET latest=?,latest_at=? WHERE id=? AND (?=1 OR latest='')",
                            (row['body'][:2200],row['at'],sid,int(added)))
            # Times an export filled in for the current latest reply also apply to the session.
            self.db.execute("UPDATE sessions SET latest_at=? WHERE id=? AND latest_at=0 AND latest=?",(row['at'],sid,row['body'][:2200]))

    def align(self,sid,role,body,seen):
        """Find the stored message that an export or a freshly read page is showing again under another ID."""
        pool=seen.get('pool')
        if pool is None:
            pool=seen['pool']=[dict(r) for r in self.db.execute("SELECT seq,role,body FROM messages WHERE session=? ORDER BY at,seq",(sid,))]
        matched=seen['matched']
        free=[r for r in pool if r['role']==role and r['seq'] not in matched]
        exact=next((r for r in free if r['body']==body),None)
        if exact:return exact['seq']
        mark=fingerprint(body)
        if not mark:return None
        def marked(r):
            if 'mark' not in r:r['mark']=fingerprint(r['body'])
            return r['mark']
        same=next((r for r in free if marked(r)==mark),None)
        if same:return same['seq']
        # Pages add or drop a little text around code, citations and math. Accept the message stored right after the
        # previous match only when nearly all of the text agrees. A regenerated answer often opens with the same
        # sentence, so a shared beginning or ending alone is not enough and would hide the newer answer.
        spot=next((n for n,r in enumerate(pool) if r['seq']==seen.get('last')),None)
        after=pool[spot+1] if spot is not None and spot+1<len(pool) else None
        if after and after['role']==role and after['seq'] not in matched:
            other=marked(after)
            if min(len(mark),len(other))>=24 and similar(mark,other):return after['seq']
        return None

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

    def alerts_after(self,after):
        limit=100
        last=self.db.execute("SELECT COALESCE(max(seq),0) FROM messages").fetchone()[0]
        if after is None:return [],last
        rows=[dict(r) for r in self.db.execute("""SELECT m.seq,m.session,m.at,substr(m.body,1,2400) body
          FROM messages m JOIN sessions s ON s.id=m.session WHERE m.seq>? AND m.seq<=? AND m.alert=1 AND s.hidden=0
          ORDER BY m.seq LIMIT ?""",(after,last,limit))]
        return rows,(rows[-1]['seq'] if len(rows)==limit else last)

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
