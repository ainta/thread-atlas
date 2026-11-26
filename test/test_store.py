import io
import json
import tempfile
import time
import unittest
from pathlib import Path

from atlas.indexer import import_export,scan_file
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


if __name__=='__main__':unittest.main()
