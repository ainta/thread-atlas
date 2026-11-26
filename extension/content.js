// The user connects one URL and one document instance; navigation invalidates the binding.
const instance=crypto.randomUUID();
let connected=false,observedSource='',initial=true,working=false,pending=null,lastSignature='',stableAt=0;
const observed=new Map();
function identity(){
 const route=location.pathname.match(location.hostname==='chatgpt.com'?/^\/(?:g\/[\w-]+\/)?c\/([\w-]+)\/?$/:/^\/chat\/([\w-]+)\/?$/);
 return {provider:location.hostname==='chatgpt.com'?'ChatGPT':'Claude',source:route?.[1],pathname:location.pathname.replace(/\/$/,''),instance};
}
function editor(){
 const query='#prompt-textarea,textarea[data-testid="chat-input"],[contenteditable="true"].ProseMirror,[contenteditable="true"][role="textbox"],textarea[placeholder]';
 return [...document.querySelectorAll(query)].filter(el=>visible(el)&&!el.closest('[data-message-author-role],[class~="group/user-message"]')).at(-1);
}
function visible(el){return Boolean(el&&el.getClientRects().length&&!el.disabled&&el.getAttribute('aria-disabled')!=='true');}
function busy(){return [...document.querySelectorAll('button[data-testid="stop-button"],button[aria-label*="Stop" i],button[aria-label*="중지"],[data-is-streaming="true"]')].some(visible);}
function bubbles(){
 const gpt=identity().provider==='ChatGPT';
 const chatgpt='[data-message-author-role], main .bg-user-message .whitespace-pre-wrap, main [class*="DilResponseRoot-"], main [class*="MarkdownRoot-"]';
 const claude='[data-testid="user-message"],[data-testid="assistant-message"],.font-claude-response';
 const nodes=[...document.querySelectorAll(gpt?chatgpt:claude)].filter(visible);
 const messages=[],repeats=new Map();
 for(const el of nodes){
  if(nodes.some(other=>other!==el&&other.contains(el)))continue;
  const role=gpt?(el.dataset.messageAuthorRole||(el.closest('.bg-user-message')?'user':'assistant')):(el.matches('[data-testid="user-message"]')?'user':'assistant');
  if(!['user','assistant'].includes(role))continue;
  const body=(el.innerText||'').trim();if(!body)continue;
  const parent=el.closest('[data-message-id],[data-message-uuid]');
  // Without a page-provided ID, a bubble is known by its text and how often that text has appeared, not by its
  // position, so loading earlier messages or a re-render does not turn the whole conversation into new messages.
  const mark=role+'-'+hash(body),nth=repeats.get(mark)||0;repeats.set(mark,nth+1);
  const id=parent?.dataset.messageId||parent?.dataset.messageUuid||el.id||'dom-'+mark+'-'+nth;
  messages.push({id,role,text:body,at:0});
 }
 return messages.slice(-120);
}
function hash(value){let h=2166136261;for(const c of value){h=Math.imul(h^c.charCodeAt(0),16777619);}return (h>>>0).toString(16);}
// Rich editors and message bubbles re-wrap lines, so sent text is compared without regard to whitespace.
function flat(value){return String(value||'').replace(/\s+/g,' ').trim();}
function drafted(box){return flat(box.value||box.innerText);}
function clearDraft(box){
 if(!box.isConnected||!drafted(box))return;
 box.focus();
 if(box.tagName==='TEXTAREA'){Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype,'value').set.call(box,'');box.dispatchEvent(new Event('input',{bubbles:true}));return;}
 const range=document.createRange();range.selectNodeContents(box);
 const selection=getSelection();selection.removeAllRanges();selection.addRange(range);document.execCommand('delete');
}
function same(){return connected&&observedSource===identity().source;}
async function host(type,payload){return chrome.runtime.sendMessage({type,payload,source:identity().source,instance});}
async function ack(id,status,detail=''){const result=await host('ack',{id,status,detail});if(result?.error)throw Error(result.error);}
async function sendJob(job){
 if(!same()||busy()){await ack(job.id,'failed','Thread changed or is busy');return;}
 const box=editor();
 if(!visible(box)||drafted(box)){await ack(job.id,'failed','Composer unavailable or contains your own draft');return;}
 const before=bubbles().filter(m=>m.role==='user').map(m=>m.id);
 box.focus();
 if(box.tagName==='TEXTAREA'){Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype,'value').set.call(box,job.body);box.dispatchEvent(new Event('input',{bubbles:true}));}
 else document.execCommand('insertText',false,job.body);
 await new Promise(r=>setTimeout(r,150));
 const controls='button[data-testid="send-button"],button[data-testid="chat-input-send"],button[aria-label*="send message" i],button[aria-label="Send" i],button[aria-label*="보내기"]';
 const form=box.closest('form');
 const button=[...(form||document).querySelectorAll(controls)].filter(visible).at(-1)
   ||(form&&[...form.querySelectorAll('button[type="submit"]')].filter(visible).length===1?form.querySelector('button[type="submit"]'):null);
 if(!same()||!visible(button)||drafted(box)!==flat(job.body)){
  // Remove only the exact text this job inserted. If the composer now holds anything else, the person may have
  // edited it during the pause, so it is theirs and stays untouched.
  const ours=drafted(box)===flat(job.body);
  if(ours)clearDraft(box);
  await ack(job.id,'failed',ours?'Could not verify the send control':'Composer changed before sending; left it as it is');return;
 }
 pending={id:job.id,body:job.body,before,at:Date.now(),user:null};button.click();
}
async function pulse(){
 if(!connected||working)return;
 working=true;
 try{
  if(!same()){connected=false;return;}
  const all=bubbles(),running=busy();
  const signature=all.map(m=>m.id+':'+hash(m.text)).join('|');
  if(signature!==lastSignature){lastSignature=signature;stableAt=Date.now();}
  const stable=!running&&Date.now()-stableAt>=900;
  let records=all;
  if(!stable&&all.at(-1)?.role==='assistant')records=all.slice(0,-1);
  if(initial&&!stable&&!running)return;
  const changed=records.filter(m=>observed.get(m.id)!==hash(m.text));
  const result=await host('sync',{title:document.title.replace(/\s*[-|–]\s*(ChatGPT|Claude).*$/i,''),messages:changed,initial,busy:running||!!pending,writable:visible(editor())});
  if(result?.error)throw Error(result.error);
  if(!result?.attached){connected=false;return;}
  for(const m of changed)observed.set(m.id,hash(m.text));
  initial=false;
  if(pending){
   const user=all.find(m=>m.role==='user'&&!pending.before.includes(m.id)&&flat(m.text)===flat(pending.body));
   if(user&&!pending.user){await ack(pending.id,'delivered');pending.user=user.id;}
   const pos=all.findIndex(m=>m.id===pending.user);
   if(pos>=0&&stable&&all.slice(pos+1).some(m=>m.role==='assistant')){await ack(pending.id,'answered');pending=null;}
   else if(Date.now()-pending.at>(pending.user?1800000:25000)){await ack(pending.id,'uncertain','Original page did not confirm completion');pending=null;}
  }
  if(result.job)await sendJob(result.job);
 }finally{working=false;}
}
chrome.runtime.onMessage.addListener((m,sender,reply)=>{
 if(m.type==='identity'){reply(identity());return;}
 if(m.type==='deactivate'){connected=false;pending=null;host('disconnect',{}).then(()=>reply({}),e=>reply({error:e.message}));return true;}
 if(m.type==='activate'){observed.clear();connected=true;observedSource=identity().source;initial=true;lastSignature=bubbles().map(m=>m.id+':'+hash(m.text)).join('|');stableAt=0;}
 if(m.type==='activate'||m.type==='pulse'){pulse().then(()=>reply({}),e=>reply({error:e.message}));return true;}
});
setInterval(()=>{if(connected)pulse().catch(()=>{});},1800);
