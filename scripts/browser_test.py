"""Real Chromium extension + local HTTP server, with isolated provider DOM fixtures."""
import io
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import zipfile
from pathlib import Path

from playwright.sync_api import sync_playwright, expect

ROOT=Path(__file__).resolve().parents[1]
FIXTURE="""<!DOCTYPE html><title>TOPIC</title><main id="history">INITIAL</main>
<textarea data-testid="chat-input" placeholder="Message"></textarea><button data-testid="send-button" aria-label="Send message">Send</button>
<script>
const provider='PROVIDER';
function bubble(role,text){
 const d=document.createElement('div');
 d.dataset.messageId=crypto.randomUUID();
 if(provider==='ChatGPT'){
   if(role==='user'){d.className='bg-user-message';const span=document.createElement('span');span.className='whitespace-pre-wrap';span.textContent=text;d.append(span);}
   else {d.className='DilResponseRoot-fixture';const block=document.createElement('p');block.className='MarkdownRoot-fixture';block.textContent=text;d.append(block);}
 }
 else d.dataset.testid=role+'-message';
 if(provider!=='ChatGPT')d.textContent=text;document.querySelector('main').append(d);
}
document.querySelector('button').onclick=()=>{
 const box=document.querySelector('textarea');bubble('user',box.value);box.value='';
 setTimeout(()=>bubble('assistant','MARKER reply from the original '+provider+' fixture.'),250);
};
</script>"""


