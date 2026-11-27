import difflib
import hashlib
import json
import re
import sqlite3
import threading
import time
import unicodedata
import uuid
from contextlib import contextmanager, nullcontext

from .classification import CHATGPT_PROJECT_PHRASES, classify_chatgpt_project


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
SUPPRESSED_STATUSES = ("hidden", "excluded", "trashed")
DEFAULT_PROJECT = "Unclassified"
UNDO_LIMIT = 10
WORKSPACE_FIELDS = {
    "groups": ("name", "parent_id", "position", "workspace_status"),
    "sessions": ("group_id", "position", "workspace_status", "excluded_from", "trashed_at", "custom_title"),
    "todos": ("group_id", "position", "completed"),
}
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
 SELECT s.id, CASE WHEN s.trashed_at IS NOT NULL THEN 'trashed'
   ELSE {inherited_status("s.workspace_status", "g.effective_status")} END AS effective_status
 FROM sessions s LEFT JOIN project_statuses g ON g.id=s.group_id;
"""

SCHEMA = f"""
CREATE TABLE IF NOT EXISTS groups({PROJECT_COLUMNS});
CREATE TABLE IF NOT EXISTS sessions(
 id TEXT PRIMARY KEY,provider TEXT,account TEXT,source TEXT,cwd TEXT,url TEXT,title TEXT,custom_title TEXT NOT NULL DEFAULT '',
 group_id TEXT REFERENCES groups(id),position REAL DEFAULT 0,{STATUS_COLUMN},
 excluded_from TEXT REFERENCES groups(id),trashed_at REAL,
 is_new INTEGER NOT NULL DEFAULT 0,unread_snapshot_seq INTEGER NOT NULL DEFAULT 0,
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
CREATE TABLE IF NOT EXISTS workspace_undo(
 seq INTEGER PRIMARY KEY AUTOINCREMENT, entity TEXT NOT NULL, changes TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS todos(
 id TEXT PRIMARY KEY, group_id TEXT NOT NULL REFERENCES groups(id), body TEXT NOT NULL,
 completed INTEGER NOT NULL DEFAULT 0 CHECK(completed IN (0,1)), created_at REAL NOT NULL,
 position INTEGER NOT NULL DEFAULT 0);
CREATE INDEX IF NOT EXISTS todo_group ON todos(group_id);
"""

# Each entry upgrades a database from the version before it. `PRAGMA user_version` records how many have run.
MIGRATIONS = ["""
CREATE VIRTUAL TABLE messages_fts USING fts5(body,content='messages',content_rowid='seq',tokenize='porter unicode61 remove_diacritics 2');
CREATE TRIGGER messages_fts_insert AFTER INSERT ON messages BEGIN
 INSERT INTO messages_fts(rowid,body) VALUES(new.seq,new.body); END;
CREATE TRIGGER messages_fts_delete AFTER DELETE ON messages BEGIN
 INSERT INTO messages_fts(messages_fts,rowid,body) VALUES('delete',old.seq,old.body); END;
CREATE TRIGGER messages_fts_update AFTER UPDATE OF body ON messages BEGIN
 INSERT INTO messages_fts(messages_fts,rowid,body) VALUES('delete',old.seq,old.body);
 INSERT INTO messages_fts(rowid,body) VALUES(new.seq,new.body); END;
INSERT INTO messages_fts(messages_fts) VALUES('rebuild');
"""]

# The same stop words and suffix rules as `words()` in public/router.js, so search and routing agree on a term.
STOPS = set(('the a an to on for at in my me this that from with it is was are be yesterday please work thread status latest then what where did we leave can you should run set change show find update policy project session 계속 오늘 어제 어디까지 상태 확인 현재 부탁 좀 해줘 알려줘 진행해 작업').split())
PARTICLE = re.compile(r'^(.{2,}?)(에서|에게|으로|에는|에서의|의|에|을|를|는|은|이|가|로|까지)$')


def terms(text):
    raw=re.findall(r'[^\W_]+',unicodedata.normalize('NFKC',str(text or '').lower()))
    out=[]
    for w in raw:
        w=re.sub(r'^([a-z]{3,})s$',r'\1',re.sub(r'^([a-z]{3,})ies$',r'\1y',PARTICLE.sub(r'\1',w)))
        if len(w)>1 and w not in STOPS and w not in out:out.append(w)
    return out


class Store:
    def __init__(self, path):
        self.path = path
        self.local = threading.local()
        version=self.db.execute("PRAGMA user_version").fetchone()[0]
        if version>len(MIGRATIONS):raise RuntimeError(f'Database schema version {version} is newer than this Thread Atlas supports')
        self.db.executescript(SCHEMA)
        if not self._has_project_parents():
            self._migrate_projects()
        self._migrate_workspace_columns()
        self.db.executescript(STATUS_VIEWS)
        self.db.execute("CREATE UNIQUE INDEX IF NOT EXISTS root_project_name ON groups(name) WHERE parent_id IS NULL")
        self.db.execute("CREATE UNIQUE INDEX IF NOT EXISTS child_project_name ON groups(parent_id, name) WHERE parent_id IS NOT NULL")
        self.migrate()
        with self.tx():
            first_run = self.db.execute("INSERT OR IGNORE INTO prefs VALUES('baseline',?)", (str(time.time()),)).rowcount
            if first_run and not self.db.execute("SELECT 1 FROM groups UNION ALL SELECT 1 FROM sessions LIMIT 1").fetchone():
                self.db.execute("INSERT OR IGNORE INTO prefs VALUES('folder_setup','offered')")
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
            if "workspace_status" not in columns:
                self.db.execute(f"ALTER TABLE sessions ADD COLUMN {STATUS_COLUMN}")
                self.db.execute("ALTER TABLE sessions ADD COLUMN excluded_from TEXT REFERENCES groups(id)")
                if "hidden" in columns:
                    self.db.execute("UPDATE sessions SET workspace_status='hidden' WHERE hidden!=0")
            if "trashed_at" not in columns:
                self.db.execute("ALTER TABLE sessions ADD COLUMN trashed_at REAL")
                self.db.execute("DROP VIEW IF EXISTS session_statuses")
            if "is_new" not in columns:
                self.db.execute("ALTER TABLE sessions ADD COLUMN is_new INTEGER NOT NULL DEFAULT 0")
            if "unread_snapshot_seq" not in columns:
                self.db.execute("ALTER TABLE sessions ADD COLUMN unread_snapshot_seq INTEGER NOT NULL DEFAULT 0")
            if "custom_title" not in columns:
                self.db.execute("ALTER TABLE sessions ADD COLUMN custom_title TEXT NOT NULL DEFAULT ''")
            if not any(column["name"] == "position" for column in self.db.execute("PRAGMA table_info(todos)")):
                self.db.execute("ALTER TABLE todos ADD COLUMN position INTEGER NOT NULL DEFAULT 0")

    @property
    def db(self):
        if not hasattr(self.local,"db"):
            db=sqlite3.connect(self.path, isolation_level=None,timeout=20)
            db.row_factory=sqlite3.Row
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("PRAGMA foreign_keys=ON")
            self.local.db=db
        return self.local.db

    def migrate(self):
        with self.tx():
            version=self.db.execute("PRAGMA user_version").fetchone()[0]
            if version==len(MIGRATIONS):return
            statement=''
            for line in ''.join(MIGRATIONS[version:]).splitlines(keepends=True):
                statement+=line
                if sqlite3.complete_statement(statement):self.db.execute(statement);statement=''
            self.db.execute(f"PRAGMA user_version={len(MIGRATIONS)}")

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

    def _workspace_rows(self, table, key):
        fields = ", ".join(WORKSPACE_FIELDS[table])
        query = f"SELECT id, {fields} FROM {table}"
        rows = self.db.execute(query) if key is None else self.db.execute(query + " WHERE id=?", (key,))
        return {row["id"]: tuple(row)[1:] for row in rows}

    @contextmanager
    def _workspace_edit(self, table, key=None):
        with self.tx():
            before = self._workspace_rows(table, key)
            yield
            after = self._workspace_rows(table, key)
            previous = {}
            for row_id in before.keys() & after.keys():
                if before[row_id] != after[row_id]:
                    previous[row_id] = {field: old for field, old, new in
                        zip(WORKSPACE_FIELDS[table], before[row_id], after[row_id]) if old != new}
            created = sorted(after.keys() - before.keys())
            if previous or created:
                changes = json.dumps({"previous": previous, "created": created}, ensure_ascii=False)
                self.db.execute("INSERT INTO workspace_undo(entity, changes) VALUES(?, ?)", (table, changes))
                self.db.execute("DELETE FROM workspace_undo WHERE seq <= ("
                    "SELECT seq FROM workspace_undo ORDER BY seq DESC LIMIT 1 OFFSET ?)", (UNDO_LIMIT,))

    def undo_workspace_edit(self, expected):
        with self.tx():
            action = self.db.execute("SELECT * FROM workspace_undo ORDER BY seq DESC LIMIT 1").fetchone()
            if action is None or type(expected) is not int or expected != action["seq"]:
                raise ValueError("Workspace changed. Try Undo again.")
            table = action["entity"]
            changes = json.loads(action["changes"])
            # Move a folder out of the way before restoring a placeholder's name at its destination.
            rows = sorted(changes["previous"].items(), key=lambda row: "parent_id" not in row[1])
            blocked = False
            self.db.execute("SAVEPOINT undo_restore")
            try:
                for row_id, previous in rows:
                    fields = [field for field in WORKSPACE_FIELDS[table] if field in previous]
                    assignments = ", ".join(f"{field}=?" for field in fields)
                    values = [previous[field] for field in fields]
                    self.db.execute(f"UPDATE {table} SET {assignments} WHERE id=?", (*values, row_id))
                created = changes["created"]
                if created and table in ("groups", "todos"):
                    placeholders = ",".join("?" for _ in created)
                    self.db.execute(f"DELETE FROM {table} WHERE id IN ({placeholders})", created)
            except sqlite3.IntegrityError:
                # Leave the workspace as it is, but drop the entry so it does not block every earlier Undo.
                self.db.execute("ROLLBACK TO undo_restore")
                blocked = True
            self.db.execute("RELEASE undo_restore")
            if table != "todos" and not blocked:
                self._cancel_unavailable_deliveries()
            self.db.execute("DELETE FROM workspace_undo WHERE seq=?", (expected,))
        if blocked:
            raise ValueError("Undo could not restore the previous folder layout because a folder is now in use. "
                             "That change was removed from Undo.")

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

    def create_project(self, name, parent_id):
        with self._workspace_edit("groups"):
            return self.group(name, parent_id)

    def finish_folder_setup(self, paths):
        folders = {}
        if paths is not None:
            if not isinstance(paths, list) or not paths or len(paths) > 128:
                raise ValueError("Choose between 1 and 128 folders")
            for path in paths:
                if (not isinstance(path, list) or not path or len(path) > 128 or
                        any(not isinstance(name, str) or not 0 < len(name.strip()) <= 160 for name in path)):
                    raise ValueError("Folder names must have 1 to 160 characters")
                names = tuple(name.strip() for name in path)
                for depth in range(1, len(names) + 1):
                    folders.setdefault(names[:depth], None)
            folders.setdefault((DEFAULT_PROJECT,), None)
            if len(folders) > 128:
                raise ValueError("Choose between 1 and 128 folders")

        with self._workspace_edit("groups"):
            if self.pref("folder_setup") != "offered":
                return
            for path in folders:
                folders[path] = self.group(path[-1], folders.get(path[:-1]))
            self.db.execute("UPDATE prefs SET value='done' WHERE key='folder_setup'")

    def add_todo(self, group_id, body):
        if not isinstance(body, str) or not 0 < len(body.strip()) <= 400:
            raise ValueError("Enter a to-do of 1 to 400 characters")
        key = uuid.uuid4().hex
        with self._workspace_edit("todos", key):
            self._project(group_id)
            self.db.execute("""INSERT INTO todos(id, group_id, body, created_at, position)
                VALUES(?, ?, ?, ?, (SELECT COALESCE(max(position),0)+1 FROM todos WHERE group_id=?))""",
                (key, group_id, body.strip(), time.time(), group_id))
        return key

    def set_todo_completed(self, key, completed):
        if not isinstance(key, str):
            raise ValueError("Unknown to-do")
        if not isinstance(completed, bool):
            raise ValueError("Expected a boolean to-do status")
        with self._workspace_edit("todos", key):
            found = self.db.execute("UPDATE todos SET completed=? WHERE id=?", (completed, key))
            if not found.rowcount:
                raise ValueError("Unknown to-do")

    def move_todo(self, key, group_id, neighbor=None, side="before"):
        if not isinstance(key, str):
            raise ValueError("Unknown to-do")
        if side not in ("before", "after") or (neighbor is not None and not isinstance(neighbor, str)):
            raise ValueError("Choose a valid to-do position")
        with self._workspace_edit("todos"):
            todo = self.db.execute("SELECT group_id, completed FROM todos WHERE id=?", (key,)).fetchone()
            if todo is None:
                raise ValueError("Unknown to-do")
            self._project(group_id)
            rows = self.db.execute("SELECT id, completed FROM todos WHERE group_id=? ORDER BY position, created_at, id",
                                   (group_id,)).fetchall()
            previous = [row["id"] for row in rows]
            order = [item for item in previous if item != key]
            position = len(order)
            if neighbor is not None:
                if neighbor not in order or next(row["completed"] for row in rows if row["id"] == neighbor) != todo["completed"]:
                    raise ValueError("Choose another to-do with the same checked status")
                position = order.index(neighbor) + (side == "after")
            order.insert(position, key)
            if order != previous:
                self.db.executemany("UPDATE todos SET group_id=?,position=? WHERE id=?",
                                    ((group_id, index, item) for index, item in enumerate(order, 1)))

    def move_group(self, key, parent_id):
        with self._workspace_edit("groups"):
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
        with self._workspace_edit("groups"):
            project = self._project(key)
            sibling = self._project(sibling_id)
            if key == sibling_id or project["parent_id"] != sibling["parent_id"]:
                raise ValueError("Choose a different folder at the same level")
            previous = [row["id"] for row in self.db.execute(
                "SELECT id FROM groups WHERE parent_id IS ? ORDER BY position,name,id", (project["parent_id"],)
            )]
            ids = previous.copy()
            ids.remove(key)
            ids.insert(ids.index(sibling_id) + (side == "after"), key)
            if ids != previous:
                self.db.executemany("UPDATE groups SET position=? WHERE id=?", enumerate(ids, 1))

    def _clear_empty_project_collision(self, existing, parent_id, name):
        key = existing["id"]
        occupied = self.db.execute("""SELECT 1 FROM groups WHERE parent_id=?
            UNION ALL SELECT 1 FROM sessions WHERE group_id=? OR excluded_from=?
            UNION ALL SELECT 1 FROM todos WHERE group_id=? LIMIT 1""", (key, key, key, key)).fetchone()
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
        with self._workspace_edit("groups", key):
            project = self._project(key)
            if project["workspace_status"] != status:
                self.db.execute("UPDATE groups SET workspace_status=? WHERE id=?", (status, key))
                self._cancel_unavailable_deliveries()

    def session(self,provider,account,source,group=DEFAULT_PROJECT,title="",cwd="",url="",*,user_prompts=()):
        key=ident(provider,account,source)
        existing = self.db.execute("SELECT id,group_id FROM sessions WHERE id=?", (key,)).fetchone()
        if existing is None and provider == "Codex":
            # App Server can discover a thread before a transcript with its account metadata arrives.
            existing = self.db.execute(
                "SELECT id,group_id FROM sessions WHERE provider='Codex' AND source=? LIMIT 1", (source,)
            ).fetchone()
        if existing is not None:
            key = existing["id"]
            group_id = existing["group_id"]
        else:
            group_id = None
            if provider == "ChatGPT" and group == DEFAULT_PROJECT:
                projects = self.db.execute("""SELECT g.id, g.name, g.parent_id FROM groups g
                    JOIN project_statuses v ON v.id=g.id WHERE v.effective_status IN ('default','active')""")
                group_id = classify_chatgpt_project(projects, self.pref(CHATGPT_PROJECT_PHRASES, "{}"), title, user_prompts)
            group_id = group_id or self.group(group)
        self.db.execute("""INSERT INTO sessions(id,provider,account,source,cwd,url,title,group_id,synced_at,is_new)
          VALUES(?,?,?,?,?,?,?,?,?,1) ON CONFLICT(id) DO UPDATE SET synced_at=excluded.synced_at,
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
        Returns True when a message was inserted.
        """
        if role not in ('assistant','user') or not isinstance(body,str) or not body.strip():return False
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
        if found is None:return False
        matched.add(found);seen['last']=found
        if initial and at and not estimated:
            # A page read on connect stores history without times. A later export fills them in where it agrees.
            self.db.execute("UPDATE messages SET at=? WHERE seq=? AND at=0",(float(at),found))
        if role=='assistant':
            seen['tail']=(found,added,estimated)
            if initial and not added:
                seen['known_reply']=found
        return added

    def finish_batch(self,sid,seen,*,mark_reply=False):
        """Settle the latest reply after a page read or an export, by conversation order rather than timestamps.

        The batch's final reply becomes the latest only when the batch is the first view of the session or that reply
        was new. A snapshot whose final reply is already known adds nothing after it, so it cannot replace a newer one.
        """
        tail=seen.get('tail')
        if not tail:return
        seq,added,estimated=tail
        row=self.db.execute("SELECT body,at FROM messages WHERE seq=?",(seq,)).fetchone()
        if row:
            if mark_reply and added:
                previous = self.db.execute("SELECT latest, latest_at FROM sessions WHERE id=?", (sid,)).fetchone()
                anchor = self.db.execute("SELECT substr(body,1,2200) FROM messages WHERE seq=?",
                                         (seen.get('known_reply'),)).fetchone()
                follows_known_reply = anchor is not None and anchor[0] == previous['latest']
                older = not estimated and row['at'] and previous['latest_at'] and row['at'] < previous['latest_at']
                newer = not estimated and previous['latest_at'] and row['at'] > previous['latest_at']
                if not older and (not previous['latest'] or newer or follows_known_reply):
                    self.db.execute("UPDATE sessions SET unread_snapshot_seq=max(unread_snapshot_seq, ?) WHERE id=?", (seq,sid))
            self.db.execute("""UPDATE sessions SET latest=?,latest_at=? WHERE id=? AND (?=1 OR latest='')
                AND (? OR latest_at=0 OR ?=0 OR latest_at<=?)""",
                (row['body'][:2200],row['at'],sid,int(added),estimated,row['at'],row['at']))
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
        self.db.execute("""UPDATE deliveries SET status='failed', detail='Session became unavailable before dispatch', updated=?
          WHERE status='queued' AND session IN (
            SELECT id FROM session_statuses WHERE effective_status IN ('hidden','excluded','trashed')
          )""",(time.time(),))

    def set_session_trashed(self, key, trashed):
        if not isinstance(trashed, bool):
            raise ValueError("Expected a boolean Trash state")
        with self._workspace_edit("sessions", key):
            self.session_row(key)
            if trashed:
                self.db.execute("UPDATE sessions SET trashed_at=COALESCE(trashed_at, ?) WHERE id=?", (time.time(), key))
                self._cancel_unavailable_deliveries()
            else:
                self.db.execute("UPDATE sessions SET trashed_at=NULL WHERE id=?", (key,))

    def edit(self,key,change):
        if "group" in change:
            transaction = self._workspace_edit("sessions")
        elif {"workspace_status", "hidden", "title"} & change.keys():
            transaction = self._workspace_edit("sessions", key)
        else:
            transaction = self.tx()
        with transaction:
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
                through = change.get("read_through")
                if through is not None and (type(through) is not int or not 0 <= through < 2**63):
                    raise ValueError("Invalid read position")
                self.db.execute("""UPDATE sessions SET is_new=0,seen=max(seen,COALESCE(
                    (SELECT max(seq) FROM messages WHERE session=? AND (? IS NULL OR seq<=?)),0)) WHERE id=?""",
                    (key, through, through, key))
            if "title" in change:
                title=change["title"]
                if title is not None and (not isinstance(title,str) or not 0<len(title.strip())<=240):
                    raise ValueError("Enter a thread name of 1 to 240 characters")
                self.db.execute("UPDATE sessions SET custom_title=? WHERE id=?", (title.strip() if title is not None else "", key))
            if "group" in change:
                if status == "excluded":
                    raise ValueError("Restore the session before moving it to a project")
                group=self._project(change["group"])["id"]
                previous=[r[0] for r in self.db.execute("SELECT id FROM sessions WHERE group_id=? ORDER BY position,title,id",(group,))]
                ids=[sid for sid in previous if sid != key]
                before=change.get("before")
                if before != key or key not in previous:
                    ids.insert(ids.index(before) if before in ids else len(ids),key)
                    if ids != previous:
                        self.db.executemany("UPDATE sessions SET group_id=?,position=? WHERE id=?",[(group,n,i) for n,i in enumerate(ids)])
            if status != session["workspace_status"] or "group" in change:
                self._cancel_unavailable_deliveries()

    def history(self,key,seq=None):
        """The latest 200 messages, or the 200 around `seq` when the thread is opened at an older message."""
        self.session_row(key)
        target=seq is not None and self.db.execute("SELECT at,seq FROM messages WHERE session=? AND seq=?",(key,seq)).fetchone()
        if not target:
            return [dict(r) for r in self.db.execute("""SELECT seq,role,body,at FROM
              (SELECT role,body,at,seq FROM messages WHERE session=? ORDER BY at DESC,seq DESC LIMIT 200)
              ORDER BY at,seq""",(key,))]
        at,seq=target
        return [dict(r) for r in self.db.execute("""SELECT seq,role,body,at FROM
          (SELECT * FROM (SELECT role,body,at,seq FROM messages WHERE session=? AND (at<? OR at=? AND seq<=?) ORDER BY at DESC,seq DESC LIMIT 100)
           UNION ALL SELECT * FROM (SELECT role,body,at,seq FROM messages WHERE session=? AND (at>? OR at=? AND seq>?) ORDER BY at,seq LIMIT 100))
          ORDER BY at,seq""",(key,at,at,seq,key,at,at,seq))]

    def search(self,text,provider=None):
        """Visible sessions ranked by their best-matching message. Each comes with that message's excerpt, the query
        terms it contains, and whether it is the session's latest reply."""
        words=terms(text)
        if not words:return []
        # English terms are matched by their Porter stem; other terms, such as Korean, by prefix, so particles still
        # attached to the indexed word ("정책은") do not stop the bare term ("정책") from matching.
        match=lambda ws:' OR '.join('"'+w+'"'+('' if w.isascii() else '*') for w in ws)
        def ranked(query,order):
            best={}
            rows=self.db.execute(f"""SELECT m.session,m.seq,m.role,m.at FROM messages_fts f JOIN messages m ON m.seq=f.rowid
              JOIN sessions s ON s.id=m.session WHERE messages_fts MATCH ? AND (? IS NULL OR s.provider=?)
              ORDER BY {order}""",(query,provider,provider))
            for r in rows:
                if r['session'] in best or r['session'] in suppressed:continue
                best[r['session']]=dict(r)
                if len(best)==20:break
            rows.close()
            return best
        # The queries below read one snapshot, so an edit in between cannot drop a ranked message's excerpt.
        self.db.execute("BEGIN")
        try:
            # Hidden, excluded and trashed threads, including those inside a hidden or excluded folder, are left out.
            suppressed={r[0] for r in self.db.execute(
                f"SELECT id FROM session_statuses WHERE effective_status IN ({','.join('?'*len(SUPPRESSED_STATUSES))})",SUPPRESSED_STATUSES)}
            # Terms in over half the messages are skipped unless the rest find nothing.
            total=self.db.execute("SELECT count(*) FROM messages").fetchone()[0]
            distinct=[w for w in words if 2*self.db.execute("SELECT count(*) FROM messages_fts WHERE messages_fts MATCH ?",(match([w]),)).fetchone()[0]<=total]
            query=match(distinct)
            best=ranked(query,'f.rank') if distinct else {}
            if not best and distinct!=words:query=match(words);best=ranked(query,'f.rowid DESC')
            found=list(best.values());seqs=[b['seq'] for b in found];marks=','.join('?'*len(seqs))
            excerpts=dict(self.db.execute(f"""SELECT rowid,snippet(messages_fts,0,'','','…',32) FROM messages_fts
              WHERE messages_fts MATCH ? AND rowid IN ({marks})""",(query,*seqs)))
            held={w:{r[0] for r in self.db.execute(f"SELECT rowid FROM messages_fts WHERE messages_fts MATCH ? AND rowid IN ({marks})",(match([w]),*seqs))} for w in words}
            # The session keeps the latest reply's text and time, so the assistant message with both is that reply.
            latest={r[0] for r in self.db.execute(f"""SELECT (SELECT m.seq FROM messages m WHERE m.session=s.id AND +m.role='assistant'
              AND m.at=s.latest_at AND substr(m.body,1,2200)=s.latest ORDER BY m.seq DESC LIMIT 1) FROM sessions s WHERE s.id IN ({marks})""",list(best))}
        finally:self.db.execute("COMMIT")
        for b in found:
            b.update(snippet=excerpts[b['seq']],terms=[w for w in words if b['seq'] in held[w]],latest=b['seq'] in latest)
        return found

    def alerts_after(self,after):
        limit=100
        last=self.db.execute("SELECT COALESCE(max(seq),0) FROM messages").fetchone()[0]
        if after is None:return [],last
        rows=[dict(r) for r in self.db.execute("""SELECT m.seq,m.session,m.at,substr(m.body,1,2400) body
          FROM messages m JOIN session_statuses v ON v.id=m.session WHERE m.seq>? AND m.seq<=? AND m.alert=1
          AND v.effective_status IN ('default','active')
          ORDER BY m.seq LIMIT ?""",(after,last,limit))]
        return rows,(rows[-1]['seq'] if len(rows)==limit else last)

    def recent_replies(self, provider=None, session=None):
        return [dict(row) for row in self.db.execute("""SELECT m.seq,m.session,substr(m.body,1,600) body,m.at,
            m.seq>s.seen AND (m.alert OR m.seq=s.unread_snapshot_seq) unread
            FROM messages m JOIN sessions s ON s.id=m.session JOIN session_statuses v ON v.id=s.id
            WHERE v.effective_status IN ('default','active') AND m.role='assistant'
              AND (? IS NULL OR s.provider=?) AND (? IS NULL OR s.id=?)
            ORDER BY m.at DESC,m.seq DESC LIMIT 150""", (provider, provider, session, session))]

    def state(self):
        now=time.time()
        items=[dict(r) for r in self.db.execute("""SELECT s.id,s.provider,s.account,s.source,
         COALESCE(NULLIF(s.custom_title,''),s.title) title,s.custom_title,s.group_id,s.workspace_status,s.trashed_at,s.state,
         s.latest,s.latest_at,s.url,s.live_at,s.synced_at,s.bridge,s.writable,s.is_new,v.effective_status,
         CASE WHEN v.effective_status IN ('default','active') THEN
           max(CASE WHEN s.unread_snapshot_seq>s.seen THEN s.unread_snapshot_seq ELSE 0 END,
               COALESCE((SELECT max(m.seq) FROM messages m
                         WHERE m.session=s.id AND m.seq>s.seen AND m.alert=1),0))
           ELSE 0 END unread_through
         FROM sessions s JOIN session_statuses v ON v.id=s.id ORDER BY s.group_id,s.position,s.title,s.id""")]
        for i in items:
            i['hidden']=i['effective_status'] in SUPPRESSED_STATUSES
            i['unread']=int(i['unread_through']>0)
            i['web_live']=bool(i.pop('bridge') and i['live_at']>=now-35)
            source=i.pop('source')
            if i['provider']=='Codex' and not i['url'] and source:
                try:thread_id=uuid.UUID(source)
                except ValueError:pass
                else:i['url']=f'codex://threads/{thread_id}'
        undo = self.db.execute("SELECT max(seq) AS id, count(*) AS count FROM workspace_undo").fetchone()
        return {"sessions":items,"undo":dict(undo),"folder_setup":self.pref("folder_setup") == "offered",
                "groups":[dict(r) for r in self.db.execute("""SELECT g.*,v.effective_status FROM groups g
                JOIN project_statuses v ON v.id=g.id ORDER BY g.position,g.name,g.id""")],
                "todos":[dict(r) for r in self.db.execute("SELECT * FROM todos ORDER BY completed, position, created_at, id")],
                "inbox":self.recent_replies(),
                "deliveries":[dict(r) for r in self.db.execute("SELECT id,session,status,detail,created,updated FROM deliveries ORDER BY created DESC LIMIT 100")]}
