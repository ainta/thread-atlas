"""Incrementally index user-visible text. File offsets commit with the messages."""
import json
import re
import time
from datetime import datetime
from pathlib import Path
from zipfile import BadZipFile, ZipFile

from .store import ident


def epoch(value):
    if isinstance(value,(float,int)): return value/1000 if value>10**11 else value
    try:return datetime.fromisoformat(str(value).replace('Z','+00:00')).timestamp()
    except (TypeError,ValueError):return 0


def plain(content):
    if isinstance(content,str):return content
    if not isinstance(content,list):return ''
    return '\n'.join(str(v.get('text') or '') for v in content if isinstance(v,dict) and str(v.get('type','')).lower() in ('text','input_text','output_text'))


def codex_message(item):
    kind = item.get("type", "").lower()
    if kind not in ("usermessage", "agentmessage") or item.get("phase") == "commentary":
        return None
    body = plain(item.get("content")) or item.get("text", "")
    if not isinstance(body, str) or not body.strip() or body.lstrip().startswith(
            ("<environment_context>", "<local-command", "<INSTRUCTIONS>")):
        return None
    return ("user" if kind == "usermessage" else "assistant"), body


def consume(store,j,meta,kind,path,offset):
    typ=j.get("type")
    ts=epoch(j.get("timestamp"))
    if kind=="Codex":
        p=j.get("payload") or {}
        if typ=="session_meta":
            cwd=p.get("cwd","");source=p.get("id") or p.get("session_id")
            if source:
                meta['sid']=store.session(kind,p.get('creator_account_id') or 'local',source,cwd=cwd)
            return
        sid=meta.get('sid')
        if not sid:return
        tag=p.get('type')
        if typ=='event_msg':
            if tag in ('task_started','task_complete','turn_aborted'):
                state='run' if tag=='task_started' else 'idle'
                store.db.execute("UPDATE sessions SET state=? WHERE id=?",(state,sid))
            if tag=='item_completed':
                item=p.get('item',{})
                message=codex_message(item)
                if message:
                    role,body=message
                    store.message(sid,item.get('id') or ident(ts,body),role,body,ts)
                meta['modern']=True
            elif tag in ('user_message','agent_message') and not meta.get('modern'):
                body=p.get('message','')
                if p.get('phase')!='commentary':store.message(sid,ident(ts,tag,body),'user' if tag=='user_message' else 'assistant',body,ts)
        return
    if typ not in ('assistant','user','custom-title') or j.get('isSidechain') or j.get('isMeta'):return
    if not meta.get('sid'):
        source=j.get('sessionId') or path.stem;cwd=j.get('cwd') or ''
        meta['sid']=store.session(kind,'local',source,cwd=cwd)
    sid=meta['sid']
    if typ=='custom-title':
        store.db.execute("UPDATE sessions SET title=? WHERE id=?",(str(j.get('customTitle',''))[:240],sid));return
    msg=j.get('message');msg=msg if isinstance(msg,dict) else {};content=msg.get('content')
    body=plain(content)
    if not body or body.startswith(('<local-command','<command-name>','<system-reminder>')):return
    # Each content block is written as its own line. Text that leads into a tool call is narration, not the turn's reply.
    partial=typ=='assistant' and (msg.get('stop_reason')=='tool_use' or (isinstance(content,list) and any(v.get('type')=='tool_use' for v in content if isinstance(v,dict))))
    if not partial:store.message(sid,j.get('uuid') or ident(path.name,offset),typ,body,ts)


def scan_file(store,path,kind):
    try:
        stat=path.stat()
        row=store.db.execute("SELECT * FROM files WHERE path=?",(str(path),)).fetchone()
        valid=row and row['inode']==stat.st_ino and row['offset']<=stat.st_size
        offset=row['offset'] if valid else 0;meta=json.loads(row['meta']) if valid else {}
        if offset==stat.st_size:return
        with path.open('rb') as f:
            f.seek(offset)
            while offset<stat.st_size:
                start=offset
                with store.tx():
                    while offset-start<4*1024*1024 and offset<stat.st_size:
                        line=f.readline()
                        if not line.endswith(b'\n'):break
                        pos=offset;offset=f.tell()
                        try:j=json.loads(line)
                        except (ValueError,UnicodeError):continue
                        if not isinstance(j,dict):continue
                        # An unfamiliar record shape is skipped like a malformed line, so it cannot stall the index.
                        try:consume(store,j,meta,kind,path,pos)
                        except (AttributeError,TypeError,KeyError,ValueError):continue
                    store.db.execute("INSERT OR REPLACE INTO files VALUES(?,?,?,?)",(str(path),stat.st_ino,offset,json.dumps(meta)))
                    if meta.get('sid'):store.db.execute('UPDATE sessions SET synced_at=? WHERE id=?',(time.time(),meta['sid']))
                if offset==start or f.tell()!=offset:break
    except FileNotFoundError:pass


