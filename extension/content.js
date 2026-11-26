// The user connects one URL and one document instance; navigation invalidates the binding.
const instance=crypto.randomUUID();
let connected=false,observedSource='',initial=true,working=false,pending=null,lastSignature='',stableAt=0;
function identity(){
 const route=location.pathname.match(location.hostname==='chatgpt.com'?/^\/c\/([\w-]+)\/?$/:/^\/chat\/([\w-]+)\/?$/);
 return {provider:location.hostname==='chatgpt.com'?'ChatGPT':'Claude',source:route?.[1],instance};
}
function editor(){return document.querySelector('#prompt-textarea,textarea[data-testid="chat-input"],[contenteditable="true"].ProseMirror,[contenteditable="true"][role="textbox"],textarea[placeholder]');}
function visible(el){return Boolean(el&&el.getClientRects().length&&!el.disabled&&el.getAttribute('aria-disabled')!=='true');}
function busy(){return [...document.querySelectorAll('button[data-testid="stop-button"],button[aria-label*="Stop" i],button[aria-label*="중지"],[data-is-streaming="true"]')].some(visible);}
function bubbles(){
 const gpt=identity().provider==='ChatGPT';
 const nodes=[...document.querySelectorAll(gpt?'[data-message-author-role]':'[data-testid="user-message"],[data-testid="assistant-message"],.font-claude-response')];
 const messages=[];
 for(const [pos,el] of nodes.entries()){
  if(nodes.some(other=>other!==el&&other.contains(el)))continue;
  const role=gpt?el.dataset.messageAuthorRole:(el.matches('[data-testid="user-message"]')?'user':'assistant');
  if(!['user','assistant'].includes(role))continue;
  const body=(el.innerText||'').trim();if(!body)continue;
  const parent=el.closest('[data-message-id],[data-message-uuid]');
  const id=parent?.dataset.messageId||parent?.dataset.messageUuid||el.id||'dom-'+role+'-'+pos+'-'+hash(body);
  messages.push({id,role,text:body,at:0});
 }
 return messages.slice(-120);
}
function hash(value){let h=2166136261;for(const c of value){h=Math.imul(h^c.charCodeAt(0),16777619);}return (h>>>0).toString(16);}
function same(){return connected&&observedSource===identity().source;}
async function host(type,payload){return chrome.runtime.sendMessage({type,payload,source:identity().source,instance});}
async function ack(id,status,detail=''){const result=await host('ack',{id,status,detail});if(result?.error)throw Error(result.error);}
async function sendJob(job){
 if(!same()||busy()){await ack(job.id,'failed','Thread changed or is busy');return;}
 const box=editor();
 if(!visible(box)||(box.value||box.innerText||'').trim()){await ack(job.id,'failed','Composer unavailable or contains your own draft');return;}
 const before=bubbles().filter(m=>m.role==='user').map(m=>m.id);
 box.focus();
 if(box.tagName==='TEXTAREA'){Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype,'value').set.call(box,job.body);box.dispatchEvent(new Event('input',{bubbles:true}));}
 else document.execCommand('insertText',false,job.body);
 await new Promise(r=>setTimeout(r,150));
 const button=document.querySelector('button[data-testid="send-button"],button[aria-label*="send message" i],button[aria-label="Send" i],button[aria-label*="보내기"]');
 if(!same()||!visible(button)||(box.value||box.innerText||'').trim()!==job.body.trim()){await ack(job.id,'failed','Could not verify the composer and send control');return;}
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
  const result=await host('sync',{title:document.title.replace(/\s*[-|–]\s*(ChatGPT|Claude).*$/i,''),messages:records,initial,busy:running||!!pending,writable:visible(editor())});
  if(result?.error)throw Error(result.error);
  if(!result?.attached){connected=false;return;}
  initial=false;
  if(pending){
   const user=all.find(m=>m.role==='user'&&!pending.before.includes(m.id)&&m.text.trim()===pending.body.trim());
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
 if(m.type==='deactivate'){connected=false;pending=null;reply({});return;}
 if(m.type==='activate'){connected=true;observedSource=identity().source;initial=true;lastSignature=bubbles().map(m=>m.id+':'+hash(m.text)).join('|');stableAt=0;}
 if(m.type==='activate'||m.type==='pulse'){pulse().then(()=>reply({}),e=>reply({error:e.message}));return true;}
});
setInterval(()=>{if(connected)pulse().catch(()=>{});},1800);
