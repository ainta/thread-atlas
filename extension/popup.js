const $=id=>document.getElementById(id);
let tab;
const ko=()=> $('lang').value==='ko';
function labels(){
 document.documentElement.lang=$('lang').value;
 for(const [id,en,kr] of [['keyLabel','Pairing key','연결 키'],['accountLabel','Account label','계정 이름'],['writeLabel','Allow replies to this thread','이 스레드에 답장 허용'],['connect','Connect tab','탭 연결'],['disconnect','Disconnect','연결 해제'],['all','Track open chats (read-only)','열린 대화 모두 추적 (읽기 전용)']])$(id).textContent=ko()?kr:en;
}
async function start(){
 [tab]=await chrome.tabs.query({active:true,currentWindow:true});
 const saved=await chrome.storage.local.get(['pairing','account','language']);
 $('key').value=saved.pairing||'';$('account').value=saved.account||'Personal';$('lang').value=saved.language||'en';labels();
 const data=await chrome.storage.session.get('tab'+tab.id);const attached=data['tab'+tab.id];
 if(attached){$('write').checked=attached.allowed;$('status').textContent=(ko()?'연결됨 · ':'Connected · ')+attached.provider;}
}
$('lang').onchange=()=>{labels();chrome.storage.local.set({language:$('lang').value});};
$('connect').onclick=async()=>{
 try{
  const data=await chrome.tabs.sendMessage(tab.id,{type:'identity'});if(!data?.source)throw Error(ko()?'기존 대화 탭을 여세요.':'Open an existing conversation tab.');
  const pairing=$('key').value.trim(),account=$('account').value.trim();
  if(!/^\d{1,5}:[\w-]{30,}$/.test(pairing)||!account)throw Error(ko()?'연결 키와 계정을 확인하세요.':'Check the pairing key and account label.');
  await chrome.storage.local.set({pairing,account});
  const bindingKey='tab'+tab.id,previous=(await chrome.storage.session.get(bindingKey))[bindingKey];
  if(previous&&previous.account!==account)await chrome.tabs.sendMessage(tab.id,{type:'deactivate'}).catch(()=>{});
  const same=previous?.instance===data.instance&&previous.account===account&&previous.source===data.source;
  await chrome.storage.session.set({[bindingKey]:{...data,account,pairing,allowed:$('write').checked,tab:same?previous.tab:crypto.randomUUID()}});
  const reply=await chrome.tabs.sendMessage(tab.id,{type:'activate'});
  if(reply?.error)throw Error(reply.error);
  $('status').textContent=ko()?'이 대화에 연결했습니다.':'Connected to this conversation.';
 }catch(e){$('status').textContent=e.message;}
};
$('disconnect').onclick=async()=>{await chrome.tabs.sendMessage(tab.id,{type:'deactivate'}).catch(()=>{});await chrome.storage.session.remove('tab'+tab.id);$('status').textContent=ko()?'연결 해제됨':'Disconnected';};
start();

$('all').onclick=async()=>{
 try{
  const pairing=$('key').value.trim(),account=$('account').value.trim();
  if(!/^\d{1,5}:[\w-]{30,}$/.test(pairing)||!account)throw Error(ko()?'연결 키와 계정을 확인하세요.':'Check the pairing key and account label.');
  await chrome.storage.local.set({pairing,account});
  const tabs=await chrome.tabs.query({url:['https://chatgpt.com/c/*','https://chatgpt.com/g/*/c/*','https://claude.ai/chat/*']});
  let count=0;
  for(const tab of tabs){
   try{
    const identity=await chrome.tabs.sendMessage(tab.id,{type:'identity'});if(!identity?.source)continue;
    const key='tab'+tab.id,prior=(await chrome.storage.session.get(key))[key];
    if(prior?.instance!==identity.instance)await chrome.storage.session.set({[key]:{...identity,account,pairing,allowed:false,tab:crypto.randomUUID()}});
    const result=await chrome.tabs.sendMessage(tab.id,{type:'activate'});if(!result?.error)count++;
   }catch{}
  }
  $('status').textContent=ko()?count+'개 대화를 연결했습니다.':'Connected '+count+' open conversations.';
 }catch(e){$('status').textContent=e.message;}
};