def scan(store,codex_dir,claude_dir):
    files=[(p,'Codex') for p in codex_dir.glob('*/*/*/*.jsonl')]
    files.extend((p,'Claude Code') for p in claude_dir.glob('*/*.jsonl'))
    for path,kind in sorted(files,key=lambda pair:pair[0].name,reverse=True):
        # One unreadable transcript is retried on the next pass; the others still advance.
        try:scan_file(store,path,kind)
        except OSError:continue


def import_conversation(store,provider,account,c):
    uid=c.get('conversation_id') or c.get('uuid') or c.get('id')
    if not isinstance(uid,str) or not uid or not all(ch.isalnum() or ch in '-_' for ch in uid):return None
    if provider=='ChatGPT':
        mapping=c.get('mapping')
        if not isinstance(mapping,dict):return None
        tip=c.get('current_node');visited=set();batch=[]
        while tip in mapping and tip not in visited:
            visited.add(tip);node=mapping[tip];batch.append(node);tip=node.get('parent')
        nodes=reversed(batch) if batch else mapping.values()
        msgs=[]
        for n in nodes:
            m=n.get('message') or {};content=m.get('content') or {}
            if (m.get('metadata') or {}).get('is_visually_hidden_from_conversation'):continue
            msgs.append((m.get('id'),(m.get('author') or {}).get('role'),'\n'.join(v for v in content.get('parts') or [] if isinstance(v,str)),epoch(m.get('create_time'))))
        url='https://chatgpt.com/c/'+uid
    else:
        batch=c.get('chat_messages')
        if not isinstance(batch,list):return None
        msgs=[(m.get('uuid'),{'human':'user','assistant':'assistant'}.get(m.get('sender')),m.get('text') or plain(m.get('content')),epoch(m.get('created_at'))) for m in batch]
        url='https://claude.ai/chat/'+uid
    new_chat = store.db.execute("SELECT 1 FROM sessions WHERE id=?", (ident(provider, account, uid),)).fetchone() is None
    user_prompts = (body for _, role, body, _ in msgs if role == 'user')
    sid=store.session(provider,account,uid,title=str(c.get('title') or c.get('name') or ''),url=url,user_prompts=user_prompts)
    seen={}
    new_messages = 0
    for n,(source,role,body,at) in enumerate(msgs):
        if store.merge_message(sid,source or ident(n,role,body,at),role,body,at,True,seen,False):
            new_messages += 1
    store.finish_batch(sid,seen,mark_reply=not new_chat)
    return new_chat, new_messages


def import_export(store,provider,account,data):
    return import_export_report(store, provider, account, data)["count"]


def import_export_report(store, provider, account, data):
    if provider not in ('ChatGPT','Claude') or not isinstance(account,str) or not account.strip():raise ValueError('Choose a provider and account label')
    if isinstance(data,dict):data=data.get('conversations')
    if not isinstance(data,list):raise ValueError('Expected conversations.json containing an array')
    account = account.strip()
    chats = [c for c in data if isinstance(c, dict)]
    count = new_chats = new_messages = 0
    # Short transactions keep the dashboard and the dispatcher writable during a large import. Known messages are
    # skipped on a later import, so a partly applied file can simply be imported again.
    for start in range(0,len(chats),25):
        with store.tx():
            for c in chats[start:start+25]:
                try:
                    imported = import_conversation(store, provider, account, c)
                except (AttributeError, TypeError, KeyError, ValueError):
                    continue
                if imported is not None:
                    added_chat, added_messages = imported
                    count += 1
                    new_chats += added_chat
                    new_messages += added_messages
    if not count:raise ValueError('No compatible conversations found')
    return {"count": count, "new_chats": new_chats, "new_messages": new_messages}


def read_export_archive(archive):
    members = [item for item in archive.infolist() if re.fullmatch(
        r"conversations(?:[-_]?[0-9]+)?\.json", Path(item.filename).name, re.IGNORECASE
    )]
    if not members or sum(item.file_size for item in members) > 256 * 1024**2:
        raise ValueError("Missing or oversized conversations JSON in ZIP")
    conversations = []
    for item in members:
        batch = json.loads(archive.read(item))
        if isinstance(batch, dict):
            batch = batch.get("conversations")
        if not isinstance(batch, list):
            raise ValueError("Invalid export file")
        conversations.extend(batch)
    return conversations


def refresh_chatgpt_downloads(store, downloads, account):
    if not isinstance(account, str) or not account.strip():
        raise ValueError("Enter a ChatGPT account label")
    candidates = []
    try:
        for path in downloads.iterdir():
            if path.suffix.lower() == ".zip" and not path.is_symlink() and path.is_file():
                try:
                    candidates.append((path.stat().st_mtime_ns, path))
                except FileNotFoundError:
                    continue
    except FileNotFoundError:
        return None
    except OSError as error:
        raise ValueError("Thread Atlas could not read the downloads folder") from error

    for _, path in sorted(candidates, reverse=True):
        try:
            with ZipFile(path) as archive:
                conversations = read_export_archive(archive)
        except (BadZipFile, OSError, RuntimeError, ValueError):
            continue
        if any(isinstance(chat, dict) and isinstance(chat.get("mapping"), dict) for chat in conversations):
            return {"file": path.name, **import_export_report(store, "ChatGPT", account, conversations)}
    return None
