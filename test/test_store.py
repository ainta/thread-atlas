import asyncio
import io
import json
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path

from atlas.indexer import import_export,scan,scan_file
from atlas.store import Store
from atlas.server import Service


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);self.store=Store(self.root/'workspace.sqlite')

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
