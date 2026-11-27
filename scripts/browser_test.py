"""Real Chromium extension + local HTTP server, with isolated provider DOM fixtures."""
import io
import json
import os
import re
import socket
import subprocess
import sys
import tempfile
import time
import zipfile
from datetime import datetime
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


def exported_chat(key, title, *replies):
    mapping = {}
    parent = None
    for index, text in enumerate(replies):
        node = f'{key}-reply-{index}'
        mapping[node] = {'parent': parent, 'message': {
            'id': node, 'author': {'role': 'assistant'}, 'content': {'parts': [text]},
            'create_time': 1735689600 + index,
        }}
        parent = node
    return {'conversation_id': key, 'title': title, 'current_node': parent, 'mapping': mapping}
def activate(page,origin,*names):
    """Earlier steps leave other threads Active, so a step marks the threads it imports Active to have them matched."""
    sessions=page.request.get(origin+'/api/state').json()['sessions']
    for name in names:
        sid=next(i['id'] for i in sessions if i['title']==name)
        assert page.request.post(origin+'/api/edit',data={'id':sid,'workspace_status':'active'}).ok


def full_history_lookup(page,origin,token):
    """A lookup finds a thread by an older message, shows it with its time, and View source opens the history there."""
    # The live fixture titled 'Payment retry policy' would match by title. Hiding it leaves only the older message to match.
    titled=next(i for i in page.request.get(origin+'/api/state').json()['sessions'] if i['title']=='Payment retry policy')
    assert page.request.post(origin+'/api/edit',data={'id':titled['id'],'hidden':True}).ok
    older='Agreed payment retry policy: retry failed charges three times with exponential backoff.'
    thread=[{'uuid':'billing-0','sender':'human','text':older,'created_at':'2025-03-14T12:00:00Z'}]+[
        {'uuid':'billing-'+str(n),'sender':'assistant' if n%2 else 'human','text':'CSV export step '+str(n)+' is done.',
         'created_at':'2025-03-15T00:%02d:00Z'%n} for n in range(1,41)]
    decoy=[{'uuid':'invoice-1','sender':'assistant','text':'The payment button is now green.','created_at':'2025-03-16T00:00:00Z'}]
    assert page.request.post(origin+'/api/import',data={'provider':'Claude','account':'History','data':[
        {'uuid':'billing-sync','name':'Billing sync notes','chat_messages':thread},
        {'uuid':'invoice-layout','name':'Invoice layout','chat_messages':decoy}]}).ok
    activate(page,origin,'Billing sync notes','Invoice layout')
    page.locator('[data-view=map]').click();expect(page.locator('.card').filter(has_text='Billing sync notes')).to_have_count(1,timeout=10000)
    page.locator('#prompt').fill('Where did we leave the payment retry policy?');page.locator('#prompt').press('Enter')
    answer=page.locator('#chat .answer').last
    expect(answer).to_contain_text('Billing sync notes');expect(answer).to_contain_text(older)
    when=datetime.fromtimestamp(datetime.fromisoformat('2025-03-14T12:00:00+00:00').timestamp())
    expect(answer).to_contain_text(re.compile(rf'{when:%b} {when.day}, {when.hour%12 or 12}:{when:%M}\s?{when:%p}'))
    link=answer.locator('[data-seq]');expect(link).to_have_count(1);seq=link.get_attribute('data-seq');link.click()
    match=page.locator('#dialog .history section.match')
    expect(match).to_have_attribute('data-seq',seq);expect(match).to_contain_text(older);expect(match).to_be_in_viewport()
    page.locator('#closeDialog').click()


def claude_thread(uuid,name,*messages):
    """One Claude export conversation, a day per message from April 1, 2025."""
    return {'uuid':uuid,'name':name,'chat_messages':[{'uuid':uuid+'-'+str(n),'sender':role,'text':text,'created_at':'2025-04-%02dT12:00:00Z'%(n+1)}
                                                     for n,(role,text) in enumerate(messages)]}


def show(page,origin,*threads):
    assert page.request.post(origin+'/api/import',data={'provider':'Claude','account':'History','data':list(threads)}).ok
    activate(page,origin,*(t['name'] for t in threads))
    page.locator('[data-view=map]').click()
    for t in threads:expect(page.locator('.card').filter(has_text=t['name'])).to_have_count(1,timeout=10000)


def ask(page,text):
    """Submit a request and return the dashboard's answer to it."""
    page.locator('#prompt').fill(text);page.locator('#prompt').press('Enter')
    return answer_to(page,text)


def answer_to(page,text):
    answer=page.locator('#chat .utter').filter(has_text=text).last.locator('xpath=following-sibling::div[contains(@class,"answer")][1]')
    expect(answer).to_have_count(1);return answer


def older_match_stays_unread(page,origin,token):
    """Opening the history at an older matched message leaves the thread's newer reply unread."""
    older='Agreed tax nexus: register in the three new states by June.'
    sid=page.request.post(origin+'/bridge/sync',headers={'Authorization':'Bearer '+token},data={
        'provider':'Claude','account':'Personal','source':'state-filings','tab':'tab-state-filings-0001','title':'State filings',
        'messages':[{'id':'filing-1','role':'user','text':older,'at':'2025-04-01T12:00:00Z'},
                    {'id':'filing-2','role':'assistant','text':'Drafted the cover letter.'}]}).json()['session']
    unread=lambda:next(i for i in page.request.get(origin+'/api/state').json()['sessions'] if i['id']==sid)['unread']
    activate(page,origin,'State filings')
    assert unread()
    page.locator('[data-view=map]').click();expect(page.locator('.card').filter(has_text='State filings')).to_have_count(1,timeout=10000)
    answer=ask(page,'Where did we leave the tax nexus?');expect(answer).to_contain_text(older)
    answer.locator('[data-seq]').click();expect(page.locator('#dialog .history section.match')).to_contain_text(older)
    page.locator('#closeDialog').click()
    assert unread()


def far_apart_terms_route(page,origin,token):
    """Routing counts every query word in the matched message, not only the words in its excerpt."""
    filler=' '.join(['and then we compared every number in the quarterly sheet with the bank export line by line']*4)
    show(page,origin,claude_thread('month-end','Month-end notes',('human','The ledger '+filler+' cutoff is the 25th.'),('assistant','Archived.')),
         claude_thread('release-calendar','Release calendar',('human','The cutoff for the release moved to Monday.'),('assistant','Updated.')))
    answer=ask(page,'Where did we leave the ledger cutoff?')
    expect(answer).to_contain_text('Month-end notes');expect(answer).not_to_contain_text('Which thread')


