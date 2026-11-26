import difflib
import hashlib
import json
import re
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager, nullcontext


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


WORKSPACE_STATUSES = ("default", "active", "hidden", "excluded")
SUPPRESSED_STATUSES = ("hidden", "excluded")
DEFAULT_PROJECT = "Unclassified"
STATUS_COLUMN = (
    "workspace_status TEXT NOT NULL DEFAULT 'default' "
    "CHECK(workspace_status IN ('default','active','hidden','excluded'))"
)
PROJECT_COLUMNS = f"""
 id TEXT PRIMARY KEY, name TEXT NOT NULL, parent_id TEXT REFERENCES groups(id),
 position INTEGER NOT NULL DEFAULT 0, {STATUS_COLUMN}
"""


def inherited_status(own, parent):
    return f"""CASE
      WHEN 'excluded' IN ({own}, {parent}) THEN 'excluded'
      WHEN 'hidden' IN ({own}, {parent}) THEN 'hidden'
      WHEN 'active' IN ({own}, {parent}) THEN 'active'
      ELSE 'default' END"""


STATUS_VIEWS = f"""
CREATE VIEW IF NOT EXISTS project_statuses AS
 WITH RECURSIVE tree(id, effective_status) AS (
   SELECT id, workspace_status FROM groups WHERE parent_id IS NULL
   UNION ALL
   SELECT g.id, {inherited_status("g.workspace_status", "parent.effective_status")}
   FROM groups g JOIN tree parent ON parent.id=g.parent_id
 ) SELECT id, effective_status FROM tree;
CREATE VIEW IF NOT EXISTS session_statuses AS
 SELECT s.id, {inherited_status("s.workspace_status", "g.effective_status")} AS effective_status
 FROM sessions s LEFT JOIN project_statuses g ON g.id=s.group_id;
"""

