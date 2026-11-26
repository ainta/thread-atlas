import asyncio
import io
import json
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path

from atlas.codex_history import import_codex_history
from atlas.indexer import import_export,scan,scan_file
from atlas.store import Store
from atlas.server import Service


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);self.store=Store(self.root/'workspace.sqlite')

    def test_flat_projects_migrate_with_existing_ids_and_session_links(self):
        path = self.root / "legacy.sqlite"
        legacy = sqlite3.connect(path)
        try:
            legacy.executescript("""
                CREATE TABLE groups(id TEXT PRIMARY KEY, name TEXT NOT NULL UNIQUE);
                CREATE TABLE sessions(id TEXT PRIMARY KEY, group_id TEXT REFERENCES groups(id), hidden INTEGER DEFAULT 0);
                INSERT INTO groups VALUES('legacy-project', 'Research');
                INSERT INTO sessions(id, group_id) VALUES('legacy-session', 'legacy-project');
                INSERT INTO sessions VALUES('hidden-session', 'legacy-project', 1);
            """)
        finally:
            legacy.close()

        migrated = Store(path)
        self.addCleanup(migrated.db.close)
        self.assertEqual(migrated.group("Research"), "legacy-project")
        self.assertEqual(migrated.session_row("legacy-session")["group_id"], "legacy-project")
        self.assertEqual(migrated.session_row("legacy-session")["workspace_status"], "default")
        hidden = migrated.session_row("hidden-session")
        self.assertEqual((hidden["workspace_status"], hidden["group_id"]), ("hidden", "legacy-project"))
        parent = migrated.db.execute("SELECT parent_id FROM groups WHERE id='legacy-project'").fetchone()
        self.assertIsNone(parent["parent_id"])
        self.assertNotEqual(migrated.group("Research", "legacy-project"), "legacy-project")
        self.assertEqual(migrated.db.execute("PRAGMA foreign_key_check").fetchall(), [])

    def test_move_project_keeps_subtree_and_sessions_and_rejects_cycles(self):
        research = self.store.group("Research")
        study = self.store.group("Study")
        branch = self.store.group("AI", research)
        self.assertEqual(self.store.group("AI", research), branch)
        self.assertNotEqual(self.store.group("AI", study), branch)
        deepest = branch
        for name in ("Agents", "Evaluations", "Benchmarks", "Notes", "Drafts"):
            deepest = self.store.group(name, deepest)
        sid = self.store.session("Codex", "local", "example", "Research")
        self.store.edit(sid, {"group": deepest})

        with self.assertRaisesRegex(ValueError, "subprojects"):
            self.store.move_group(research, deepest)
        with self.assertRaisesRegex(ValueError, "already exists"):
            self.store.move_group(branch, study)
        with self.assertRaisesRegex(ValueError, "Unknown project"):
            self.store.group("Orphan", "missing")

        self.store.move_group(branch, None)
        self.store.move_group(research, study)
        self.store.move_group(branch, research)
        self.assertEqual(self.store.session("Codex", "local", "example", "Research"), sid)
        reopened = Store(self.store.path)
        self.addCleanup(reopened.db.close)
        groups = {g["id"]: g for g in reopened.state()["groups"]}
        self.assertEqual(groups[research]["parent_id"], study)
        self.assertEqual(groups[branch]["parent_id"], research)
        self.assertEqual(sum(g["name"] == "Research" for g in groups.values()), 1)
        self.assertEqual(reopened.session_row(sid)["group_id"], deepest)

    def test_workspace_status_affects_placement_inbox_and_dispatch(self):
        service = Service(self.root / "statuses", self.root, self.root, 12345)
        self.addCleanup(service.lock.close)
        store = service.store
        self.addCleanup(store.db.close)
        project = store.group("Benchmarks", store.group("Research"))
        source = "11111111-1111-1111-1111-111111111111"
        sid = store.session("Codex", "local", source, "Research")
        store.edit(sid, {"group": project, "workspace_status": "active"})
        service.codex_ok = True
        self.assertTrue(service.snapshot()["sessions"][0]["direct"])
        store.message(sid, "first", "assistant", "First reply", time.time())
        self.assertTrue(store.state()["inbox"])
        queued = {"id": "status-queued-0001", "session": sid, "body": "Continue"}
        service.enqueue(queued)

        store.edit(sid, {"workspace_status": "hidden"})
        self.assertEqual(store.session_row(sid)["group_id"], project)
        self.assertFalse(store.state()["inbox"])
        self.assertFalse(store.alerts_after(0)[0])
        self.assertFalse(service.begin_local_delivery(queued["id"]))
        self.assertEqual(service.enqueue(queued)["status"], "failed")
        with self.assertRaisesRegex(ValueError, "hidden"):
            service.enqueue({**queued, "id": "status-hidden-0001"})

        store.edit(sid, {"workspace_status": "excluded"})
        store.edit(sid, {"workspace_status": "excluded"})  # Retrying exclusion keeps the restore location.
        self.assertEqual(store.session("Codex", "local", source, "Research"), sid)
        self.assertIsNone(store.session_row(sid)["group_id"])
        store.message(sid, "later", "assistant", "Later reply", time.time())
        self.assertFalse(store.state()["inbox"])
        self.assertFalse(store.alerts_after(0)[0])
        with self.assertRaisesRegex(ValueError, "excluded"):
            service.enqueue({**queued, "id": "status-excluded-0001"})
        with self.assertRaisesRegex(ValueError, "Restore"):
            store.edit(sid, {"group": project})

        store.edit(sid, {"workspace_status": "default"})
        session = store.session_row(sid)
        self.assertEqual(session["group_id"], project)
        self.assertIsNone(session["excluded_from"])
        self.assertFalse(session["hidden"])
        self.assertEqual([row["body"] for row in store.history(sid)], ["First reply", "Later reply"])
        self.assertEqual(len(store.alerts_after(0)[0]), 1)

    def test_move_past_empty_excluded_placeholder_keeps_both_projects(self):
        research = self.store.group("Research")
        placeholder = self.store.group("Thesis", research)
        self.store.set_project_status(placeholder, "excluded")
        thesis = self.store.group("Thesis")
        session = self.store.session("Codex", "local", "thesis", "Thesis")

        self.store.move_group(thesis, research)
        groups = {group["id"]: group for group in self.store.state()["groups"]}
        self.assertEqual((groups[thesis]["name"], groups[thesis]["parent_id"]), ("Thesis", research))
        self.assertEqual(groups[placeholder]["name"], "Thesis (empty)")
        self.assertEqual(groups[placeholder]["workspace_status"], "excluded")
        self.assertEqual(self.store.session_row(session)["group_id"], thesis)

    def test_reorder_projects_keeps_siblings_and_descendants_in_place(self):
        research = self.store.group("Research")
        study = self.store.group("Study")
        alpha = self.store.group("Alpha", research)
        beta = self.store.group("Beta", research)
        gamma = self.store.group("Gamma", research)
        leaf = self.store.group("Notes", gamma)
        sid = self.store.session("Codex", "local", "thread", "Research")
        self.store.edit(sid, {"group": leaf})
        self.store.reorder_group(gamma, alpha, "before")
        self.store.reorder_group(alpha, beta, "after")
        self.store.move_group(gamma, research)
        new = self.store.group("A new project", research)
        with self.assertRaisesRegex(ValueError, "same level"):
            self.store.reorder_group(gamma, study, "before")

        reopened = Store(self.store.path)
        self.addCleanup(reopened.db.close)
        groups = reopened.state()["groups"]
        self.assertEqual([g["id"] for g in groups if g["parent_id"] == research], [gamma, beta, alpha, new])
        self.assertEqual(next(g["parent_id"] for g in groups if g["id"] == leaf), gamma)
        self.assertEqual(reopened.session_row(sid)["group_id"], leaf)

    def test_codex_history_import_pages_messages_and_skips_existing_threads(self):
        existing = self.store.session("Codex", "original-profile", "existing", "Research")

        class AppServer:
            async def call(self, method, params):
                if method == "thread/list":
                    if "cursor" not in params:
                        return {"data": [{"id": "existing"}, {"id": "history", "name": "Research notes"}],
                                "nextCursor": "last"}
                    return {"data": [{"id": "temporary", "ephemeral": True}], "nextCursor": None}
                if method == "thread/items/list" and params["threadId"] == "history":
                    if "cursor" not in params:
                        items = [
                            {"type": "userMessage", "id": "prompt", "content": [{"type": "text", "text": "Question"}]},
                            {"type": "agentMessage", "id": "note", "text": "Work in progress", "phase": "commentary"},
                        ]
                        return {"data": [{"item": item} for item in items], "nextCursor": "reply"}
                    item = {"type": "agentMessage", "id": "answer", "text": "Answer", "phase": "final_answer"}
                    return {"data": [{"item": item}], "nextCursor": None}
                raise AssertionError(f"Unexpected App Server call: {method}")

        first = asyncio.run(import_codex_history(self.store, AppServer()))
        second = asyncio.run(import_codex_history(self.store, AppServer()))
        self.assertEqual(first, {"added": 1, "already_indexed": 1, "failed": 0, "messages": 2})
        self.assertEqual(second, {"added": 0, "already_indexed": 2, "failed": 0, "messages": 0})
        self.assertEqual(self.store.session("Codex", "local", "existing", "Other"), existing)
        imported = self.store.session("Codex", "original-profile", "history", "Other")
        self.assertEqual(self.store.session_row(imported)["group_id"], self.store.group("Unclassified"))
        self.assertEqual([message["body"] for message in self.store.history(imported)], ["Question", "Answer"])
        self.assertFalse(self.store.alerts_after(0)[0])

    def test_new_local_and_web_sessions_share_unclassified_project(self):
        service = Service(self.root / "default-project", self.root, self.root, 12345)
        self.addCleanup(service.lock.close)
        store = service.store
        self.addCleanup(store.db.close)
        research = store.group("Research")
        codex_path = self.root / "codex.jsonl"
        codex_path.write_text(json.dumps({"type": "session_meta", "payload": {"id": "local", "cwd": "/tmp/some-repo"}}) + "\n")
        scan_file(store, codex_path, "Codex")
        import_export(store, "ChatGPT", "Personal", [{"id": "export", "mapping": {}}])
        live = service.web_sync({"provider": "Claude", "account": "Work", "source": "live-chat",
                                 "tab": "live-tab-000001", "messages": []})
        state = store.state()
        unclassified = next(g["id"] for g in state["groups"] if g["name"] == "Unclassified")
        self.assertEqual({g["name"] for g in state["groups"]}, {"Research", "Unclassified"})
        self.assertEqual(len(state["sessions"]), 3)
        self.assertTrue(all(s["group_id"] == unclassified for s in state["sessions"]))
        store.edit(live["session"], {"group": research})
        import_export(store, "Claude", "Work", [{"uuid": "live-chat", "chat_messages": []}])
        self.assertEqual(store.session_row(live["session"])["group_id"], research)

    def test_project_status_applies_to_its_branch_and_preserves_session_status(self):
        service = Service(self.root / "folders", self.root, self.root, 12345)
        self.addCleanup(service.lock.close)
        store = service.store
        self.addCleanup(store.db.close)
        research = store.group("Research")
        branch = store.group("Agents", research)
        leaf = store.group("Evaluations", branch)
        sid = store.session("Codex", "local", "11111111-1111-1111-1111-111111111111", "Research")
        store.edit(sid, {"group": leaf, "workspace_status": "active"})
        store.set_project_status(research, "active")
        groups = {g["id"]: g for g in store.state()["groups"]}
        self.assertEqual(groups[leaf]["effective_status"], "active")
        self.assertEqual(groups[leaf]["workspace_status"], "default")
        service.codex_ok = True
        draft = {"id": "folder-status-0001", "session": sid, "body": "Continue"}
        service.enqueue(draft)
        store.message(sid, "before", "assistant", "A visible reply", time.time())

        store.set_project_status(branch, "hidden")
        session = store.session_row(sid)
        self.assertEqual((session["workspace_status"], session["effective_status"], session["group_id"]),
                         ("active", "hidden", leaf))
        self.assertFalse(store.state()["inbox"])
        self.assertFalse(store.alerts_after(0)[0])
        self.assertEqual(service.enqueue(draft)["status"], "failed")
        self.assertFalse(service.begin_local_delivery(draft["id"]))

        store.set_project_status(research, "excluded")
        self.assertEqual(store.session_row(sid)["effective_status"], "excluded")
        store.message(sid, "during", "assistant", "A quiet reply", time.time())
        with self.assertRaisesRegex(ValueError, "excluded"):
            service.enqueue({**draft, "id": "folder-status-0002"})

        store.set_project_status(research, "default")
        self.assertEqual(store.session_row(sid)["effective_status"], "hidden")
        store.set_project_status(branch, "default")
        session = store.session_row(sid)
        self.assertEqual((session["workspace_status"], session["effective_status"], session["group_id"]),
                         ("active", "active", leaf))
        self.assertEqual(len(store.alerts_after(0)[0]), 1)

    def test_import_selected_branch_and_accounts(self):
        c={'id':'abc','title':'A','current_node':'good','mapping':{
           'start':{'parent':None,'message':{'id':'q','author':{'role':'user'},'content':{'parts':['Question']}}},
           'good':{'parent':'start','message':{'id':'a','author':{'role':'assistant'},'content':{'parts':['Correct branch']}}},
           'old':{'parent':'start','message':{'id':'old','author':{'role':'assistant'},'content':{'parts':['Old branch']}}}}}
        for account in ('Personal','Personal','Work'):
            import_export(self.store,'ChatGPT',account,[c])
        sessions=self.store.state()['sessions'];self.assertEqual(len(sessions),2)
        for s in sessions:
            self.assertEqual([m['body'] for m in self.store.history(s['id'])],['Question','Correct branch'])
            self.assertFalse(s['unread'])

    def test_claude_export_live_update_and_layout_persist(self):
        c={'uuid':'abc','name':'Retry rules','chat_messages':[{'uuid':'m','sender':'assistant','text':'Limit?','created_at':'2025-01-02T00:00:00Z'}]}
        import_export(self.store,'Claude','Work',[c]);s=self.store.state()['sessions'][0];g=self.store.group('Payments')
        self.store.edit(s['id'],{'hidden':True,'group':g});import_export(self.store,'Claude','Work',[c])
        reopened=Store(self.store.path);updated=reopened.session_row(s['id'])
        self.assertTrue(updated['hidden']);self.assertEqual(g,updated['group_id'])
        self.assertFalse(reopened.state()['inbox'])
        reopened.edit(s['id'],{'hidden':False})
        reopened.message(s['id'],'m2','assistant','New reply',time.time())
        self.assertTrue(reopened.state()['sessions'][0]['unread'])
        reopened.edit(s['id'],{'read':True})
        self.assertFalse(reopened.state()['sessions'][0]['unread'])

    def test_incremental_partial_line_and_no_tool_data(self):
        p=self.root/'thread.jsonl'
        a={'type':'user','sessionId':'local','cwd':str(self.root),'uuid':'one','message':{'content':'Focus on retries'},'timestamp':'2025-01-01T01:00:00Z'}
        b={**a,'type':'assistant','uuid':'two','message':{'content':[{'type':'tool_use','name':'Read'}]}}
        final={**b,'uuid':'three','message':{'content':[{'type':'text','text':'Finished.'}]}}
        p.write_text(json.dumps(a)+'\n'+json.dumps(b)+'\n'+json.dumps(final)[:-2])
        scan_file(self.store,p,'Claude Code');s=self.store.state()['sessions'][0]
        self.assertEqual(len(self.store.history(s['id'])),1)
        with p.open('a') as f:f.write(json.dumps(final)[-2:]+'\n')
        scan_file(self.store,p,'Claude Code');scan_file(self.store,p,'Claude Code')
        self.assertEqual(len(self.store.history(s['id'])),2)

    def test_web_export_alignment_preserves_repeats(self):
        service=Service(self.root/'bridge',self.root,self.root,12345);self.addCleanup(service.lock.close)
        payload={'provider':'Claude','account':'Work','tab':'tab-binding-123456','source':'web1','initial':True,
                 'messages':[{'id':'dom-a','role':'user','text':'Again'},{'id':'dom-b','role':'user','text':'Again'}]}
        sid=service.web_sync(payload)['session']
        export={'uuid':'web1','name':'Demo','chat_messages':[{'uuid':'real-a','sender':'human','text':'Again'},
                {'uuid':'real-b','sender':'human','text':'Again'},{'uuid':'real-c','sender':'assistant','text':'Complete'}]}
        import_export(service.store,'Claude','Work',[export])
        import_export(service.store,'Claude','Work',[export])
        service.web_sync({**payload,'messages':payload['messages']+[{'id':'dom-c','role':'assistant','text':'Complete'}]})
        self.assertEqual([m['body'] for m in service.store.history(sid)].count('Again'),2)
        self.assertEqual(len(service.store.history(sid)),3)

    def test_batched_alerts_include_each_visible_reply_once(self):
        sid=self.store.session('Claude','Personal','one','Work')
        other=self.store.session('ChatGPT','Work','two','Work')
        self.store.edit(other,{'hidden':True})
        _,cursor=self.store.alerts_after(None)
        for n in range(105):
            self.store.message(sid,str(n),'assistant','Reply '+str(n),time.time())
        self.store.message(other,'hidden','assistant','Hidden reply',time.time())
        page1,cursor=self.store.alerts_after(cursor)
        page2,cursor=self.store.alerts_after(cursor)
        self.assertEqual([len(page1),len(page2)],[100,5])
        self.assertEqual(len({m['seq'] for m in page1+page2}),105)
        self.assertEqual(self.store.alerts_after(cursor)[0],[])

    def test_restart_quarantines_an_inflight_send(self):
        service=Service(self.root/'service',self.root,self.root,12345)
        sid=service.store.session('Claude Code','local','one','Work')
        service.claude_ok=True
        draft={'id':'restart-draft-0001','session':sid,'body':'Continue'}
        service.enqueue(draft);service.update(draft['id'],'sending');service.lock.close()
        restarted=Service(self.root/'service',self.root,self.root,12345)
        self.addCleanup(restarted.lock.close)
        self.assertEqual(restarted.enqueue(draft)['status'],'uncertain')
        self.assertEqual(len(restarted.snapshot()['deliveries']),1)

    def test_web_claim_idempotency_and_read_only(self):
        service=Service(self.root/'s',self.root,self.root,12345);self.addCleanup(service.lock.close)
        payload={'provider':'Claude','account':'Personal','source':'test-one','tab':'test-tab-abcdefghijkl',
                 'title':'Test','initial':True,'messages':[{'id':'a1','role':'assistant','text':'Original answer'}]}
        sid=service.web_sync(payload)['session']
        request={'id':'durable-draft-0001','session':sid,'body':'Follow up'}
        with self.assertRaises(ValueError):service.enqueue(request)
        payload['writable']=True;service.web_sync(payload)
        service.enqueue(request);service.enqueue(request)
        with self.assertRaises(ValueError):service.enqueue({**request,'body':'Other request'})
        first=service.web_sync(payload);self.assertEqual(first['job']['body'],'Follow up')
        self.assertIsNone(service.web_sync(payload)['job'])
        with self.assertRaises(ValueError):service.ack({'id':request['id'],'tab':payload['tab'],'source':'wrong','status':'answered'})
        service.ack({'id':request['id'],'tab':payload['tab'],'source':payload['source'],'status':'answered'})
        self.assertEqual(service.snapshot()['deliveries'][0]['status'],'answered')
        payload.update(initial=False,messages=[{'id':'a2','role':'assistant','text':'New response'}])
        service.web_sync(payload);service.web_sync(payload)
        self.assertEqual(len(service.store.history(sid)),2)
        service.store.db.execute("UPDATE sessions SET live_at=0 WHERE id=?",(sid,))
        with self.assertRaises(ValueError):service.enqueue({**request,'id':'durable-draft-0002'})

    def test_narration_before_a_tool_call_is_not_a_reply(self):
        p=self.root/'turn.jsonl'
        base={'type':'assistant','sessionId':'local','cwd':str(self.root),'timestamp':'2025-01-01T01:00:00Z'}
        lines=[{**base,'type':'user','uuid':'q','message':{'content':'Fix the retry test'}},
               {**base,'uuid':'n','message':{'id':'m1','stop_reason':'tool_use','content':[{'type':'text','text':'Let me read the file.'}]}},
               {**base,'uuid':'t','message':{'id':'m1','stop_reason':'tool_use','content':[{'type':'tool_use','name':'Read'}]}},
               {**base,'uuid':'a','message':{'id':'m2','stop_reason':'end_turn','content':[{'type':'text','text':'Fixed.'}]}}]
        p.write_text(''.join(json.dumps(line)+'\n' for line in lines))
        scan_file(self.store,p,'Claude Code');s=self.store.state()['sessions'][0]
        self.assertEqual([m['body'] for m in self.store.history(s['id'])],['Fix the retry test','Fixed.'])
        self.assertEqual(s['latest'],'Fixed.')

    def test_unfamiliar_or_unreadable_transcripts_do_not_stall_the_index(self):
        codex=self.root/'codex'/'2026'/'01'/'01';codex.mkdir(parents=True);claude=self.root/'claude'/'project';claude.mkdir(parents=True)
        meta={'type':'session_meta','payload':{'id':'11111111-1111-1111-1111-111111111111','cwd':str(self.root)}}
        reply={'type':'event_msg','timestamp':'2025-01-01T01:00:00Z','payload':{'type':'item_completed','item':{'type':'AgentMessage','id':'i1','content':[{'type':'text','text':'Done.'}]}}}
        odd=[{'type':'event_msg','payload':['not','a','mapping']},{'type':'event_msg','payload':{'type':'item_completed','item':None}}]
        (codex/'zz-rollout.jsonl').write_text(''.join(json.dumps(line)+'\n' for line in [meta,*odd,reply]))
        (claude/'aa-session.jsonl').write_text(json.dumps({'type':'assistant','sessionId':'22222222-2222-2222-2222-222222222222','cwd':str(self.root),
            'uuid':'u','message':{'content':[{'type':'text','text':None},{'type':'text','text':'Hello'}]},'timestamp':'2025-01-01T01:00:00Z'})+'\n')
        (claude/'mm-folder.jsonl').mkdir()
        scan(self.store,self.root/'codex',self.root/'claude')
        latest=sorted(s['latest'] for s in self.store.state()['sessions'])
        self.assertEqual(latest,['Done.','Hello'])

    def test_one_malformed_conversation_does_not_reject_an_import(self):
        good={'uuid':'good','name':'Kept','chat_messages':[{'uuid':'m','sender':'assistant','text':'Kept reply'}]}
        self.assertEqual(import_export(self.store,'Claude','Work',[{'uuid':'bad','chat_messages':['not a message']},good]),1)
        self.assertEqual([s['title'] for s in self.store.state()['sessions'] if s['latest']],['Kept'])

    def test_formatted_export_aligns_with_the_rendered_page(self):
        service=Service(self.root/'aligned',self.root,self.root,12345);self.addCleanup(service.lock.close)
        export={'uuid':'conv1','name':'Demo','chat_messages':[
            {'uuid':'e1','sender':'human','text':'How do I retry?','created_at':'2025-03-01T00:00:00Z'},
            {'uuid':'e2','sender':'assistant','text':'**Use backoff.**\n\n- start at 1s\n- cap at 30s','created_at':'2025-03-01T00:00:05Z'},
            {'uuid':'e3','sender':'human','text':'Show me','created_at':'2025-03-01T00:01:00Z'},
            {'uuid':'e4','sender':'assistant','text':'Here is the loop:\n\n```python\nfor delay in delays: wait(delay)\n```\n\nThat keeps every attempt bounded.','created_at':'2025-03-01T00:01:05Z'}]}
        import_export(service.store,'Claude','Personal',[export])
        page={'provider':'Claude','account':'Personal','tab':'tab-binding-123456','source':'conv1','initial':True,'messages':[
            {'id':'dom-user-a','role':'user','text':'How do I retry?','at':0},
            {'id':'dom-assistant-b','role':'assistant','text':'Use backoff.\n\nstart at 1s\ncap at 30s','at':0},
            {'id':'dom-user-c','role':'user','text':'Show me','at':0},
            {'id':'dom-assistant-d','role':'assistant','text':'Here is the loop:\n\npython\nCopy\nfor delay in delays: wait(delay)\n\nThat keeps every attempt bounded.','at':0},
            {'id':'dom-user-e','role':'user','text':'Thanks','at':0},
            {'id':'dom-assistant-f','role':'assistant','text':'A different closing answer that the export never contained.','at':0}]}
        sid=service.web_sync(page)['session']
        history=service.store.history(sid)
        self.assertEqual([m['role'] for m in history],['user','assistant','user','assistant','user','assistant'])
        self.assertEqual(history[1]['body'],export['chat_messages'][1]['text'])
        # History first read from a page carries no clock time, so it does not look newer than the last known message.
        self.assertLess(service.store.session_row(sid)['latest_at'],time.time()-3600)
        self.assertFalse(service.snapshot()['sessions'][0]['unread'])
        service.web_sync({**page,'initial':False,'messages':[{'id':'dom-assistant-g','role':'assistant','text':'A live reply','at':0}]})
        self.assertGreater(service.store.session_row(sid)['latest_at'],time.time()-60)

    def test_regenerated_answer_is_kept_as_the_latest(self):
        service=Service(self.root/'regen',self.root,self.root,12345);self.addCleanup(service.lock.close)
        first='Here is a short summary of the release notes. The API now paginates every list endpoint by default.'
        again='Here is a short summary of the release notes. Billing moved to monthly invoices and exports are faster.'
        import_export(service.store,'ChatGPT','Personal',[{'id':'regen1','title':'Notes','current_node':'a','mapping':{
            'q':{'parent':None,'message':{'id':'q','author':{'role':'user'},'content':{'parts':['Summarize the notes']},'create_time':1735689600}},
            'a':{'parent':'q','message':{'id':'a','author':{'role':'assistant'},'content':{'parts':[first]},'create_time':1735689610}}}}])
        sid=service.web_sync({'provider':'ChatGPT','account':'Personal','tab':'regen-tab-abcdefgh','source':'regen1','initial':True,'messages':[
            {'id':'dom-q','role':'user','text':'Summarize the notes'},{'id':'dom-a2','role':'assistant','text':again}]})['session']
        self.assertEqual([m['body'] for m in service.store.history(sid)].count(again),1)
        self.assertEqual(service.store.session_row(sid)['latest'],again)

    def test_an_older_export_does_not_replace_the_latest_reply_from_a_page(self):
        service=Service(self.root/'pagefirst',self.root,self.root,12345);self.addCleanup(service.lock.close)
        page=[{'id':'p1','role':'user','text':'Plan the migration'},{'id':'p2','role':'assistant','text':'Step one is a backup.'},
              {'id':'p3','role':'user','text':'And then?'},{'id':'p4','role':'assistant','text':'Then switch the traffic.'}]
        sid=service.web_sync({'provider':'Claude','account':'Work','tab':'page-first-abcdef','source':'conv9','initial':True,'messages':page})['session']
        self.assertEqual(service.store.session_row(sid)['latest'],'Then switch the traffic.')
        # The export is an older snapshot that also holds messages scrolled out of the page.
        import_export(service.store,'Claude','Work',[{'uuid':'conv9','name':'Migration','chat_messages':[
            {'uuid':'e0','sender':'human','text':'Hello','created_at':'2025-01-01T00:00:00Z'},
            {'uuid':'e1','sender':'assistant','text':'Hi, what are we planning?','created_at':'2025-01-01T00:00:05Z'},
            {'uuid':'e2','sender':'human','text':'Plan the migration','created_at':'2025-01-01T00:01:00Z'},
            {'uuid':'e3','sender':'assistant','text':'Step one is a backup.','created_at':'2025-01-01T00:01:05Z'}]}])
        row=service.store.session_row(sid)
        self.assertEqual(row['latest'],'Then switch the traffic.')
        self.assertEqual(len(service.store.history(sid)),6)
        # A newer export that continues the conversation does move the latest reply.
        import_export(service.store,'Claude','Work',[{'uuid':'conv9','name':'Migration','chat_messages':[
            {'uuid':'e5','sender':'human','text':'And then?','created_at':'2025-01-01T00:02:00Z'},
            {'uuid':'e6','sender':'assistant','text':'Then switch the traffic.','created_at':'2025-01-01T00:02:05Z'},
            {'uuid':'e7','sender':'human','text':'Rollback?','created_at':'2025-01-01T00:03:00Z'},
            {'uuid':'e8','sender':'assistant','text':'Keep the old cluster for a day.','created_at':'2025-01-01T00:03:05Z'}]}])
        self.assertEqual(service.store.session_row(sid)['latest'],'Keep the old cluster for a day.')

    def test_restart_does_not_send_stale_requests_or_leave_delivered_open(self):
        service=Service(self.root/'restart',self.root,self.root,12345)
        sid=service.store.session('Codex','local','11111111-1111-1111-1111-111111111111','Work');service.codex_ok=True
        for key in ('stale-queued-0001','stale-turned-0001'):service.enqueue({'id':key,'session':sid,'body':'Continue'})
        service.update('stale-turned-0001','delivered');service.lock.close()
        restarted=Service(self.root/'restart',self.root,self.root,12345);self.addCleanup(restarted.lock.close)
        status={d['id']:d['status'] for d in restarted.snapshot()['deliveries']}
        self.assertEqual(status,{'stale-queued-0001':'failed','stale-turned-0001':'uncertain'})

    def test_web_queue_waits_for_a_busy_tab_and_stale_claims_expire(self):
        service=Service(self.root/'queue',self.root,self.root,12345);self.addCleanup(service.lock.close)
        payload={'provider':'ChatGPT','account':'Personal','source':'busy-one','tab':'busy-tab-abcdefghijkl','title':'Busy','initial':True,
                 'writable':True,'busy':True,'messages':[{'id':'a1','role':'assistant','text':'Working on it'}]}
        sid=service.web_sync(payload)['session']
        status=lambda key:service.store.db.execute("SELECT status FROM deliveries WHERE id=?",(key,)).fetchone()[0]
        age=lambda key,column,seconds:service.store.db.execute("UPDATE deliveries SET "+column+"=? WHERE id=?",(time.time()-seconds,key))
        service.enqueue({'id':'waits-for-busy-01','session':sid,'body':'Next'});age('waits-for-busy-01','created',200)
        service.sweep();self.assertEqual(status('waits-for-busy-01'),'queued')
        claimed=service.web_sync({**payload,'busy':False,'initial':False,'messages':[]})['job']
        self.assertEqual(claimed['id'],'waits-for-busy-01')
        service.enqueue({'id':'waits-behind-it-1','session':sid,'body':'After that'})
        age('waits-for-busy-01','updated',130);service.sweep()
        self.assertEqual(status('waits-for-busy-01'),'uncertain')
        self.assertEqual(service.web_sync({**payload,'busy':False,'initial':False,'messages':[]})['job']['id'],'waits-behind-it-1')
        service.enqueue({'id':'tab-went-away-01','session':sid,'body':'Later'});age('tab-went-away-01','created',200)
        service.store.db.execute("UPDATE sessions SET live_at=0 WHERE id=?",(sid,));service.sweep()
        self.assertEqual(status('tab-went-away-01'),'failed')

    def test_dispatch_survives_a_locked_database(self):
        service=Service(self.root/'locked',self.root,self.root,12345);self.addCleanup(service.lock.close)
        service.store.db.execute("PRAGMA busy_timeout=20")
        other=sqlite3.connect(service.store.path,isolation_level=None);self.addCleanup(other.close)
        async def run():
            task=asyncio.create_task(service.dispatch())
            other.execute("BEGIN IMMEDIATE");await asyncio.sleep(.5)
            self.assertFalse(task.done());self.assertIn('locked',service.dispatch_note)
            other.execute("COMMIT");await asyncio.sleep(.5)
            self.assertFalse(task.done());self.assertEqual(service.dispatch_note,'')
            task.cancel();await asyncio.gather(task,return_exceptions=True)
        asyncio.run(run())


if __name__=='__main__':unittest.main()
