"""Create four disposable, model-backed sessions and verify original-thread delivery."""
import argparse
import asyncio
import hashlib
import json
import os
from pathlib import Path
import secrets
import socket
import subprocess
import sys
import tempfile
import uuid

import aiohttp

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from atlas.codex import Codex, output


async def seed_codex(cwd,marker):
    async with Codex() as c:
        thread=(await c.call('thread/start',{'cwd':str(cwd),'sandbox':'read-only','approvalPolicy':'never'}))['thread']
        tid=thread['id']
        turn=(await c.call('turn/start',{'threadId':tid,'input':[{'type':'text','text':f'Remember test marker {marker}. Reply with exactly READY. Use no tools.'}]}))['turn']['id']
        for _ in range(120):
            await asyncio.sleep(1)
            turns=(await c.call('thread/turns/list',{'threadId':tid,'limit':2,'sortDirection':'desc','itemsView':'notLoaded'}))['data']
            if any(t['id']==turn and t['status']=='completed' for t in turns):return tid
        raise RuntimeError('Codex did not complete the seed turn')


async def seed_claude(cwd,marker):
    tid=str(uuid.uuid4())
    result=json.loads(await output('claude','-p','--session-id',tid,'--tools','','--output-format','json',
                                   '--',f'Remember test marker {marker}. Reply with exactly READY.',cwd=str(cwd),timeout=120))
    assert result.get('session_id')==tid and not result.get('is_error')
    return tid


async def verify():
    body='Recall the exact test marker from the earlier message in this thread. Reply only with that marker. Do not use tools.'
    with tempfile.TemporaryDirectory(prefix='atlas-verification-') as temp:
        d=Path(temp);runs=[]
        for provider in ('Codex','Claude Code'):
            for number in (1,2):
                marker=secrets.token_hex(6).upper();cwd=d/(provider.replace(' ','-')+str(number));cwd.mkdir()
                runs.append((provider,number,marker,(seed_codex if provider=='Codex' else seed_claude)(cwd,marker)))
        sources=await asyncio.gather(*(r[3] for r in runs))
        expected={}
        for (provider,no,marker,_),sid in zip(runs,sources):
            if provider=='Codex':
                original=Path(os.environ.get('CODEX_HOME',Path.home()/'.codex'))/'sessions';dest=d/'codex/2026/01/01';pattern='*/*/*/*'+sid+'*.jsonl'
            else:
                original=Path(os.environ.get('CLAUDE_CONFIG_DIR',Path.home()/'.claude'))/'projects';dest=d/'claude'/str(no);pattern='*/'+sid+'.jsonl'
            files=list(original.glob(pattern));assert len(files)==1,(provider,'missing source transcript')
            dest.mkdir(parents=True,exist_ok=True);(dest/files[0].name).symlink_to(files[0]);expected[marker]=(provider,sid)
        with socket.socket() as sock:sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
        proc=subprocess.Popen([sys.executable,'-m','atlas','--port',str(port),'--no-open','--data',str(d/'data'),
                               '--codex-dir',str(d/'codex'),'--claude-dir',str(d/'claude')],cwd=ROOT,stdout=subprocess.DEVNULL)
        try:
            async with aiohttp.ClientSession(cookie_jar=aiohttp.CookieJar(unsafe=True)) as http:
                launch=d/'data/launch.url';url=f'http://127.0.0.1:{port}/api/'
                for _ in range(100):
                    if launch.exists():
                        try:
                            async with http.get(launch.read_text(),allow_redirects=False) as r:r.raise_for_status()
                            break
                        except aiohttp.ClientError:pass
                    await asyncio.sleep(.1)
                async def get(path):
                    async with http.get(url+path) as r:r.raise_for_status();return await r.json()
                for _ in range(60):
                    state=await get('state')
                    if len(state['sessions'])==4 and all(s['direct'] for s in state['sessions']):break
                    await asyncio.sleep(.5)
                assert len(state['sessions'])==4 and all(s['direct'] for s in state['sessions'])
                jobs=[]
                for s in state['sessions']:
                    history=await get('history?id='+s['id'])
                    marker=next(k for k in expected if any(k in m['body'] and m['role']=='user' for m in history))
                    key=str(uuid.uuid4());draft={'id':key,'session':s['id'],'body':body}
                    for _ in range(2):
                        async with http.post(url+'send',json=draft) as r:r.raise_for_status()
                    jobs.append((s['id'],key,marker))
                for _ in range(180):
                    state=await get('state')
                    if len(state['deliveries'])==4 and all(d['status'] in ('answered','failed','uncertain') for d in state['deliveries']):break
                    await asyncio.sleep(1)
                await asyncio.sleep(3);receipts=[]
                for session,key,marker in jobs:
                    history=await get('history?id='+session)
                    status=next(d['status'] for d in state['deliveries'] if d['id']==key)
                    count=sum(m['role']=='user' and body in m['body'] for m in history)
                    answers=[m['body'] for m in history if m['role']=='assistant' and marker in m['body']]
                    assert status=='answered' and count==len(answers)==1,(expected[marker][0],status,count,len(answers))
                    assert all(other not in answers[0] for other in expected if other!=marker)
                    provider,source=expected[marker]
                    receipts.append({'provider':provider,'source_id_sha256':hashlib.sha256(source.encode()).hexdigest(),
                                     'marker':marker,'original_reply':answers[0], 'http_submissions':2,'original_followups':count,'receipt':status})
                return receipts
        finally:proc.terminate();proc.wait(timeout=15)


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--live',action='store_true',help='Use the signed-in Codex and Claude services; creates four test threads and consumes model usage.')
    parser.add_argument('--output',type=Path);args=parser.parse_args()
    if not args.live:parser.error('Supply --live for this opt-in service test')
    proof=json.dumps(asyncio.run(verify()),indent=2)+'\n'
    if args.output:args.output.write_text(proof)
    print(proof,end='')