def main():
    with tempfile.TemporaryDirectory() as tmp,sync_playwright() as p:
        d=Path(tmp);ext=ROOT/'extension'
        with socket.socket() as sock:sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
        origin=f'http://127.0.0.1:{port}'
        cmd=[sys.executable,'-m','atlas','--data',str(d/'data'),'--codex-dir',str(d/'none1'),'--claude-dir',str(d/'none2'),'--no-open','--port',str(port)]
        proc=subprocess.Popen(cmd,cwd=ROOT,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        try:
            ctx=p.chromium.launch_persistent_context(str(d/'browser'),channel='chromium',headless=True,locale='ko-KR',viewport={'width':1400,'height':900},
                args=[f'--disable-extensions-except={ext}',f'--load-extension={ext}'])
            worker=ctx.service_workers[0] if ctx.service_workers else ctx.wait_for_event('serviceworker')
            page=ctx.new_page();errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
            for _ in range(100):
                if (d/'data/launch.url').exists():break
                time.sleep(.1)
            page.goto((d/'data/launch.url').read_text());expect(page.locator('h1')).to_have_text('Work map')
            token=page.evaluate("fetch('/api/pairing').then(r=>r.json()).then(d=>d.token)")
            assert p.request.new_context().get(origin+'/api/state').status==401
            assert page.request.post(origin+'/api/group',data={'name':'Blocked'},headers={'Origin':'https://invalid.example'}).status==403
            assert page.request.post(origin+'/bridge/sync',data={}).status==401
            pages=[]
            for provider,src,topic,marker in [('ChatGPT','atlas-sample-1','Authentication review','ALPHA'),('Claude','atlas-sample-2','Payment retry policy','BETA')]:
                source=ctx.new_page()
                url=('https://chatgpt.com/c/' if provider=='ChatGPT' else 'https://claude.ai/chat/')+src
                initial=('<div data-message-author-role="assistant" data-message-id="initial">Original authentication question?</div>'
                         if provider=='ChatGPT' else '<div data-testid="assistant-message" data-message-id="initial">Original payment question?</div>')
                fixture=FIXTURE.replace('INITIAL',initial).replace('PROVIDER',provider).replace('MARKER',marker).replace('TOPIC',topic)
                source.route(url,(lambda html: lambda r:r.fulfill(body=html,content_type='text/html'))(fixture))
                source.goto(url);pages.append(source)
                # Pair the test tab through the service worker using the same binding and messages as popup.js.
                result=worker.evaluate("""async ({url,pairing})=>{
                  const [tab]=await chrome.tabs.query({url});const identity=await chrome.tabs.sendMessage(tab.id,{type:'identity'});
                  await chrome.storage.session.set({['tab'+tab.id]:{...identity,account:'Personal',pairing,allowed:true,tab:crypto.randomUUID()}});
                  return chrome.tabs.sendMessage(tab.id,{type:'activate'});
                }""",{'url':url,'pairing':str(port)+':'+token})
                assert not result.get('error'),result
            page.bring_to_front()
            expect(page.locator('.card')).to_have_count(2,timeout=12000)
            for idx,(topic,marker) in enumerate([('Authentication review','ALPHA'),('Payment retry policy','BETA')]):
                box=page.locator('#prompt');box.fill('Send a follow-up about '+topic)
                expect(page.locator('#dest')).to_contain_text(topic)
                box.press('Enter')
                expect(pages[idx].locator('main')).to_contain_text(marker+' reply',timeout=15000)
                expect(page.locator('#chat')).to_contain_text('answered',timeout=15000)
                expect(page.locator('.card').filter(has_text=topic)).to_contain_text(marker+' reply',timeout=15000)
                assert pages[idx].locator('main').inner_text().count('Send a follow-up')==1
            info=page.evaluate("fetch('/api/state').then(r=>r.json())")
            assert len(info['deliveries'])==2 and all(x['status']=='answered' for x in info['deliveries'])
            # ZIP import goes through the actual settings dialog and file picker.
            archive=d/'export.zip'
            with zipfile.ZipFile(archive,'w') as z:z.writestr('conversations.json',json.dumps([{'uuid':'fixture-import','name':'Refund guidelines','chat_messages':[{'uuid':'reply-import','sender':'assistant','text':'A refund takes five business days.','created_at':'2025-01-01T00:00:00Z'}]}]))
            page.locator('#connections').click();page.locator('#provider').select_option('Claude');page.locator('#file').set_input_files(str(archive))
            expect(page.locator('#importResult')).to_contain_text('Imported 1')
            page.locator('#closeDialog').click()
            expect(page.locator('.card')).to_have_count(3)
            page.locator('#prompt').fill('Change the refund guidelines')
            expect(page.locator('#dest')).to_contain_text('Read-only')
            page.locator('#prompt').press('Enter');expect(page.locator('#chat')).to_contain_text('Copy draft')
            card=page.locator('.card').filter(has_text='Refund guidelines')
            card.drag_to(page.locator('#hiddenDrop'));expect(page.locator('.card')).to_have_count(2)
            page.reload();expect(page.locator('#hiddenCount')).to_have_text('1')
            page.locator('#hiddenDrop').click();page.locator('[data-restore]').click()
            page.locator('[data-view=map]').click();expect(page.locator('.card')).to_have_count(3)
            page.once('dialog',lambda dialog:dialog.accept('Release prep'))
            page.locator('#addGroup').click()
            root=page.locator('.root').filter(has_text='Release prep')
            card.drag_to(root);expect(page.locator('.group').filter(has=root).locator('.card')).to_contain_text('Refund guidelines')
            page.locator('.card').filter(has_text='Authentication review').drag_to(page.locator('#composer'))
            page.locator('#prompt').fill('Draft to keep')
            page.locator('[data-lang=ko]').click();expect(page.locator('html')).to_have_attribute('lang','ko')
            expect(page.locator('#prompt')).to_have_value('Draft to keep')
            page.locator('[data-lang=en]').click();expect(page.locator('#dest')).to_contain_text('Authentication review')
            page.locator('#prompt').fill('');page.locator('#mode').select_option('auto')
            page.locator('#prompt').fill('Where did we leave authentication review?');page.locator('#prompt').press('Enter')
            expect(page.locator('#chat .answer').last).to_contain_text('ALPHA')
            # A native notification identifies the exact reply, even if a newer answer arrived before the click.
            notification=next(m for m in info['inbox'] if 'ALPHA reply' in m['body'])
            opened=ctx.new_page()
            opened.goto((d/'data/launch.url').read_text().strip()+'#reply-'+str(notification['seq']))
            expect(opened.locator('h1')).to_have_text('Inbox')
            expect(opened.locator('#dialogContent')).to_contain_text(notification['body'])
            expect(opened.locator('#dialogTitle')).to_have_text('Authentication review')
            opened.locator('#closeDialog').click();opened.close()
            if os.environ.get('ATLAS_SCREENSHOT'):page.screenshot(path=os.environ['ATLAS_SCREENSHOT'])
            for width in (390,820,1024,1440):
                page.set_viewport_size({'width':width,'height':900})
                assert page.evaluate('document.documentElement.scrollWidth')<=width
            batch=[{'uuid':'scale-'+str(n),'name':'Scale case '+str(n),'chat_messages':[
                    {'uuid':'m-'+str(n),'sender':'assistant','text':'Fixture '+str(n)}]} for n in range(100)]
            imported=page.request.post(origin+'/api/import',data={'provider':'Claude','account':'Scale','data':batch})
            assert imported.ok
            expect(page.locator('.card')).to_have_count(103,timeout=10000)
            page.locator('#viewAction').click();expect(page.locator('.card')).to_have_count(0)
            page.locator('#viewAction').click();expect(page.locator('.card')).to_have_count(103)
            # Notifications keep working after their replies fall out of the 150-entry inbox window.
            extra=[{'uuid':'old-window-'+str(n),'chat_messages':[{'uuid':'m','sender':'assistant','text':'Later','created_at':'2090-01-01T00:00:00Z'}]} for n in range(160)]
            assert page.request.post(origin+'/api/import',data={'provider':'Claude','account':'Window','data':extra}).ok
            assert notification['seq'] not in [m['seq'] for m in page.request.get(origin+'/api/state').json()['inbox']]
            old_url=(d/'data/launch.url').read_text().strip()+'#reply-'+str(notification['seq'])
            page.goto(old_url);expect(page.locator('#dialogContent')).to_contain_text(notification['body'])
            assert page.request.post(origin+'/api/edit',data={'id':notification['session'],'hidden':True}).ok
            page.reload();expect(page.locator('h1')).to_have_text('Inbox');expect(page.locator('#dialog')).not_to_be_visible()
            assert not errors,errors
            ctx.close();print('PASS: both extension DOM round trips, badge receipts, read-only ZIP import, lookup, routing, drag, hide/restore, reload, language switch, authorization, 4 widths, and a 103-session map.')
        finally:proc.terminate();proc.wait(timeout=20)


if __name__=='__main__':main()
