chrome.tabs.onRemoved.addListener(id=>chrome.storage.session.remove('tab'+id));
chrome.alarms.create('heartbeat',{periodInMinutes:.5});
chrome.alarms.onAlarm.addListener(async alarm=>{
 if(alarm.name!=='heartbeat')return;
 const entries=await chrome.storage.session.get(null);
 for(const k of Object.keys(entries).filter(k=>/^tab\d+$/.test(k)))chrome.tabs.sendMessage(Number(k.slice(3)),{type:'pulse'}).catch(()=>{});
});
chrome.runtime.onMessage.addListener((msg,sender,reply)=>{
 if(!sender.tab||sender.frameId!==0||!['sync','ack','attached','disconnect'].includes(msg.type))return;
 (async()=>{
  const key='tab'+sender.tab.id,c=(await chrome.storage.session.get(key))[key];
  if(!c||c.instance!==msg.instance)return {attached:false};
  const url=new URL(sender.url);
  const root=c.provider==='ChatGPT'?'https://chatgpt.com':'https://claude.ai';
  const path=c.pathname||(c.provider==='ChatGPT'?'/c/':'/chat/')+c.source;
  if(url.origin!==root||url.pathname.replace(/\/$/,'')!==path||msg.source!==c.source){await chrome.storage.session.remove(key);return {attached:false};}
  if(msg.type==='attached')return {attached:true};
  const [port,token]=c.pairing.split(':');
  const payload={...msg.payload,provider:c.provider,account:c.account,source:c.source,tab:c.tab,pathname:path};
  if(msg.type==='sync')payload.writable=c.allowed&&Boolean(payload.writable);
  const res=await fetch('http://127.0.0.1:'+port+'/bridge/'+msg.type,{method:'POST',headers:{'Content-Type':'application/json','Authorization':'Bearer '+token},body:JSON.stringify(payload),signal:AbortSignal.timeout(9000)});
  const result=await res.json();if(!res.ok)throw Error(result.error||'Connection failed');
  return {...result,attached:true};
 })().then(reply,e=>reply({error:e.message}));return true;
});