def latest_reply_identity(page,origin,token):
    """The matched message is shown apart from the latest reply exactly when it is a different message."""
    older='Webhook timeout is now 30 seconds.'
    filler=' '.join('Paragraph %d lists the folders we moved.'%n for n in range(80))
    show(page,origin,claude_thread('webhooks','Webhook settings',('human','What should the limit be?'),('assistant',older),('human','Anything else?'),
                                   ('assistant','Recap: '+older+' I also renamed the queue workers and moved the on-call dashboards.')),
         claude_thread('archive','Archive notes',('human','Write up the notes.'),('assistant',filler+' The archive retention window is ninety days.')))
    # An older reply whose text the latest reply repeats is still shown, with its own time and link.
    answer=ask(page,'Where did we leave the webhook timeout?')
    expect(answer.locator('.quote')).to_have_count(2);expect(answer.locator('.quote').first).to_contain_text('Matched message')
    expect(answer.locator('.quote').first).to_contain_text(older);expect(answer.locator('.quote').first.locator('[data-seq]')).to_have_count(1)
    # A latest reply that matched past the stored cut is shown once, as the matched message with its link.
    answer=ask(page,'Where did we leave the archive retention window?')
    expect(answer).to_contain_text('Archive notes');expect(answer.locator('.quote')).to_have_count(1)
    expect(answer.locator('.quote')).to_contain_text('ninety days');answer.locator('[data-seq]').click()
    expect(page.locator('#dialog .history section.match')).to_contain_text('The archive retention window is ninety days.')
    page.locator('#closeDialog').click()


def older_match_choices(page,origin,token):
    """Threads matched only by older messages are offered as choices, never sent to unasked."""
    cadence='Dunning cadence: reminders go out every three days.'
    show(page,origin,claude_thread('accounts','Accounts follow-up',('human',cadence),('assistant','Sent the summary.')),
         claude_thread('customer-mail','Customer emails',('human',cadence),('assistant','Fixed the footer.')),
         claude_thread('payouts','Vendor payouts',('human','The escheatment threshold is 300 dollars.'),('assistant','Booked.')))
    page.locator('#prompt').fill('Change the dunning cadence to weekly reminders');expect(page.locator('#dest')).to_contain_text('Choose a destination')
    page.locator('#prompt').press('Enter');answer=answer_to(page,'Change the dunning cadence to weekly reminders')
    expect(answer).to_contain_text('Which thread did you mean?');expect(answer.locator('.choice')).to_have_count(2)
    expect(answer.locator('.choice').filter(has_text='Accounts follow-up')).to_have_count(1);expect(answer.locator('.choice').filter(has_text='Customer emails')).to_have_count(1)
    page.locator('#prompt').fill('Change the escheatment threshold to 500 dollars');expect(page.locator('#dest')).to_contain_text('Confirm before sending · ')
    expect(page.locator('#dest')).to_contain_text('Vendor payouts')
    page.locator('#prompt').press('Enter');answer=answer_to(page,'Change the escheatment threshold to 500 dollars')
    expect(answer).to_contain_text('Which thread did you mean?');expect(answer.locator('.choice')).to_have_count(1)
    expect(answer.locator('.choice')).to_contain_text('Vendor payouts')


def provider_scoped_lookup(page,origin,token):
    """A request that names a provider searches that provider's history, so stronger matches elsewhere cannot crowd it out."""
    def chatgpt(n):
        node=lambda key,role,text,parent:{'message':{'id':key,'author':{'role':role},'content':{'parts':[text]},'create_time':1743500000+n},'parent':parent}
        return {'conversation_id':'vendor-note-'+str(n),'title':'Vendor note '+str(n),'current_node':'b',
                'mapping':{'a':node('a','user','Escrow audit, escrow audit.',None),'b':node('b','assistant','Escrow audit done.','a')}}
    assert page.request.post(origin+'/api/import',data={'provider':'ChatGPT','account':'Vendors','data':[chatgpt(n) for n in range(21)]}).ok
    activate(page,origin,*('Vendor note '+str(n) for n in range(21)))
    older=('Before the quarter closes we still need to finish the escrow audit, which covers the vendor deposit accounts, '
           'the clearing balances against the bank statements, and the sign-off from finance.')
    show(page,origin,claude_thread('quarterly-close','Quarterly close',('human',older),('assistant','Booked the meeting.')))
    answer=ask(page,'Where did Claude leave the escrow audit?')
    expect(answer).to_contain_text('Quarterly close');expect(answer).to_contain_text(older)


def live_thread(page,origin,token,source,title,*messages):
    """A connected, writable Claude tab, so the dashboard can send to it directly."""
    return page.request.post(origin+'/bridge/sync',headers={'Authorization':'Bearer '+token},data={
        'provider':'Claude','account':'Personal','source':source,'tab':'tab-'+source+'-0001','title':title,'writable':True,
        'messages':[{'id':source+'-'+str(n),'role':role,'text':text} for n,(role,text) in enumerate(messages)]}).json()['session']


def sent(page,origin,sid):
    """How many messages the dashboard has sent to a session."""
    return sum(d['session']==sid for d in page.request.get(origin+'/api/state').json()['deliveries'])


def pending_lookup_keeps_target(page,origin,token):
    """Choosing a destination while a lookup is still searching does not turn the lookup into a send there."""
    older='Agreed vendor escalation matrix: page the account owner first.'
    show(page,origin,claude_thread('vendor-escalation','Vendor contacts',('human',older),('assistant','Saved the contact sheet.')))
    sid=live_thread(page,origin,token,'standup-notes','Standup notes',('user','Collect the notes.'),('assistant','Collected the notes.'))
    expect(page.locator('.card').filter(has_text='Standup notes')).to_have_count(1,timeout=10000)
    held=[]
    page.route('**/api/search*',lambda r:held.append(r))
    question='Where did we leave the vendor escalation matrix?'
    page.locator('#prompt').fill(question);page.locator('#prompt').press('Enter')
    page.locator('#chatTarget').select_option(sid)
    expect(page.locator('#mode')).to_have_value('send')
    # The dashboard searches before it answers. Wait up to 5 s for that search to be held, then let it through.
    for _ in range(100):
        if held:break
        page.wait_for_timeout(50)
    for r in held:r.continue_()
    page.unroute('**/api/search*')
    answer=answer_to(page,question)
    expect(answer).to_contain_text('Vendor contacts');expect(answer).to_contain_text(older)
    assert sent(page,origin,sid)==0
    page.locator('#chatTarget').select_option('')


def long_draft_lookup_and_send(page,origin,token):
    """Drafts longer than a URL allows still find a thread by an older message, and still send to a chosen destination."""
    older='Agreed chargeback evidence checklist: receipts, shipping proof and the customer emails.'
    show(page,origin,claude_thread('chargebacks','Dispute handling',('human',older),('assistant','Filed the template.')))
    question='Where did we leave the chargeback evidence checklist? '+'x'*9000
    page.locator('#prompt').fill(question);page.locator('#prompt').press('Enter')
    answer=page.locator('#chat .utter').last.locator('xpath=following-sibling::div[contains(@class,"answer")][1]')
    expect(answer).to_contain_text('Dispute handling');expect(answer).to_contain_text(older)
    sid=live_thread(page,origin,token,'release-notes','Release notes',('user','Draft the notes.'),('assistant','Drafted the notes.'))
    expect(page.locator('.card').filter(has_text='Release notes')).to_have_count(1,timeout=10000)
    page.locator('#chatTarget').select_option(sid)
    draft='Please apply this patch. '+'y'*9000
    page.locator('#prompt').fill(draft);page.locator('#prompt').press('Enter')
    expect(page.locator('#chat .answer').last).to_contain_text('Release notes')
    assert sent(page,origin,sid)==1
    page.locator('#chatTarget').select_option('')