SCHEMA = f"""
CREATE TABLE IF NOT EXISTS groups({PROJECT_COLUMNS});
CREATE TABLE IF NOT EXISTS sessions(
 id TEXT PRIMARY KEY,provider TEXT,account TEXT,source TEXT,cwd TEXT,url TEXT,title TEXT,
 group_id TEXT REFERENCES groups(id),position REAL DEFAULT 0,{STATUS_COLUMN},
 excluded_from TEXT REFERENCES groups(id),
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
        if not self._has_project_parents():
            self._migrate_projects()
        self._migrate_workspace_columns()
        self.db.executescript(STATUS_VIEWS)
        self.db.execute("CREATE UNIQUE INDEX IF NOT EXISTS root_project_name ON groups(name) WHERE parent_id IS NULL")
        self.db.execute("CREATE UNIQUE INDEX IF NOT EXISTS child_project_name ON groups(parent_id, name) WHERE parent_id IS NOT NULL")
        self.db.execute("INSERT OR IGNORE INTO prefs VALUES('baseline',?)",(str(time.time()),))
        self.baseline = float(self.pref("baseline"))

    def _has_project_parents(self):
        return any(column["name"] == "parent_id" for column in self.db.execute("PRAGMA table_info(groups)"))

    def _migrate_projects(self):
        # Rebuild the flat table to replace its global name constraint. Keep the IDs that sessions reference.
        self.db.execute("PRAGMA foreign_keys=OFF")
        try:
            with self.tx():
                if self._has_project_parents():
                    return
                self.db.execute(f"CREATE TABLE nested_groups({PROJECT_COLUMNS})")
                self.db.execute("INSERT INTO nested_groups(id, name) SELECT id, name FROM groups")
                self.db.execute("DROP TABLE groups")
                self.db.execute("ALTER TABLE nested_groups RENAME TO groups")
                if self.db.execute("PRAGMA foreign_key_check").fetchone():
                    raise sqlite3.IntegrityError("Project migration would leave invalid references")
        finally:
            self.db.execute("PRAGMA foreign_keys=ON")

    def _migrate_workspace_columns(self):
        with self.tx():
            project_columns = {column["name"] for column in self.db.execute("PRAGMA table_info(groups)")}
            if "workspace_status" not in project_columns:
                self.db.execute(f"ALTER TABLE groups ADD COLUMN {STATUS_COLUMN}")
            if "position" not in project_columns:
                self.db.execute("ALTER TABLE groups ADD COLUMN position INTEGER NOT NULL DEFAULT 0")
            columns = {column["name"] for column in self.db.execute("PRAGMA table_info(sessions)")}
            if "workspace_status" in columns:
                return
            self.db.execute(f"ALTER TABLE sessions ADD COLUMN {STATUS_COLUMN}")
            self.db.execute("ALTER TABLE sessions ADD COLUMN excluded_from TEXT REFERENCES groups(id)")
            if "hidden" in columns:
                self.db.execute("UPDATE sessions SET workspace_status='hidden' WHERE hidden!=0")

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

    def _project(self, key):
        if not isinstance(key, str):
            raise ValueError("Unknown project")
        row = self.db.execute("SELECT id, name, parent_id, workspace_status FROM groups WHERE id=?", (key,)).fetchone()
        if row is None:
            raise ValueError("Unknown project")
        return row

    def group(self, name=DEFAULT_PROJECT, parent_id=None):
        name=str(name or DEFAULT_PROJECT).strip()[:160] or DEFAULT_PROJECT
        with nullcontext() if self.db.in_transaction else self.tx():
            if parent_id is not None:
                self._project(parent_id)
            self.db.execute(
                "INSERT INTO groups(id, name, parent_id, position) VALUES(?, ?, ?, ?) ON CONFLICT DO NOTHING",
                (uuid.uuid4().hex, name, parent_id, self._new_project_position(parent_id)),
            )
            row = self.db.execute("SELECT id FROM groups WHERE parent_id IS ? AND name=?", (parent_id, name)).fetchone()
            return row["id"]

    def move_group(self, key, parent_id):
        with self.tx():
            project = self._project(key)
            if project["parent_id"] == parent_id:
                return
            if parent_id is not None:
                self._project(parent_id)
                cycle = self.db.execute("""WITH RECURSIVE ancestors(id) AS (
                    SELECT ? UNION
                    SELECT g.parent_id FROM groups g JOIN ancestors a ON g.id=a.id WHERE g.parent_id IS NOT NULL
                  ) SELECT 1 FROM ancestors WHERE id=?""", (parent_id, key)).fetchone()
                if cycle:
                    raise ValueError("A project cannot move into itself or one of its subprojects")
            duplicate = self.db.execute(
                "SELECT id, workspace_status FROM groups WHERE parent_id IS ? AND name=? AND id!=?",
                (parent_id, project["name"], key),
            ).fetchone()
            if duplicate:
                self._clear_empty_project_collision(duplicate, parent_id, project["name"])
            self.db.execute("UPDATE groups SET parent_id=?, position=? WHERE id=?",
                            (parent_id, self._new_project_position(parent_id), key))
            self._cancel_unavailable_deliveries()

    def _new_project_position(self, parent_id):
        last = self.db.execute(
            "SELECT coalesce(max(position),0) FROM groups WHERE parent_id IS ?", (parent_id,)
        ).fetchone()[0]
        return last + 1 if last else 0

    def reorder_group(self, key, sibling_id, side):
        if side not in ("before", "after"):
            raise ValueError("Choose a position before or after another folder")
        with self.tx():
            project = self._project(key)
            sibling = self._project(sibling_id)
            if key == sibling_id or project["parent_id"] != sibling["parent_id"]:
                raise ValueError("Choose a different folder at the same level")
            ids = [row["id"] for row in self.db.execute(
                "SELECT id FROM groups WHERE parent_id IS ? ORDER BY position,name,id", (project["parent_id"],)
            )]
            ids.remove(key)
            ids.insert(ids.index(sibling_id) + (side == "after"), key)
            self.db.executemany("UPDATE groups SET position=? WHERE id=?", enumerate(ids, 1))

    def _clear_empty_project_collision(self, existing, parent_id, name):
        key = existing["id"]
        occupied = self.db.execute("""SELECT 1 FROM groups WHERE parent_id=?
            UNION ALL SELECT 1 FROM sessions WHERE group_id=? OR excluded_from=? LIMIT 1""",
            (key, key, key)).fetchone()
        if existing["workspace_status"] != "excluded" or occupied:
            raise ValueError("A project with that name already exists at the destination")
        # Preserve the empty excluded folder so the move never deletes a restorable project.
        index = 1
        while True:
            suffix = " (empty)" if index == 1 else f" (empty {index})"
            replacement = name[:160-len(suffix)] + suffix
            if not self.db.execute(
                "SELECT 1 FROM groups WHERE parent_id IS ? AND name=?", (parent_id, replacement)
            ).fetchone():
                self.db.execute("UPDATE groups SET name=? WHERE id=?", (replacement, key))
                return
            index += 1

    def set_project_status(self, key, status):
        if status not in WORKSPACE_STATUSES:
            raise ValueError("Unknown workspace status")
        with self.tx():
            project = self._project(key)
            if project["workspace_status"] != status:
                self.db.execute("UPDATE groups SET workspace_status=? WHERE id=?", (status, key))
                self._cancel_unavailable_deliveries()

    def session(self,provider,account,source,group=DEFAULT_PROJECT,title="",cwd="",url=""):
        key=ident(provider,account,source)
        existing = self.db.execute("SELECT id,group_id FROM sessions WHERE id=?", (key,)).fetchone()
        if existing is None and provider == "Codex":
            # App Server can discover a thread before a transcript with its account metadata arrives.
            existing = self.db.execute(
                "SELECT id,group_id FROM sessions WHERE provider='Codex' AND source=? LIMIT 1", (source,)
            ).fetchone()
        if existing is not None:
            key = existing["id"]
        group_id = existing["group_id"] if existing is not None else self.group(group)
        self.db.execute("""INSERT INTO sessions(id,provider,account,source,cwd,url,title,group_id,synced_at)
          VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET synced_at=excluded.synced_at,
          title=CASE WHEN excluded.title!='' THEN excluded.title ELSE sessions.title END,
          url=CASE WHEN excluded.url!='' THEN excluded.url ELSE sessions.url END""",
          (key,provider,account,source,cwd,url,title[:240],group_id,time.time()))
        return key

    def session_row(self,key):
        row=self.db.execute("""SELECT s.*,v.effective_status FROM sessions s JOIN session_statuses v ON v.id=s.id
                              WHERE s.id=?""",(key,)).fetchone()
        if not row: raise ValueError("Unknown session")
        session = dict(row)
        session["hidden"] = session["effective_status"] in SUPPRESSED_STATUSES
        return session

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
            SELECT id,?,?,?,?,CASE WHEN effective_status IN ('default','active') THEN ? ELSE 0 END
            FROM session_statuses WHERE id=?""",
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

    def _set_workspace_status(self, session, status):
        key = session["id"]
        if status == "excluded":
            self.db.execute(
                "UPDATE sessions SET workspace_status=?, excluded_from=group_id, group_id=NULL WHERE id=?",
                (status, key),
            )
        elif session["workspace_status"] == "excluded":
            group = session["excluded_from"] or self.group()
            self.db.execute(
                "UPDATE sessions SET workspace_status=?, group_id=?, excluded_from=NULL WHERE id=?",
                (status, group, key),
            )
        else:
            self.db.execute("UPDATE sessions SET workspace_status=? WHERE id=?", (status, key))

    def _cancel_unavailable_deliveries(self):
        self.db.execute("""UPDATE deliveries SET status='failed', detail='Session was hidden or excluded before dispatch', updated=?
          WHERE status='queued' AND session IN (
            SELECT id FROM session_statuses WHERE effective_status IN ('hidden','excluded')
          )""",(time.time(),))

    def edit(self,key,change):
        with self.tx():
            session = self.session_row(key)
            status = change.get("workspace_status", session["workspace_status"])
            own_hidden = session["workspace_status"] in SUPPRESSED_STATUSES
            if "workspace_status" not in change and "hidden" in change and bool(change["hidden"]) != own_hidden:
                status = "hidden" if change["hidden"] else "default"
            if status not in WORKSPACE_STATUSES:
                raise ValueError("Unknown workspace status")
            if status != session["workspace_status"]:
                self._set_workspace_status(session, status)
            if change.get("read"):
                self.db.execute("UPDATE sessions SET seen=COALESCE((SELECT max(seq) FROM messages WHERE session=?),0) WHERE id=?",(key,key))
            if "title" in change:
                name=str(change["title"]).strip()[:240]
                if name:self.db.execute("UPDATE sessions SET title=? WHERE id=?",(name,key))
            if "group" in change:
                if status == "excluded":
                    raise ValueError("Restore the session before moving it to a project")
                group=self._project(change["group"])["id"]
                ids=[r[0] for r in self.db.execute("SELECT id FROM sessions WHERE group_id=? AND id!=? ORDER BY position,title,id",(group,key))]
                before=change.get("before")
                ids.insert(ids.index(before) if before in ids else len(ids),key)
                self.db.executemany("UPDATE sessions SET group_id=?,position=? WHERE id=?",[(group,n,i) for n,i in enumerate(ids)])
            if status != session["workspace_status"] or "group" in change:
                self._cancel_unavailable_deliveries()

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
          FROM messages m JOIN session_statuses v ON v.id=m.session WHERE m.seq>? AND m.seq<=? AND m.alert=1
          AND v.effective_status IN ('default','active')
          ORDER BY m.seq LIMIT ?""",(after,last,limit))]
        return rows,(rows[-1]['seq'] if len(rows)==limit else last)

    def state(self):
        now=time.time()
        items=[dict(r) for r in self.db.execute("""SELECT s.id,s.provider,s.account,s.title,s.group_id,s.workspace_status,s.state,
         s.latest,s.latest_at,s.url,s.live_at,s.synced_at,s.bridge,s.writable,v.effective_status,
         v.effective_status IN ('default','active') AND
           EXISTS(SELECT 1 FROM messages m WHERE m.session=s.id AND m.seq>s.seen AND m.alert=1) unread
         FROM sessions s JOIN session_statuses v ON v.id=s.id ORDER BY s.group_id,s.position,s.title,s.id""")]
        for i in items:
            i['hidden']=i['effective_status'] in SUPPRESSED_STATUSES
            i['web_live']=bool(i.pop('bridge') and i['live_at']>=now-35)
        return {"sessions":items,"groups":[dict(r) for r in self.db.execute("""SELECT g.*,v.effective_status FROM groups g
                JOIN project_statuses v ON v.id=g.id ORDER BY g.position,g.name,g.id""")],
                "inbox":[dict(r) for r in self.db.execute("""SELECT m.seq,m.session,substr(m.body,1,600) body,m.at,m.seq>s.seen AND m.alert unread
                FROM messages m JOIN sessions s ON s.id=m.session JOIN session_statuses v ON v.id=s.id
                WHERE v.effective_status IN ('default','active') AND m.role='assistant'
                ORDER BY m.at DESC,m.seq DESC LIMIT 150""")],
                "deliveries":[dict(r) for r in self.db.execute("SELECT id,session,status,detail,created,updated FROM deliveries ORDER BY created DESC LIMIT 100")]}