STEPS=[full_history_lookup,older_match_stays_unread,far_apart_terms_route,latest_reply_identity,older_match_choices,
       provider_scoped_lookup,pending_lookup_keeps_target,long_draft_lookup_and_send]


def main():
    # Step names given as arguments run only those full-history steps, after the shared checks.
    steps=[next(f for f in STEPS if f.__name__==name) for name in sys.argv[1:]] or STEPS
    with tempfile.TemporaryDirectory() as tmp,sync_playwright() as p:
        d=Path(tmp);ext=ROOT/'extension'
        with socket.socket() as sock:sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
        origin=f'http://127.0.0.1:{port}'
        downloads = d / 'Downloads'
        downloads.mkdir()
        cmd=[sys.executable,'-m','atlas','--data',str(d/'data'),'--codex-dir',str(d/'none1'),'--claude-dir',str(d/'none2'),
             '--downloads-dir',str(downloads),'--no-open','--port',str(port)]
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
            legend = page.locator('#borderLegend')
            expect(legend.locator('li')).to_have_text(['Red · Unread reply', 'Blue · Newly added thread'])
            undo = page.get_by_role('button', name='Undo', exact=True)
            expect(undo).to_be_disabled()
            setup_prompt = page.locator('#folderSetupPrompt')
            expect(setup_prompt).to_be_visible()
            setup_prompt.get_by_role('button', name='Set up folders', exact=True).click()
            setup = page.locator('#folderSetupForm')
            research = setup.locator('.setupFolder').first
            research.get_by_role('button',name='Add subfolder under Research',exact=True).click()
            setup.locator('[data-setup-row="1"] input').fill('Benchmarks')
            setup.get_by_role('button', name='Create folders').click()
            expect(setup_prompt).to_be_hidden()
            expect(page.locator('#canvas').get_by_title('Research › Benchmarks',exact=True)).to_be_visible()
            expect(page.locator('#canvas').get_by_title('Unclassified',exact=True)).to_be_visible()
            undo.click()
            expect(page.locator('#canvas .treeProjectRow')).to_have_count(0)
            expect(setup_prompt).to_be_hidden()
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
            initial_sessions = page.request.get(origin+'/api/state').json()['sessions']
            initial_chat = next(thread for thread in initial_sessions if thread['provider'] == 'ChatGPT')
            group = initial_chat['group_id']
            expect(page.locator('#chatTarget option[value=""]')).to_have_text('Match active threads')
            # While nothing is Active, every visible thread can be matched.
            page.locator('#prompt').fill('Send to the Authentication review thread')
            expect(page.locator('#dest')).to_contain_text('Authentication review')
            # Once another thread is Active, a thread that is not is left out of matching.
            other_chat = next(thread for thread in initial_sessions if thread['provider'] == 'Claude')
            assert page.request.post(origin+'/api/edit',data={'id':other_chat['id'],'workspace_status':'active'}).ok
            page.locator('#prompt').fill('Send to the Authentication review thread again')
            expect(page.locator('#dest')).to_contain_text('Payment retry policy')
            expect(page.locator('#dest')).not_to_contain_text('Authentication review')
            page.locator('#prompt').fill('')
            blocked = page.request.post(origin+'/api/send',data={
                'id':'inactive-route-fixture','session':initial_chat['id'],'body':'Do not enqueue this fixture','active_only':True,
            })
            assert blocked.status == 400 and 'no longer Active' in blocked.json()['error']
            assert not page.request.get(origin+'/api/state').json()['deliveries']
            # Undo the status change, so the Undo history the later steps count is unchanged.
            assert page.request.post(origin+'/api/undo',data={'id':page.request.get(origin+'/api/state').json()['undo']['id']}).ok
            assert next(t for t in page.request.get(origin+'/api/state').json()['sessions'] if t['id']==other_chat['id'])['workspace_status']=='default'
            assert page.request.post(origin+'/api/group-status',data={'id':group,'workspace_status':'active'}).ok
            expect(page.locator('.card .workspaceBadge')).to_have_count(2)
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
            alpha = next(thread for thread in info['sessions'] if thread['provider'] == 'ChatGPT')
            beta = next(thread for thread in info['sessions'] if thread['provider'] == 'Claude')
            page.get_by_role('button', name='Activity', exact=True).click()
            activity = page.locator('#activityFeed')
            expect(activity).to_contain_text('ALPHA reply')
            expect(activity).to_contain_text('BETA reply')
            page.locator('#activitySource').select_option('ChatGPT')
            page.locator('#activityThread').select_option(alpha['id'])
            expect(activity).to_contain_text('ALPHA reply')
            expect(activity).not_to_contain_text('BETA reply')
            expect(activity.locator('.activityThread').first).to_have_text('Authentication review')

            page.locator('.card').filter(has_text='Authentication review').click()
            thread_window = page.locator('#dialog')
            expect(thread_window.get_by_role('link', name='Open original')).to_have_attribute('href', alpha['url'])
            thread_window.get_by_role('button', name='Reply here', exact=True).click()
            expect(thread_window.get_by_label('Reply to this thread')).to_be_focused()
            thread_window.get_by_label('Reply to this thread').fill('Send from the thread window')
            thread_window.get_by_role('button', name='Send reply', exact=True).click()
            expect(pages[0].locator('main')).to_contain_text('Send from the thread window', timeout=15000)
            expect(thread_window.locator('#threadHistory section').filter(has_text='ALPHA reply')).to_have_count(2, timeout=15000)
            expect(thread_window.locator('#threadReplyStatus')).to_have_text('Reply received', timeout=15000)
            expect(thread_window).to_be_visible()
            page.locator('#closeDialog').click()
            expect(activity.locator('.activityExcerpt').filter(has_text='ALPHA reply')).to_have_count(2, timeout=15000)
            unread_activity = activity.locator('.activityReply.unreadReply')
            expect(unread_activity).to_have_count(1)
            unread_activity.get_by_role('button', name='Mark as read').click()
            expect(unread_activity).to_have_count(0)
            expect(thread_window).not_to_be_visible()

            page.locator('[data-view=inbox]').click()
            beta_reply = page.locator('#canvas .listitem').filter(has_text='BETA reply')
            beta_reply.get_by_role('button', name='Mark as read').click()
            expect(beta_reply).to_contain_text('BETA reply')
            expect(beta_reply.get_by_role('button', name='Mark as read')).to_have_count(0)
            expect(thread_window).not_to_be_visible()

            assert page.request.post(origin+'/api/group-status',data={'id':group,'workspace_status':'default'}).ok
            page.locator('[data-view=active]').click()
            expect(page.locator('.card')).to_have_count(0)
            page.locator('#chatTarget').select_option(beta['id'])
            page.locator('#prompt').fill('Send from Main chat to the selected destination')
            expect(page.locator('#dest')).to_contain_text('Payment retry policy')
            page.locator('#prompt').press('Enter')
            expect(pages[1].locator('main')).to_contain_text('Send from Main chat to the selected destination', timeout=15000)
            assert 'Send from Main chat to the selected destination' not in pages[0].locator('main').inner_text()
            expect(page.locator('#chat .answer').last).to_contain_text('BETA reply', timeout=15000)
            expect(page.locator('h1')).to_have_text('Active')
            expect(thread_window).not_to_be_visible()
            expect(page.locator('#chatTarget')).to_have_value(beta['id'])
            page.locator('#chatTarget').select_option('')
            page.get_by_role('button', name='Activity', exact=True).click()
            page.locator('#activitySource').select_option('')
            page.locator('[data-view=map]').click()
            # ZIP import goes through the actual settings dialog and file picker.
            archive=d/'export.zip'
            with zipfile.ZipFile(archive,'w') as z:z.writestr('conversations.json',json.dumps([{'uuid':'fixture-import','name':'Refund guidelines','chat_messages':[{'uuid':'reply-import','sender':'assistant','text':'A refund takes five business days.','created_at':'2025-01-01T00:00:00Z'}]}]))
            page.locator('#connections').click();page.locator('#provider').select_option('Claude');page.locator('#file').set_input_files(str(archive))
            expect(page.get_by_role('button', name='Import Codex history')).to_be_visible()
            expect(page.locator('#importResult')).to_contain_text('Imported 1')
            page.locator('#closeDialog').click()
            expect(page.locator('.card')).to_have_count(3)
            folder = page.locator('#canvas').get_by_title('Unclassified', exact=True)
            expect(folder.locator('xpath=../..').locator('.card')).to_have_count(3)
            card=page.locator('.card').filter(has_text='Refund guidelines')
            page.locator('#chatTarget').select_option(card.get_attribute('data-card'))
            page.locator('#prompt').fill('Change the refund guidelines')
            expect(page.locator('#dest')).to_contain_text('Read-only')
            page.locator('#prompt').press('Enter');expect(page.locator('#chat')).to_contain_text('Copy draft')
            page.locator('#chatTarget').select_option('')
            card.drag_to(page.locator('#hiddenDrop'));expect(page.locator('.card')).to_have_count(2)
            page.reload();expect(page.locator('#hiddenCount')).to_have_text('1')
            page.locator('#hiddenDrop').click();page.locator('[data-restore]').click()
            page.locator('[data-view=map]').click();expect(page.locator('.card')).to_have_count(3)
            page.once('dialog',lambda dialog:dialog.accept('Release prep'))
            page.locator('#addGroup').click()
            root=page.locator('.treeFolder').filter(has_text='Release prep')
            card.drag_to(root);expect(root.locator('xpath=../..').locator('.card')).to_contain_text('Refund guidelines')

            release_card = root.locator('xpath=../..').locator('.card')
            card_actions = page.get_by_role('group', name='Actions for Refund guidelines', exact=True)
            active_button = card_actions.get_by_role('button', name='Active', exact=True)
            hidden_button = card_actions.get_by_role('button', name='Hidden', exact=True)
            trash_card = page.get_by_role('button', name='Move Refund guidelines to Trash', exact=True)
            undo_count = page.locator('#undoCount')
            changes_before = int(undo_count.inner_text())
            active_button.click()
            expect(active_button).to_have_attribute('aria-pressed', 'true')
            page.get_by_label('Show hidden').check()
            hidden_button.click()
            expect(hidden_button).to_have_attribute('aria-pressed', 'true')
            trash_card.click()
            expect(release_card).to_have_count(0)
            expect(undo_count).to_have_text(str(changes_before + 3))
            page.reload()
            expect(undo_count).to_have_text(str(changes_before + 3))
            undo.click()
            expect(hidden_button).to_have_attribute('aria-pressed', 'true')
            undo.click()
            expect(active_button).to_have_attribute('aria-pressed', 'true')
            undo.click()
            expect(undo_count).to_have_text(str(changes_before))
            expect(release_card).to_contain_text('Refund guidelines')
            page.get_by_label('Show hidden').uncheck()
            expect(active_button).to_have_attribute('aria-pressed', 'false')
            active_button.click()
            expect(active_button).to_have_attribute('aria-pressed', 'true')
            active_button.click()
            expect(active_button).to_have_attribute('aria-pressed', 'false')
            active_button.click()
            expect(active_button).to_have_attribute('aria-pressed', 'true')
            page.locator('[data-view=active]').click()
            expect(page.locator('#canvas .card')).to_have_count(1)
            expect(trash_card.locator('.trashIcon')).to_be_visible()
            trash_card.click()
            expect(release_card).to_have_count(0)
            expect(page.locator('#trashCount')).to_have_text('1')
            undo.click()
            expect(active_button).to_have_attribute('aria-pressed', 'true')
            expect(page.locator('#trashCount')).to_have_text('0')

            trash_card.click()
            expect(page.locator('#trashCount')).to_have_text('1')
            page.reload()
            expect(page.locator('#trashCount')).to_have_text('1')
            page.get_by_label('Show hidden').check()
            expect(card).to_have_count(0)
            page.get_by_label('Show hidden').uncheck()
            page.locator('[data-view=trash]').click()
            entry = page.locator('#canvas .listitem').filter(has_text='Refund guidelines')
            expect(entry).to_contain_text('Release prep')
            entry.get_by_role('button', name='View session').click()
            expect(page.locator('#dialog')).to_contain_text('A refund takes five business days.')
            page.locator('#closeDialog').click()
            entry.get_by_role('button', name='Restore', exact=True).click()
            expect(page.locator('#canvas')).to_contain_text('Trash is empty.')
            page.locator('[data-view=active]').click()
            expect(active_button).to_have_attribute('aria-pressed', 'true')

            hidden_button.click()
            expect(page.locator('#canvas .card')).to_have_count(0)
            page.locator('[data-view=map]').click()
            expect(card).to_have_count(0)
            page.get_by_label('Show hidden').check()
            expect(hidden_button).to_have_attribute('aria-pressed', 'true')
            expect(active_button).to_have_attribute('aria-pressed', 'false')
            page.reload()
            expect(page.get_by_label('Show hidden')).to_be_checked()
            expect(release_card).to_contain_text('Refund guidelines')
            expect(hidden_button).to_have_attribute('aria-pressed', 'true')
            hidden_button.click()
            expect(hidden_button).to_have_attribute('aria-pressed', 'false')
            card.click()
            page.get_by_label('Workspace status').select_option('excluded')
            expect(card).to_have_count(0)
            page.locator('[data-view=excluded]').click()
            expect(page.locator('h1')).to_have_text('Will not consider')
            expect(page.locator('#canvas .listitem')).to_contain_text('Refund guidelines')
            expect(page.locator('#hiddenCount')).to_have_text('0')
            page.get_by_role('button', name='Restore', exact=True).click()
            expect(page.locator('#excludedCount')).to_have_text('0')
            page.locator('[data-view=map]').click()
            page.get_by_label('Show hidden').uncheck()
            expect(release_card).to_contain_text('Refund guidelines')
            expect(release_card.locator('.workspaceBadge')).to_have_count(0)

            page.locator('.card').filter(has_text='Authentication review').drag_to(page.locator('#composer'))
            page.locator('#prompt').fill('Draft to keep')
            page.locator('[data-lang=ko]').click();expect(page.locator('html')).to_have_attribute('lang','ko')
            expect(legend.locator('li').first).to_have_text('빨간색 · 읽지 않은 답변')
            expect(page.locator('#prompt')).to_have_value('Draft to keep')
            page.locator('[data-lang=en]').click();expect(page.locator('#dest')).to_contain_text('Authentication review')
            page.locator('#prompt').fill('');page.locator('#mode').select_option('auto')
            assert page.request.post(origin+'/api/edit',data={'id':alpha['id'],'workspace_status':'active'}).ok
            alpha_actions = page.get_by_role('group', name='Actions for Authentication review', exact=True)
            expect(alpha_actions.get_by_role('button', name='Active')).to_have_attribute('aria-pressed','true')
            page.locator('#prompt').fill('Where did we leave authentication review?');page.locator('#prompt').press('Enter')
            expect(page.locator('#chat .answer').last).to_contain_text('ALPHA')
            assert page.request.post(origin+'/api/edit',data={'id':alpha['id'],'workspace_status':'default'}).ok
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
            batch[0]['name'] += ' ' + 'A long thread title that would otherwise wrap onto multiple lines. ' * 3
            imported=page.request.post(origin+'/api/import',data={'provider':'Claude','account':'Scale testing account with a deliberately long display name','data':batch})
            assert imported.ok
            expect(page.locator('.card')).to_have_count(103,timeout=10000)
            page.locator('#viewAction').click();expect(page.locator('.card')).to_have_count(0)
            page.locator('#viewAction').click();expect(page.locator('.card')).to_have_count(103)
            # Notifications keep working after their replies fall out of the 150-entry inbox window.
            extra=[{'uuid':'old-window-'+str(n),'chat_messages':[{'uuid':'m','sender':'assistant','text':'Later','created_at':'2090-01-01T00:00:00Z'}]} for n in range(160)]
            assert page.request.post(origin+'/api/import',data={'provider':'Claude','account':'Window','data':extra}).ok
            assert notification['seq'] not in [m['seq'] for m in page.request.get(origin+'/api/state').json()['inbox']]
            activity_query = origin+'/api/state?activity_provider=ChatGPT&activity_thread='+alpha['id']
            assert notification['seq'] in [m['seq'] for m in page.request.get(activity_query).json()['activity']]
            old_url=(d/'data/launch.url').read_text().strip()+'#reply-'+str(notification['seq'])
            page.goto(old_url);expect(page.locator('#dialogContent')).to_contain_text(notification['body'])
            assert page.request.post(origin+'/api/edit',data={'id':notification['session'],'hidden':True}).ok
            page.reload();expect(page.locator('h1')).to_have_text('Inbox');expect(page.locator('#dialog')).not_to_be_visible()

            # Create and move a deep project branch through the controls used by the dashboard.
            page.evaluate("history.replaceState(null, '', '/')")
            page.locator('[data-view=map]').click()
            parent = 'Release prep'
            nested = 'AI › Agents › Evaluations › Benchmarks'
            for subproject in nested.split(' › '):
                page.get_by_role('button', name='Project options for ' + parent, exact=True).click()
                page.once('dialog', lambda prompt, name=subproject: prompt.accept(name))
                page.get_by_role('button', name='New subproject', exact=True).click()
                expect(page.get_by_role('button', name='Project options for ' + subproject, exact=True)).to_be_visible()
                parent = subproject
            card = page.locator('.card').filter(has_text='Refund guidelines')
            card.click()
            page.locator('#moveSelect').select_option(label='Release prep › ' + nested)
            deepest = page.get_by_role('button', name='Project options for Benchmarks', exact=True).locator('xpath=../..')
            deepest_card = deepest.locator(':scope > .treeBranch > .treeSession > .threadCard > .card')
            expect(deepest_card).to_contain_text('Refund guidelines')
            page.get_by_role('button', name='Collapse Release prep', exact=True).click()
            expect(deepest).to_have_count(0)
            expect(page.get_by_role('button', name='Expand Release prep', exact=True)).to_be_focused()
            page.get_by_role('button', name='Expand Release prep', exact=True).click()
            expect(deepest_card).to_contain_text('Refund guidelines')
            page.once('dialog', lambda prompt: prompt.accept('Study'))
            page.locator('#addGroup').click()
            expect(page.get_by_role('button', name='Project options for Study')).to_be_visible()
            page.get_by_role('button', name='Project options for AI', exact=True).click()
            expect(page.locator('#parentProject option').filter(has_text='Benchmarks')).to_have_count(0)
            page.locator('#parentProject').select_option(label='Study')
            page.get_by_role('button', name='Move project', exact=True).click()
            leaf = page.locator('#projectList').get_by_title('Study › ' + nested, exact=True)
            expect(leaf).to_be_visible()
            page.reload()
            expect(leaf).to_be_visible()
            leaf.click()
            expect(page.locator('h1')).to_have_text('Benchmarks')
            expect(page.locator('#projectTrail')).to_contain_text('Study › AI › Agents › Evaluations')
            expect(page.locator('.workTree > .treeProject')).to_have_count(1)
            expect(deepest_card).to_contain_text('Refund guidelines')

            work_map = page.locator('[data-view=map]')
            work_map.click()
            map_projects = page.locator('#canvas')
            sidebar_projects = page.locator('#projectList')
            map_projects.get_by_title('Study › AI', exact=True).drag_to(map_projects.get_by_title('Release prep', exact=True))
            moved_leaf = sidebar_projects.get_by_title('Release prep › ' + nested, exact=True)
            expect(moved_leaf).to_be_visible()
            expect(moved_leaf.locator('..').locator('.projectGuide')).to_have_count(4)
            release_tree = map_projects.get_by_title('Release prep', exact=True).locator('xpath=../..')
            direct_subprojects = release_tree.locator(':scope > .treeBranch > .treeProject > .treeProjectRow > .treeFolder')
            expect(direct_subprojects).to_have_text('AI')
            expect(deepest_card).to_contain_text('Refund guidelines')
            sidebar_projects.get_by_title('Release prep', exact=True).drag_to(moved_leaf)
            expect(moved_leaf).to_be_visible()
            map_projects.get_by_title('Study', exact=True).click()
            source = sidebar_projects.get_by_title('Release prep › AI', exact=True)
            destination = sidebar_projects.get_by_title('Study', exact=True)
            destination.scroll_into_view_if_needed()
            source.hover()
            target = destination.bounding_box()
            x, y = target['x'] + target['width'] / 2, target['y'] + target['height'] / 2
            preview = page.locator('#projectDropPreview')
            page.mouse.down()
            try:
                page.mouse.move(x, y, steps=8)
                page.mouse.move(x + 1, y)
                expect(preview).to_have_text('Move AI under Study')
            finally:
                page.mouse.up()
            expect(preview).to_be_hidden()
            expect(leaf).to_be_visible()
            sidebar_projects.get_by_title('Study › AI', exact=True).drag_to(work_map)
            root_leaf = sidebar_projects.get_by_title(nested, exact=True)
            expect(root_leaf).to_be_visible()
            page.reload()
            expect(root_leaf).to_be_visible()
            root_ai = map_projects.get_by_title('AI', exact=True).locator('xpath=../..')
            expect(root_ai.locator(':scope > .treeBranch > .treeProject > .treeProjectRow > .treeFolder')).to_have_text('Agents')
            expect(deepest_card).to_contain_text('Refund guidelines')

            ai_menu = page.get_by_role('button', name='Project options for AI', exact=True)
            ai_menu.click()
            page.get_by_label('Folder status').select_option('active')
            page.locator('[data-view=active]').click()
            expect(root_ai.locator(':scope > .treeProjectRow .workspaceBadge')).to_have_text('Active')
            expect(deepest_card.locator('.workspaceBadge')).to_have_text('Active')
            expect(page.locator('#canvas .card')).to_have_count(1)
            ai_menu.click()
            page.get_by_label('Folder status').select_option('hidden')
            expect(ai_menu).to_have_count(0)
            work_map.click()
            expect(ai_menu).to_have_count(0)
            page.get_by_label('Show hidden').check()
            expect(root_ai.locator(':scope > .treeProjectRow .workspaceBadge')).to_have_text('Hidden')
            expect(deepest_card.locator('.workspaceBadge')).to_have_text('Hidden')
            ai_menu.click()
            page.get_by_label('Folder status').select_option('excluded')
            expect(ai_menu).to_have_count(0)
            page.locator('[data-view=excluded]').click()
            expect(page.locator('#canvas .listitem')).to_contain_text('AI')
            page.get_by_role('button', name='Restore', exact=True).click()
            expect(page.locator('#excludedCount')).to_have_text('0')
            work_map.click()
            page.get_by_label('Show hidden').uncheck()
            expect(deepest_card).to_contain_text('Refund guidelines')
            expect(deepest_card.locator('.workspaceBadge')).to_have_count(0)

            top_folders = page.locator('#canvas > .workTree > .treeProject > .treeProjectRow .treeFolderTitle')
            expect(top_folders).to_have_text(['AI', 'Release prep', 'Study', 'Unclassified'])
            page.get_by_role('button', name='Project options for Study', exact=True).click()
            page.get_by_role('button', name='Move up', exact=True).click()
            expect(top_folders).to_have_text(['AI', 'Study', 'Release prep', 'Unclassified'])
            page.reload()
            expect(top_folders).to_have_text(['AI', 'Study', 'Release prep', 'Unclassified'])
            ai_menu.click()
            page.once('dialog', lambda prompt: prompt.accept('Notes'))
            page.get_by_role('button', name='New subproject', exact=True).click()
            page.get_by_role('button', name='Project options for Notes', exact=True).click()
            expect(page.get_by_role('button', name='Move down', exact=True)).to_be_disabled()
            page.get_by_role('button', name='Move up', exact=True).click()
            child_folders = root_ai.locator(':scope > .treeBranch > .treeProject > .treeProjectRow .treeFolderTitle')
            expect(child_folders).to_have_text(['Notes', 'Agents'])
            expect(page.get_by_role('button', name='Move up', exact=True)).to_be_disabled()
            page.get_by_role('button', name='Move down', exact=True).click()
            expect(child_folders).to_have_text(['Agents', 'Notes'])
            page.locator('#closeDialog').click()

            page.set_viewport_size({'width':390,'height':900})
            work_map.click()
            expect(deepest_card).to_be_visible()
            expect(trash_card).to_be_visible()
            expect(active_button).to_be_visible()
            expect(hidden_button).to_be_visible()
            expect(undo).to_be_visible()
            expect(page.locator('[data-view=trash] .trashIcon')).to_be_visible()
            for width in (320,390,820,1024,1440):
                page.set_viewport_size({'width':width,'height':900})
                heights = page.locator('#canvas .threadCard').evaluate_all(
                    'cards => [...new Set(cards.map(card => card.getBoundingClientRect().height))]'
                )
                assert len(heights) == 1, (width, heights)
                assert page.evaluate('document.documentElement.scrollWidth')<=width

            page.locator('#connections').click()
            refresh_chatgpt = page.get_by_role('button', name='Refresh ChatGPT', exact=True)
            expect(page.get_by_role('link', name='How to export ChatGPT chats')).to_have_attribute(
                'href', 'https://help.openai.com/en/articles/7260999-exporting-your-chatgpt-history-and-data')
            import_result = page.locator('#importResult')
            refresh_chatgpt.click()
            expect(import_result).to_have_text('No ChatGPT export ZIP found in Downloads.')
            page.get_by_label('Account label').fill('Export account')
            first_chat = exported_chat('refresh-chat-1', 'Research planning', 'The first reply.')
            first_archive = downloads / 'older.zip'
            with zipfile.ZipFile(first_archive, 'w') as archive:
                archive.writestr('conversations.json', json.dumps([first_chat]))
            os.utime(first_archive, (1735689600, 1735689600))
            refresh_chatgpt.click()
            expect(import_result).to_have_text('New chats: 1 · New messages: 1')
            state = page.request.get(origin + '/api/state').json()
            original = next(row for row in state['sessions'] if row['account'] == 'Export account')
            release = next(row['id'] for row in state['groups'] if row['name'] == 'Release prep')
            changed = page.request.post(origin + '/api/edit', data={'id': original['id'], 'group': release, 'workspace_status': 'active'})
            assert changed.ok

            updated_chat = exported_chat('refresh-chat-1', 'Research planning', 'The first reply.', 'A later reply.')
            new_chat = exported_chat('refresh-chat-2', 'Release prep retrospective', 'A new conversation.')
            new_archive = downloads / 'newer.zip'
            with zipfile.ZipFile(new_archive, 'w') as archive:
                archive.writestr('conversations-001.json', json.dumps([updated_chat]))
                archive.writestr('conversations-002.json', json.dumps([new_chat]))
            os.utime(new_archive, (1735689700, 1735689700))
            claude_archive = downloads / 'unrelated.zip'
            with zipfile.ZipFile(claude_archive, 'w') as archive:
                archive.writestr('conversations.json', json.dumps([{'uuid': 'ignored', 'chat_messages': []}]))
            os.utime(claude_archive, (1735689800, 1735689800))
            page.reload()
            page.locator('#connections').click()
            expect(page.get_by_label('Account label')).to_have_value('Export account')
            refresh_chatgpt.click()
            expect(import_result).to_have_text('New chats: 1 · New messages: 2')
            expect(import_result).to_have_attribute('title', 'newer.zip')
            refresh_chatgpt.click()
            expect(import_result).to_have_text('No new chats or messages in the latest export.')
            state = page.request.get(origin + '/api/state').json()
            refreshed = [row for row in state['sessions'] if row['account'] == 'Export account']
            assert len(refreshed) == 2
            added = next(row for row in refreshed if row['title'] == 'Release prep retrospective')
            assert added['group_id'] == release
            original = next(row for row in refreshed if row['id'] == original['id'])
            assert (original['group_id'], original['workspace_status']) == (release, 'active')
            assert (original['latest'], original['latest_at']) == ('A later reply.', 1735689601)
            history = page.request.get(origin + '/api/history', params={'id': original['id']}).json()
            assert [row['body'] for row in history] == ['The first reply.', 'A later reply.']

            # Red takes precedence on a fresh thread, and quiet siblings follow both attention states.
            quiet = exported_chat('quiet-topic', 'A quiet thread', 'A checked reply.')
            assert page.request.post(origin + '/api/import', data={
                'provider': 'ChatGPT', 'account': 'Export account', 'data': [quiet],
            }).ok
            state = page.request.get(origin + '/api/state').json()
            quiet_id = next(row['id'] for row in state['sessions'] if row['title'] == 'A quiet thread')
            assert page.request.post(origin + '/api/edit', data={'id': quiet_id, 'group': release, 'read': True}).ok
            page.locator('#closeDialog').click()
            release_folder = page.get_by_role('button', name='Project options for Release prep', exact=True).locator('..')
            release_branch = release_folder.locator('..').locator(':scope > .treeBranch')
            direct_cards = release_branch.locator(':scope > .treeSession > .threadCard')
            expect(direct_cards.locator('.ctitle')).to_have_text(['Research planning', 'Release prep retrospective', 'A quiet thread'])
            expect(direct_cards.nth(0)).to_have_class('threadCard unreadReply')
            expect(direct_cards.nth(1)).to_have_class('threadCard newThread')
            expect(direct_cards.nth(2)).to_have_class('threadCard')
            expect(release_folder).to_have_class('treeProjectRow unreadReply')

            intake = page.request.post(origin + '/api/group', data={'name': 'Intake', 'parent': release}).json()['id']
            updates = page.request.post(origin + '/api/group', data={'name': 'Updates', 'parent': intake}).json()['id']
            assert page.request.post(origin + '/api/edit', data={'id': original['id'], 'group': updates}).ok
            nested_red = release_branch.locator(':scope > li').first.locator(':scope > .treeProjectRow')
            expect(nested_red).to_have_class('treeProjectRow unreadReply')
            expect(page.locator('.treeProjectRow[data-group="'+updates+'"]')).to_have_class('treeProjectRow unreadReply')

            # Updates opens the relevant branches without changing folded folders in Work map.
            release_folder.locator('.treeFolder').click()
            expect(release_folder.locator('.treeFolder')).to_have_attribute('aria-expanded', 'false')
            page.locator('[data-view=updates]').click()
            expect(page.locator('h1')).to_have_text('Updates')
            expect(release_folder.locator('.treeFolder')).to_have_attribute('aria-expanded', 'true')
            expect(page.locator('.card[data-card="'+quiet_id+'"]')).to_have_count(0)
            expect(release_branch.locator('.ctitle')).to_have_text(['Research planning', 'Release prep retrospective'])
            red_update = page.locator(f'.treeSession[data-session="{original["id"]}"]')
            red_update.get_by_role('button', name='Mark as read').click()
            expect(page.locator('#dialog')).not_to_be_visible()
            expect(red_update).to_have_count(0)
            expect(release_folder).to_have_class('treeProjectRow newThread')
            blue_update = page.locator(f'.treeSession[data-session="{added["id"]}"]')
            blue_update.get_by_role('button', name='Mark as read').click()
            expect(page.locator('#dialog')).not_to_be_visible()
            expect(blue_update).to_have_count(0)
            expect(release_folder).to_have_count(0)
            work_map.click()
            expect(release_folder).to_have_class('treeProjectRow')
            expect(release_folder.locator('.treeFolder')).to_have_attribute('aria-expanded', 'false')

            empty_shortcut = release_folder.get_by_role('button', name='To-dos for Release prep', exact=True)
            empty_shortcut.click()
            memo = page.locator('.projectTodoMemo')
            expect(memo).to_contain_text('No to-dos in this project or its subprojects.')
            expect(empty_shortcut).to_have_attribute('aria-expanded', 'true')
            empty_shortcut.press('Escape')
            expect(memo).to_have_count(0)
            expect(empty_shortcut).to_be_focused()
            release_folder.get_by_role('button', name='Project options for Release prep').click()
            page.locator('#dialog').get_by_role('button', name='To-dos', exact=True).click()
            expect(page.locator('#todoScope')).to_have_value(release)
            expect(page.locator('#todoDestination')).to_be_hidden()
            page.locator('#todoText').fill('Review the release notes')
            page.locator('#addTodo').click()
            first_todo = page.get_by_role('checkbox', name='Review the release notes')
            expect(first_todo).to_be_visible()
            work_map.click()
            todo_shortcut = release_folder.get_by_role('button', name='To-dos for Release prep · 1 open', exact=True)
            todo_shortcut.click()
            expect(page.locator('h1')).to_have_text('Work map')
            expect(release_folder.locator('.treeFolder')).to_have_attribute('aria-expanded', 'false')
            expect(memo).to_have_css('background-color', 'rgb(255, 244, 189)')
            memo.get_by_role('checkbox', name='Review the release notes').check()
            expect(empty_shortcut).to_have_attribute('aria-expanded', 'true')
            expect(memo.get_by_role('checkbox', name='Review the release notes')).to_be_checked()
            memo.get_by_role('button', name='Open in To-dos').click()
            expect(page.locator('#todoScope')).to_have_value(release)
            expect(first_todo).to_be_checked()
            undo.click()
            expect(first_todo).not_to_be_checked()
            page.locator('[data-view=todos]').click()
            expect(page.locator('#todoScope')).to_have_value('')
            page.locator('#todoProject').select_option(intake)
            page.locator('#todoText').fill('Summarize the intake')
            page.locator('#addTodo').click()
            second_todo = page.get_by_role('checkbox', name='Summarize the intake')
            expect(second_todo).to_be_visible()
            expect(page.locator('.todoGroup h2')).to_have_text(['Release prep', 'Release prep › Intake'])
            first_todo.check()
            expect(page.locator('#todosCount')).to_have_text('1')
            undo.click()
            expect(first_todo).not_to_be_checked()
            expect(page.locator('#todosCount')).to_have_text('2')
            page.locator('#todoScope').select_option(intake)
            expect(first_todo).to_have_count(0)
            expect(second_todo).to_be_visible()
            page.reload()
            page.locator('[data-view=todos]').click()
            expect(first_todo).to_be_visible()
            expect(second_todo).to_be_visible()
            page.set_viewport_size({'width':320,'height':900})
            assert page.evaluate('document.documentElement.scrollWidth')<=320
            study_id = next(group['id'] for group in state['groups'] if group['name'] == 'Study')
            assert page.request.post(origin + '/api/todo-create', data={
                'group': updates, 'body': 'Review the update details',
            }).ok
            assert page.request.post(origin + '/api/todo-create', data={
                'group': study_id, 'body': 'Read the study notes',
            }).ok
            work_map.click()
            parent_shortcut = release_folder.get_by_role('button', name='To-dos for Release prep · 3 open', exact=True)
            parent_shortcut.click()
            expect(memo).to_be_visible()
            expect(release_folder.locator('.treeFolder')).to_have_attribute('aria-expanded', 'false')
            expect(memo.get_by_role('checkbox', name='Review the release notes')).to_be_visible()
            expect(memo.get_by_role('checkbox', name='Summarize the intake')).to_be_visible()
            expect(memo.get_by_role('checkbox', name='Review the update details')).to_be_visible()
            expect(memo.get_by_role('checkbox', name='Read the study notes')).to_have_count(0)
            expect(memo.locator('h4')).to_have_text(['Intake', 'Updates'])
            indent = memo.locator('.todoGroup').evaluate_all('nodes => nodes.map(node => node.getBoundingClientRect().left)')
            assert indent[0] < indent[1]
            assert page.evaluate('document.documentElement.scrollWidth')<=320
            memo.get_by_role('button',name='Open in To-dos').click()
            expect(memo).to_have_count(0)
            expect(page.locator('#todoScope')).to_have_value(release)
            expect(page.locator('#summary')).to_have_text('3 open to-dos')
            expect(page.get_by_role('checkbox',name='Review the update details')).to_be_visible()

            work_map.click()
            root_shortcut = page.get_by_role('button', name='All to-dos · 4 open', exact=True)
            root_shortcut.click()
            expect(memo.get_by_role('checkbox')).to_have_count(4)
            memo.get_by_role('checkbox',name='Review the update details').check()
            expect(page.locator('#rootTodoCount')).to_have_text('3')
            expect(release_folder.get_by_role('button', name='To-dos for Release prep · 2 open', exact=True)).to_be_visible()
            memo.get_by_role('button',name='Open in To-dos').click()
            expect(page.locator('#todoScope')).to_have_value('')
            expect(page.get_by_role('checkbox',name='Review the update details')).to_be_checked()
            expect(page.get_by_role('checkbox',name='Read the study notes')).to_be_visible()

            page.set_viewport_size({'width':1440,'height':900})
            page.locator('#todoProject').select_option(release)
            page.locator('#todoText').fill('Copy the release link')
            page.locator('#addTodo').click()
            release_tasks = page.locator(f'.todoGroup[data-todo-project="{release}"]')
            study_tasks = page.locator(f'.todoGroup[data-todo-project="{study_id}"]')
            release_handle = release_tasks.get_by_role('button', name='Move to-do · Copy the release link', exact=True)
            release_handle.drag_to(release_tasks.locator('.todoRow').filter(has_text='Review the release notes'),
                                   target_position={'x':28,'y':4})
            expect(release_tasks.locator('.todoItem span')).to_have_text(['Copy the release link', 'Review the release notes'])
            release_handle.drag_to(study_tasks.locator('h2'))
            expect(study_tasks.get_by_role('checkbox', name='Copy the release link')).to_be_visible()
            undo.click()
            expect(release_tasks.get_by_role('checkbox', name='Copy the release link')).to_be_visible()

            page.set_viewport_size({'width':320,'height':900})
            work_map.click()
            page.locator('#rootTodoButton').click()
            memo.get_by_role('button', name='Move to-do · Review the update details', exact=True).click()
            page.locator('#todoMoveProject').select_option(study_id)
            page.locator('#dialog').get_by_role('button', name='Move to-do', exact=True).click()
            expect(memo.locator(f'.todoGroup[data-todo-project="{study_id}"]')
                   .get_by_role('checkbox', name='Review the update details')).to_be_checked()
            undo.click()
            expect(memo.locator(f'.todoGroup[data-todo-project="{updates}"]')
                   .get_by_role('checkbox', name='Review the update details')).to_be_checked()
            memo.get_by_role('button', name='Open in To-dos').click()

            # Unbroken provider labels and titles must not create horizontal scrolling in the chat dialog.
            wide_title = 'VeryLongChatTitle' * 12
            long_reply = 'https://example.com/' + 'segment' * 130 + '\n' + 'Next line of the reply.\n' * 45
            wide_chat = exported_chat('wide-chat', wide_title, long_reply)
            assert page.request.post(origin + '/api/import', data={
                'provider': 'ChatGPT', 'account': 'LongAccountName' * 7, 'data': [wide_chat],
            }).ok
            work_map.click()
            page.locator('.card').get_by_text(wide_title, exact=True).click()
            chat_dialog = page.locator('#dialog')
            expect(chat_dialog).to_be_visible()
            expect(page.locator('#dialogTitle')).to_have_text(wide_title)
            assert chat_dialog.evaluate('node => node.scrollWidth === node.clientWidth')
            assert page.evaluate("getComputedStyle(document.documentElement).overflow === 'hidden'")
            messages = chat_dialog.locator('.history')
            assert messages.evaluate('node => node.scrollWidth === node.clientWidth')
            # The history overflows and scrolls vertically. This is checked directly, because headless Chromium on
            # Linux delivers wheel scrolling too unevenly to time.
            assert messages.evaluate("node => getComputedStyle(node).overflowY === 'auto' && node.scrollHeight > node.clientHeight")
            assert messages.evaluate('node => (node.scrollTop = 0, node.scrollTop = 180, node.scrollTop > 0)')
            messages.hover()
            page.mouse.wheel(300, 0)
            assert chat_dialog.evaluate('node => node.scrollLeft') == 0
            assert messages.evaluate('node => node.scrollLeft') == 0
            assert page.evaluate('window.scrollX') == 0
            rename = chat_dialog.get_by_role('button',name='Rename',exact=True)
            thread_name = chat_dialog.get_by_role('textbox',name='Thread name')
            rename.click()
            expect(thread_name).to_be_focused()
            expect(thread_name).to_have_value(wide_title)
            thread_name.fill('Unsaved name')
            thread_name.press('Escape')
            expect(thread_name).to_be_hidden()
            expect(chat_dialog).to_be_visible()
            expect(page.locator('#dialogTitle')).to_have_text(wide_title)
            rename.click()
            thread_name.fill('Release review notes')
            thread_name.press('Enter')
            expect(page.locator('#dialogTitle')).to_have_text('Release review notes')
            expect(thread_name).to_be_hidden()
            assert chat_dialog.evaluate('node => node.scrollWidth === node.clientWidth')
            page.locator('#closeDialog').click()
            assert page.evaluate("getComputedStyle(document.documentElement).overflow !== 'hidden'")
            assert page.request.post(origin + '/api/import', data={
                'provider': 'ChatGPT', 'account': 'LongAccountName' * 7, 'data': [wide_chat],
            }).ok
            page.reload()
            renamed_card = page.locator('.card').get_by_text('Release review notes', exact=True)
            renamed_card.click()
            expect(page.locator('#dialogTitle')).to_have_text('Release review notes')
            rename.click()
            chat_dialog.get_by_role('button',name='Use original title',exact=True).click()
            expect(page.locator('#dialogTitle')).to_have_text(wide_title)
            page.locator('#closeDialog').click()
            undo.click()
            expect(renamed_card).to_be_visible()

            for step in steps:step(page,origin,token)
            assert not errors,errors
            ctx.close();print('PASS: browser bridge, incremental imports, routing, drag-and-drop, status, undo, Trash, update filtering, project to-dos, authorization, responsive layouts with vertical-only chat scrolling, and full-history lookups and routing.')
        finally:proc.terminate();proc.wait(timeout=20)


if __name__=='__main__':main()
