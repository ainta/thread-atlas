import {route} from './router.js';
const $=id=>document.getElementById(id);
const escape=value=>String(value??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const load=(key,def)=>{try{return JSON.parse(localStorage.getItem('atlas:'+key))??def;}catch{return def;}};
const save=(key,obj)=>localStorage.setItem('atlas:'+key,JSON.stringify(obj));
let lang=load('lang','en'),view='map',filter=null,focus=load('target',null),locked=load('locked',false);
let folded=new Set(load('folded',[])),messages=load('chat',[]),state={sessions:[],groups:[],inbox:[],deliveries:[]};
let showHidden=load('showHidden',false);
let projects=new Map(),children=new Map();
const PROJECT_INDENT_LIMIT=5;
const WORKSPACE_STATUSES=['default','active','hidden','excluded'];
let received=null,ready=false,polling=false,requestSeq=0,appliedSeq=0,dragging=null,lastPaint='',timer,draftID=load('draftID',null);
const l=(en,ko)=>lang==='ko'?ko:en;
const labels={map:['Work map','작업 지도'],inbox:['Inbox','받은 답변'],default:['Default','기본'],active:['Active','진행 중'],hidden:['Hidden','숨김'],excluded:['Will not consider','고려 안 함'],showHidden:['Show hidden','숨긴 세션 표시'],projects:['Projects','프로젝트'],connections:['Connections','연결 범위'],newGroup:['New project','새 프로젝트'],workspace:['Workspace','워크스페이스'],myWork:['My work','내 작업'],mainChat:['Main chat','메인 대화'],message:['Message','메시지'],send:['Send','보내기'],close:['Close','닫기']};
const text=key=>l(...labels[key]);
const item=id=>state.sessions.find(i=>i.id===id);
const name=i=>i?.title||l('Untitled session','제목 없는 세션');
const path=i=>(projectPath(i.group_id)||l('Unclassified','미분류'))+' › '+name(i);
const stamp=n=>n?new Date(n*1000).toLocaleString(lang==='ko'?'ko-KR':'en-US',{month:'short',day:'numeric',hour:'numeric',minute:'2-digit'}):'—';
const inMap=entity=>!['hidden','excluded'].includes(entity.effective_status)||(showHidden&&entity.effective_status==='hidden');
const mapSessions=()=>state.sessions.filter(inMap);
const treeSessions=()=>view==='active'?state.sessions.filter(i=>i.effective_status==='active'):mapSessions();
const status=i=>i.owned_terminal?l('Open in terminal','다른 터미널에서 실행 중'):i.state==='run'?l('Running','실행 중'):i.state==='wait'?l('Needs reply','답변 대기'):i.direct?l('Connected','연결됨'):l('Read-only','읽기 전용');
function indexProjects(groups){
 const byParent=new Map();
 for(const group of groups){
  const parent=group.parent_id??null;
  if(!byParent.has(parent))byParent.set(parent,[]);
  byParent.get(parent).push(group);
 }
 projects=new Map(groups.map(group=>[group.id,group]));
 children=byParent;
}
function projectRows(root=null,respectFolded=false,include=()=>true){
 const roots=(root?[projects.get(root)]:(children.get(null)||[])).filter(include);
 const pending=roots.map(group=>({group,depth:0,ancestorLines:[],lastSibling:true})).reverse(),rows=[];
 while(pending.length){
  const row=pending.pop();
  rows.push(row);
  if(respectFolded&&folded.has(row.group.id))continue;
  const subprojects=(children.get(row.group.id)||[]).filter(include);
  const ancestorLines=row.depth>0&&row.depth<PROJECT_INDENT_LIMIT
   ?[...row.ancestorLines,!row.lastSibling]:row.ancestorLines;
  for(let n=subprojects.length-1;n>=0;n--){
   pending.push({group:subprojects[n],depth:row.depth+1,ancestorLines,lastSibling:n===subprojects.length-1});
  }
 }
 return rows;
}
function projectPath(id){
 const names=[];
 for(let group=projects.get(id);group;group=projects.get(group.parent_id))names.push(group.name);
 return names.reverse().join(' › ');
}
function expandProject(id){
 for(let group=projects.get(id);group;group=projects.get(group.parent_id))folded.delete(group.id);
 save('folded',[...folded]);
}
function projectCounts(sessions){
 const counts=new Map(state.groups.map(g=>[g.id,{sessions:[],total:0,unread:0,hasActiveFolder:g.effective_status==='active'}]));
 for(const session of sessions){
  const count=counts.get(session.group_id);
  if(!count)continue;
  count.sessions.push(session);
  count.total++;
  if(session.unread)count.unread++;
 }
 for(const {group} of projectRows().reverse()){
  const parent=counts.get(group.parent_id);
  if(!parent)continue;
  const count=counts.get(group.id);
  parent.total+=count.total;
  parent.unread+=count.unread;
  parent.hasActiveFolder||=count.hasActiveFolder;
 }
 return counts;
}
function projectInTree(group,counts){
 if(view!=='active')return inMap(group);
 const count=counts.get(group.id);
 return count.total>0||count.hasActiveFolder;
}
function treeProjects(counts){
 return projectRows(filter,false,group=>projectInTree(group,counts));
}
function projectSelectOptions(selected,excluded=new Set()){
 return projectRows().filter(({group})=>group.effective_status!=='excluded'&&!excluded.has(group.id)).map(({group})=>
  '<option value="'+group.id+'" '+(group.id===selected?'selected':'')+'>'+escape(projectPath(group.id))+'</option>'
 ).join('');
}
function toast(msg){$('toast').textContent=msg;clearTimeout(timer);timer=setTimeout(()=>$('toast').textContent='',4200);}
async function api(action,data){
 const res=await fetch('/api/'+action,data===undefined?{}:{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(data)});
 if(!res.ok){const err=await res.json().catch(()=>({}));throw Error(err.error||'Request failed ('+res.status+')');}
 return res.json();
}
function dialog(title,html){$('dialogTitle').textContent=title;$('dialogContent').innerHTML=html;if(!$('dialog').open)$('dialog').showModal();}
function applyLanguage(){
 document.documentElement.lang=lang;document.querySelectorAll('[data-label]').forEach(e=>e.textContent=text(e.dataset.label));
 document.querySelectorAll('[data-lang]').forEach(e=>e.setAttribute('aria-pressed',e.dataset.lang===lang));
 $('mode').options[0].textContent=l('Auto','자동');$('mode').options[1].textContent=l('Find','찾기');$('mode').options[2].textContent=l('Send to thread','스레드에 전송');
 $('prompt').placeholder=l('Find a thread or give it a follow-up…','하던 일을 찾거나 다음 요청을 입력하세요');
 $('chatPanel').ariaLabel=text('mainChat');$('canvas').ariaLabel=text('map');$('activeScope').textContent=l('Local workspace','로컬 워크스페이스');
 redraw();
}
function card(i){
 const category=i.effective_status;
 const badge=workspaceBadge(category);
 return '<button draggable="true" class="card '+(focus===i.id?'focus':'')+(category==='hidden'?' hiddenSession':'')+'" data-card="'+i.id+'"><span class="ctop"><span class="provider">'+escape(i.provider)+' · '+escape(i.account)+'</span><span class="state '+(i.state==='wait'?'wait':'')+'">'+escape(status(i))+'</span></span><span class="ctitle">'+escape(name(i))+badge+(focus===i.id?'<span class="target">↳ '+l('Reply target','대화 대상')+'</span>':'')+'</span><span class="recent" style="display:block">'+escape(i.latest||l('No assistant reply yet','아직 답변 없음'))+'</span><span class="sub">'+stamp(i.latest_at)+'</span></button>';
}
function workspaceBadge(category){
 return category==='active'||category==='hidden'?'<span class="workspaceBadge '+category+'">'+text(category)+'</span>':'';
}
function workspaceStatusOptions(selected){
 return WORKSPACE_STATUSES.map(value=>'<option value="'+value+'" '+(selected===value?'selected':'')+'>'+text(value)+'</option>').join('');
}
function sidebarProject({group,depth,ancestorLines,lastSibling},count){
 const hasChildren=(children.get(group.id)||[]).some(inMap),isOpen=!folded.has(group.id);
 const toggleLabel=(isOpen?l('Collapse ','접기 '):l('Expand ','펼치기 '))+group.name;
 const toggle=hasChildren?'<button class="projectToggle" data-fold="'+group.id+'" aria-expanded="'+isOpen+'" aria-label="'+escape(toggleLabel)+'">'+(isOpen?'▾':'▸')+'</button>':'<span class="projectToggle"></span>';
 const guides=depth?'<span class="projectGuides" aria-hidden="true">'+
  ancestorLines.map(line=>'<span class="projectGuide'+(line?' continued':'')+'"></span>').join('')+
  '<span class="projectGuide elbow '+(lastSibling?'last':'continued')+'"></span></span>':'';
 const rootClass=depth?'':' topProject';
 const openClass=hasChildren&&isOpen&&depth<PROJECT_INDENT_LIMIT?' openBranch':'';
 const hiddenClass=group.effective_status==='hidden'?' hiddenFolder':'';
 return '<div class="projectRow'+rootClass+openClass+hiddenClass+'" data-group="'+group.id+'" style="--project-depth:'+Math.min(depth,PROJECT_INDENT_LIMIT)+'">'+guides+toggle+
  '<button class="projectButton '+(group.id===filter?'active':'')+'" draggable="true" data-drag-project="'+group.id+'" data-project="'+group.id+'" title="'+escape(projectPath(group.id))+'"><span class="projectName">'+escape(group.name)+'</span><small>'+count.total+'</small></button></div>';
}
function treeProjectRow(group,count,depth){
 const isOpen=!folded.has(group.id),path=projectPath(group.id);
 const optionsLabel=l('Project options for ','프로젝트 옵션 ')+group.name;
 const deepPath=depth>PROJECT_INDENT_LIMIT?'<small>'+escape(projectPath(group.parent_id))+'</small>':'';
 const sessions=l(count.total+' '+(count.total===1?'session':'sessions'),'세션 '+count.total+'개');
 const unread=count.unread?'<span class="treeUnread">'+l(count.unread+' unread','미확인 '+count.unread+'개')+'</span>':'';
 const badge=group.workspace_status===group.effective_status?workspaceBadge(group.workspace_status):'';
 const hiddenClass=group.effective_status==='hidden'?' hiddenFolder':'';
 return '<div class="treeProjectRow'+hiddenClass+'" data-group="'+group.id+'">'+
  '<button class="treeFolder" draggable="true" data-drag-project="'+group.id+'" data-fold="'+group.id+'" title="'+escape(path)+'" aria-expanded="'+isOpen+'">'+
   '<span class="treeChevron" aria-hidden="true"></span><span class="treeFolderIcon" aria-hidden="true"></span>'+
   '<span class="treeFolderTitle">'+deepPath+'<span>'+escape(group.name)+badge+'</span></span></button>'+
  '<span class="treeCount">'+sessions+unread+'</span>'+
  '<button class="groupOptions" data-project-options="'+group.id+'" title="'+escape(optionsLabel)+'" aria-label="'+escape(optionsLabel)+'">⋯</button></div>';
}
function workTree(root,counts){
 const tree=document.createElement('ul');
 tree.className='workTree';
 tree.setAttribute('role','list');
 const roots=(root?[projects.get(root)]:(children.get(null)||[])).filter(group=>projectInTree(group,counts));
 const pending=[{list:tree,groups:roots,sessions:[],depth:0}];
 while(pending.length){
  const {list,groups,sessions,depth}=pending.pop();
  for(const group of groups){
   const node=document.createElement('li'),count=counts.get(group.id);
   node.className='treeProject';
   node.innerHTML=treeProjectRow(group,count,depth);
   list.append(node);
   const subprojects=(children.get(group.id)||[]).filter(child=>projectInTree(child,counts));
   if(folded.has(group.id)||(!subprojects.length&&!count.sessions.length))continue;
   const branch=document.createElement('ul');
   branch.className=depth>=PROJECT_INDENT_LIMIT?'treeBranch compactBranch':'treeBranch';
   branch.setAttribute('role','list');
   node.append(branch);
   pending.push({list:branch,groups:subprojects,sessions:count.sessions,depth:depth+1});
  }
  if(sessions.length)list.insertAdjacentHTML('beforeend',sessions.map(session=>
   '<li class="treeSession" data-group="'+session.group_id+'">'+card(session)+'</li>'
  ).join(''));
 }
 return tree;
}
function redraw(){
 if(filter&&!inMap(projects.get(filter)))filter=null;
 const sessions=treeSessions(),counts=projectCounts(sessions),groups=treeProjects(counts);
 const isTree=view==='map'||view==='active';
 const totals={active:0,hidden:0,excluded:0};
 for(const i of state.sessions)if(i.effective_status==='active')totals.active++;
 for(const i of [...state.sessions,...state.groups]){
  if(i.workspace_status===i.effective_status&&['hidden','excluded'].includes(i.workspace_status))totals[i.workspace_status]++;
 }
 totals.active+=state.groups.filter(g=>g.workspace_status==='active'&&g.effective_status==='active').length;
 $('mapCount').textContent=mapSessions().length;
 $('activeCount').textContent=totals.active;
 $('hiddenCount').textContent=totals.hidden;
 $('excludedCount').textContent=totals.excluded;
 $('inboxCount').textContent=state.sessions.filter(i=>i.unread).length;
 const sidebarCounts=view==='active'?projectCounts(mapSessions()):counts;
 $('projectList').innerHTML=projectRows(null,true,inMap).map(row=>sidebarProject(row,sidebarCounts.get(row.group.id))).join('');
 document.querySelectorAll('[data-view]').forEach(b=>b.classList.toggle('active',b.dataset.view===view));
 $('heading').textContent=view==='map'&&filter?projects.get(filter).name:text(view);
 const trail=view==='map'&&filter?projectPath(projects.get(filter).parent_id):'';
 $('projectTrail').textContent=trail?' › '+trail:'';
 const area=$('canvas');area.className=isTree?'board':'';$('viewAction').hidden=!isTree||!groups.length;
 $('visibilityFilter').hidden=view!=='map';
 $('showHidden').checked=showHidden;
 $('viewAction').textContent=groups.every(({group})=>folded.has(group.id))?l('Expand all','모두 펼치기'):l('Collapse all','모두 접기');
 if(isTree){
  const total=filter?counts.get(filter).total:sessions.length;
  $('summary').textContent=l('Projects','프로젝트')+' '+groups.length+' · '+l('Sessions','세션')+' '+total;
  if(groups.length)area.replaceChildren(workTree(filter,counts));
  else area.innerHTML='<p class="empty">'+(view==='active'
   ?l('Mark a folder or session Active to see it here.','폴더나 세션을 진행 중으로 표시하면 여기에 나타납니다.')
   :l('Looking for local sessions. Open Connections to import web conversations or pair a browser.','로컬 세션을 찾는 중입니다. 연결 범위에서 웹 대화를 가져오거나 브라우저를 연결하세요.'))+'</p>';
  $('hint').textContent=groups.length?l(
   'Drag a project onto another project to nest it, or onto Work map to move it to the top level. Drop a session card on a project, Hidden, or the message box.',
   '프로젝트를 다른 프로젝트에 놓으면 하위로, 작업 지도에 놓으면 최상위로 이동합니다. 세션 카드는 프로젝트, 숨김 또는 입력창에 놓으세요.'
  ):'';
 }else if(view==='inbox'){
  $('summary').textContent=l('Latest replies from visible sessions','표시 중인 세션의 최신 답변');
  area.innerHTML=state.inbox.map(m=>{const i=item(m.session);return i?'<article class="listitem"><span class="sub">'+(m.unread?'● ':'')+escape(i.provider)+' · '+stamp(m.at)+'</span><h3>'+escape(name(i))+'</h3><p>'+escape(m.body.slice(0,500))+'</p><div class="actions"><button class="choice" data-card="'+i.id+'">'+l('View source','원문 보기')+'</button><button class="choice" data-pick="'+i.id+'">'+l('Reply here','이어서 답장')+'</button></div></article>':'';}).join('')||'<p class="empty">'+l('No replies yet.','받은 답변이 없습니다.')+'</p>';
  $('hint').textContent='';
 }else{
  const isHidden=view==='hidden';
  $('summary').textContent=isHidden
   ?l('Turn on Show hidden in Work map to reveal these folders and sessions in place.','작업 지도에서 숨김 표시를 켜면 폴더와 세션이 원래 위치에 표시됩니다.')
   :l('Outside the folder tree, inbox, and routing.','프로젝트 트리, 받은 답변 및 라우팅에서 제외됩니다.');
  const folders=state.groups.filter(g=>g.workspace_status===view&&g.effective_status===view).map(g=>
   '<article class="listitem"><span class="sub">'+l('Folder','폴더')+' · '+escape(projectPath(g.id))+'</span><h3>'+escape(g.name)+'</h3><div class="actions">'+
   '<button class="choice" data-project-options="'+g.id+'">'+l('Folder options','폴더 옵션')+'</button>'+
   '<button class="choice" data-restore-project="'+g.id+'">'+l('Restore','복구')+'</button></div></article>');
  const cards=state.sessions.filter(i=>i.workspace_status===view&&i.effective_status===view).map(i=>{
   const location=isHidden?projectPath(i.group_id):i.provider;
   return '<article class="listitem"><span class="sub">'+escape(location)+'</span><h3>'+escape(name(i))+'</h3><div class="actions">'+
    '<button class="choice" data-card="'+i.id+'">'+l('View session','세션 보기')+'</button>'+
    '<button class="choice" data-restore="'+i.id+'">'+l('Restore','복구')+'</button></div></article>';
  });
  area.innerHTML=[...folders,...cards].join('')||'<p class="empty">'+(isHidden
   ?l('No hidden folders or sessions.','숨긴 폴더나 세션이 없습니다.')
   :l('Folders and sessions marked Will not consider will appear here.','고려 안 함으로 표시한 폴더와 세션이 여기에 나타납니다.'))+'</p>';
  $('hint').textContent='';
 }
 $('statusline').textContent=(state.scan==='Ready'?l('Synced locally','로컬 동기화됨'):state.scan||l('Connecting…','연결 중…'))+' · '+stamp(state.lastScan);
 renderChat();preview();
}
function plan(){return route($('prompt').value,state.sessions,focus,$('mode').value,locked);}
function preview(){
 const value=$('prompt').value.trim(),target=item(focus);
 if(!value&&target&&!target.hidden){$('dest').innerHTML='↳ '+escape(path(target))+' · '+escape(target.provider)+' <button type="button" data-clear>'+l('Clear','해제')+'</button>';return;}
 const p=value?plan():null;
 $('dest').textContent=!p?l('Destination · Match from message','보낼 곳 · 메시지에서 찾기'):!p.action?l('Search local history','내 기록에서 찾기'):p.candidates.length!==1?l('Choose a destination','대상 선택'):p.confirm?l('Confirm before sending · ','전송 전 확인 · ')+path(p.candidates[0])+' · '+p.candidates[0].provider:(p.candidates[0].direct?l('To · ','보낼 곳 · '):l('Read-only · ','읽기 전용 · '))+path(p.candidates[0])+' · '+p.candidates[0].provider;
}
function pin(id,explicit){const i=item(id);if(!i||i.hidden)return;focus=id;locked=explicit;save('target',focus);save('locked',locked);expandProject(i.group_id);view='map';filter=null;redraw();}
function push(m){messages.push(m);messages=messages.slice(-70);save('chat',messages);renderChat();}
function renderChat(){
 const box=$('chat'),bottom=box.scrollHeight-box.clientHeight-box.scrollTop<55;
 const html=messages.map((m,n)=>{
  if(m.kind==='user')return '<div class="utter">'+escape(m.body)+'</div>';
  let h='';
  if(m.kind==='source')h='<b>'+escape(m.path)+'</b><div class="quote"><span class="sub">'+escape(m.provider)+' · '+stamp(m.at)+'</span><p>'+escape(m.body)+'</p><button class="linkbtn" data-card="'+m.session+'">'+l('View source','원문 보기')+' ↗</button></div>';
  if(m.kind==='choose')h=l('Which thread did you mean?','어느 스레드인가요?')+'<div class="replyopts">'+m.ids.map(id=>{const i=item(id);return i&&!i.hidden?'<button '+(m.chosen?'disabled':'')+' class="choice" data-select="'+id+'" data-n="'+n+'"><b>'+escape(path(i))+'</b> · '+escape(i.provider)+'<br>'+escape(i.latest.slice(0,125))+'</button>':'';}).join('')+'</div>';
  if(m.kind==='delivery'){const d=state.deliveries.find(d=>d.id===m.key);h='<b>'+escape(m.path)+'</b><div class="quote"><span>'+escape(d?.status||m.status||'queued')+'</span><p>'+escape(d?.detail&&['failed','uncertain'].includes(d.status)?d.detail:m.body)+'</p></div>';}
  if(m.kind==='readonly')h='<b>'+escape(m.path)+'</b><p>'+l('Read-only. Copy the draft to the original conversation.','읽기 전용입니다. 원본 대화로 초안을 복사하세요.')+'</p><button class="choice" data-copy="'+n+'">'+l('Copy draft','초안 복사')+'</button> <button class="choice" data-card="'+m.session+'">'+l('View source','원문 보기')+'</button>';
  if(m.kind==='info')h=escape(m.body);
  return '<div class="answer"><div class="assistantname">Thread Atlas</div>'+h+'</div>';
 }).join('');
 if(box.innerHTML!==html){box.innerHTML=html;if(bottom)box.scrollTop=box.scrollHeight;}
}
async function refresh(force=false){
 if(polling&&!force)return;polling=true;
 try{
  const req=++requestSeq,after=received;const s=await api('state'+(after===null?'':'?after='+after));
  if(req<appliedSeq)return;appliedSeq=req;
  indexProjects(s.groups);
  for(const i of s.sessions){i.project=projectPath(i.group_id);if(i.state==='idle'&&i.latest.trim().endsWith('?'))i.state='wait';}
  for(const m of s.events||[]){
   if(received!==null&&m.seq<=received)continue;
   const i=s.sessions.find(x=>x.id===m.session);
   if(i){toast(l('New reply · ','새 답변 · ')+name(i));if(messages.some(v=>v.kind==='delivery'&&v.session===i.id)&&!messages.some(v=>v.event===m.seq))
    push({kind:'source',event:m.seq,session:i.id,path:i.project+' › '+name(i),provider:i.provider,at:m.at,body:m.body});}
  }
  received=s.cursor;ready=true;state=s;
  if(focus&&(!item(focus)||item(focus).hidden)){
   focus=null;locked=false;save('target',null);save('locked',false);
  }
  const signature=JSON.stringify([s.scan,s.codex,s.claude,s.groups,s.inbox,s.deliveries,s.sessions.map(({live_at,synced_at,...i})=>i)]);
  if(!dragging&&(force||signature!==lastPaint)){lastPaint=signature;redraw();}

 }catch(e){$('statusline').textContent=l('Disconnected · ','연결 끊김 · ')+e.message;}finally{polling=false;}
}
async function detail(id){
 const i=item(id);if(!i)return;const history=await api('history?id='+encodeURIComponent(id));await api('edit',{id,read:true});
 const excluded=i.effective_status==='excluded';
 const location=i.workspace_status==='excluded'?text('excluded'):path(i);
 const inherited=i.effective_status!==i.workspace_status
  ?'<p class="sub">'+l('From folder: ','폴더에서 적용: ')+text(i.effective_status)+'</p>':'';
 const reply=i.hidden?'':'<button class="choice" data-pick="'+i.id+'">'+l('Reply here','이어서 답장')+'</button>';
 const original=i.url?'<a class="choice" target="_blank" rel="noopener noreferrer" href="'+escape(i.url)+'">'+l('Open original','원본 열기')+'</a>':'';
 const move=excluded?'':'<label class="modalfield">'+l('Move to project','프로젝트 이동')+'<select id="moveSelect">'+projectSelectOptions(i.group_id)+'</select></label>';
 dialog(name(i),'<p class="sub">'+escape(i.provider)+' · '+escape(i.account)+' · '+escape(location)+'</p>'+
  '<p>'+escape(status(i))+' · '+l('Last seen ','마지막 확인 ')+stamp(i.synced_at)+'</p>'+
  (reply||original?'<div class="actions">'+reply+original+'</div>':'')+
  '<label class="modalfield">'+l('Workspace status','워크스페이스 상태')+
  '<select id="workspaceStatus" aria-describedby="workspaceStatusHelp">'+workspaceStatusOptions(i.workspace_status)+'</select></label>'+
  '<div id="workspaceStatusHelp" class="sub">'+l('Hidden stays in its folder. Will not consider removes it from the tree and routing.','숨긴 세션은 프로젝트에 남고, 고려 안 함은 트리와 라우팅에서 빠집니다.')+'</div>'+
  inherited+move+'<p id="sessionEditError" class="sub" role="alert"></p>'+
  '<div class="history">'+history.map(m=>'<section><strong>'+escape(m.role)+'</strong> <small>'+stamp(m.at)+'</small><p>'+escape(m.body)+'</p></section>').join('')+'</div>');
 const fields=[$('workspaceStatus'),$('moveSelect')].filter(Boolean);
 for(const field of fields){
  const isStatus=field.id==='workspaceStatus',key=isStatus?'workspace_status':'group';
  field.onchange=async()=>{
   const errorBox=$('sessionEditError');
   errorBox.textContent='';
   for(const control of fields)control.disabled=true;
   try{
    await edit(id,{[key]:field.value});
    if(field.isConnected)$('dialog').close();
   }catch(error){
    field.value=isStatus?i.workspace_status:i.group_id;
    errorBox.textContent=error.message;
   }finally{
    for(const control of fields)control.disabled=false;
   }
  };
 }
 const hist=$('dialogContent').querySelector('.history');
 hist.scrollTop=hist.scrollHeight;
 await refresh(true);
}
async function edit(id,changes){await api('edit',{id,...changes});await refresh(true);}
async function updateProjectStatus(id,workspace_status){
 await api('group-status',{id,workspace_status});
 await refresh(true);
}
async function moveProject(id,parent){
 await api('move-group',{id,parent});
 if(filter)filter=id;
 expandProject(parent);
 await refresh(true);
}
async function createProject(parent=null){
 const title=parent?l('Subproject name','하위 프로젝트 이름'):l('Project name','프로젝트 이름');
 const name=prompt(title)?.trim();
 if(!name)return;
 await api('group',{name,parent});
 if(parent){
  expandProject(parent);
  $('dialog').close();
 }else{
  filter=null;
 }
 view='map';
 await refresh(true);
}
function showProjectOptions(id){
 const group=projects.get(id);
 const excluded=new Set(projectRows(id).map(({group})=>group.id));
 const counts=view==='active'?projectCounts(treeSessions()):null;
 const siblings=(children.get(group.parent_id)||[]).filter(g=>counts?projectInTree(g,counts):inMap(g));
 const index=siblings.findIndex(g=>g.id===id);
 const order=index>=0&&siblings.length>1?(
  '<div class="actions" role="group" aria-label="'+l('Folder order','폴더 순서')+'">'+
  '<button class="choice" data-order-side="before" data-order-sibling="'+(siblings[index-1]?.id||'')+'" '+(index===0?'disabled':'')+'>'+l('Move up','위로 이동')+'</button>'+
  '<button class="choice" data-order-side="after" data-order-sibling="'+(siblings[index+1]?.id||'')+'" '+(index===siblings.length-1?'disabled':'')+'>'+l('Move down','아래로 이동')+'</button></div>'):'';
 const structure=group.effective_status==='excluded'?'':(
  '<button class="choice" data-new-subproject="'+id+'">'+l('New subproject','새 하위 프로젝트')+'</button>'+order+
  '<form id="moveProjectForm"><label class="modalfield">'+l('Move under','상위 프로젝트')+'<select id="parentProject">'+
  '<option value="">'+l('Top level','최상위')+'</option>'+projectSelectOptions(group.parent_id,excluded)+'</select></label>'+
  '<button class="choice" type="submit">'+l('Move project','프로젝트 이동')+'</button></form>');
 dialog(group.name,
  '<p class="sub">'+escape(projectPath(id))+'</p>'+
  '<label class="modalfield">'+l('Folder status','폴더 상태')+'<select id="folderStatus">'+workspaceStatusOptions(group.workspace_status)+'</select></label>'+
  '<p class="sub">'+l('Folder status applies to its branch.','폴더 상태는 하위 폴더와 세션에도 적용됩니다.')+'</p>'+
  structure+'<p id="projectError" class="sub" role="alert"></p>');
 const statusField=$('folderStatus'),form=$('moveProjectForm'),errorBox=$('projectError');
 const orderButtons=Array.from($('dialogContent').querySelectorAll('[data-order-side]'));
 for(const button of orderButtons){
  button.onclick=async()=>{
   const side=button.dataset.orderSide,sibling=button.dataset.orderSibling;
   errorBox.textContent='';
   for(const control of orderButtons)control.disabled=true;
   try{
    await api('reorder-group',{id,sibling,side});
    await refresh(true);
    if(button.isConnected&&$('dialog').open){
     showProjectOptions(id);
     const content=$('dialogContent');
     const nextButton=content.querySelector('[data-order-side="'+side+'"]:enabled')||
      content.querySelector('[data-order-side]:enabled')||$('closeDialog');
     nextButton.focus();
    }
   }catch(error){
    errorBox.textContent=error.message;
   }finally{
    for(const control of orderButtons)control.disabled=!control.dataset.orderSibling;
   }
  };
 }
 statusField.onchange=async()=>{
  statusField.disabled=true;
  errorBox.textContent='';
  try{
   await updateProjectStatus(id,statusField.value);
   if(statusField.isConnected)$('dialog').close();
  }catch(error){
   statusField.value=group.workspace_status;
   errorBox.textContent=error.message;
  }finally{
   statusField.disabled=false;
  }
 };
 if(!form)return;
 form.onsubmit=async event=>{
  event.preventDefault();
  const submit=form.querySelector('[type=submit]');
  submit.disabled=true;
  errorBox.textContent='';
  try{
   const parent=$('parentProject').value||null;
   await moveProject(id,parent);
   if(form.isConnected)$('dialog').close();
  }catch(error){
   errorBox.textContent=error.message;
  }finally{
   submit.disabled=false;
  }
 };
}
async function handle(i,body,action,key){
 if(!i||i.hidden)throw Error(l('Session unavailable','세션을 사용할 수 없습니다'));
 if(!action){pin(i.id,false);push({kind:'source',session:i.id,path:path(i),provider:i.provider,at:i.latest_at,body:i.latest||l('No assistant reply yet.','아직 답변이 없습니다.')});return;}
 if(!i.direct){push({kind:'readonly',path:path(i),session:i.id,body});return;}
 const d=await api('send',{id:key,session:i.id,body});pin(i.id,false);push({kind:'delivery',session:i.id,key,body,path:path(i),status:d.status});await refresh(true);
}
$('composer').onsubmit=async e=>{
 e.preventDefault();const body=$('prompt').value.trim();if(!body)return;const p=plan();
 const key=draftID||crypto.randomUUID();draftID=key;save('draftID',key);
 const submit=$('composer').querySelector('[type=submit]');if(submit.disabled)return;submit.disabled=true;
 try{if(!messages.some(m=>m.kind==='user'&&m.key===key))push({kind:'user',body,key});
  if(!p.candidates.length)push({kind:'info',body:l('No matching visible session.','일치하는 세션이 없습니다.')});
  else if(p.candidates.length===1&&!p.waiting&&!p.confirm)await handle(p.candidates[0],body,p.action,key);
  else push({kind:'choose',ids:p.candidates.map(i=>i.id),body,action:p.action,key});
  if($('prompt').value.trim()===body){$('prompt').value='';save('draft','');draftID=null;save('draftID',null);}preview();
 }catch(err){toast(err.message);}finally{submit.disabled=false;}
};
async function settings(){
 const info=await api('pairing');
 const counts=['Codex','Claude Code','ChatGPT','Claude'].map(p=>{const list=state.sessions.filter(i=>i.provider===p);return '<p><b>'+p+'</b> · '+list.length+' '+l('indexed','색인됨')+' · '+list.filter(i=>i.direct).length+' '+l('writable','전송 가능')+'</p>';}).join('');
 const codexImport='<button class="choice" id="importCodex">'+l('Import Codex history','Codex 기록 가져오기')+'</button><p id="codexImportResult" class="sub" role="status"></p>';
 dialog(text('connections'),counts+codexImport+'<label class="modalfield"><span>'+l('Local notifications','로컬 알림')+'</span><input id="alerts" type="checkbox" '+(state.notifications?'checked':'')+'></label><hr><p><b>'+l('Import conversation history','대화 기록 가져오기')+'</b></p><label class="modalfield">'+l('Service','서비스')+'<select id="provider"><option>ChatGPT</option><option>Claude</option></select></label><label class="modalfield">'+l('Account label','계정 이름')+'<input id="account" value="Personal"></label><label class="modalfield">'+l('Export file (.json or .zip)','내보내기 파일 (.json 또는 .zip)')+'<input id="file" type="file" accept=".json,.zip"></label><p id="importResult" role="status"></p><hr><p><b>'+l('Connect a browser tab','브라우저 탭 연결')+'</b></p><p class="sub">'+l('Load the extension folder in Chrome, open a source conversation, then paste the pairing key into the extension. Use the same account label when importing.','Chrome에 extension 폴더를 로드한 뒤 원본 대화에서 확장 프로그램에 연결 키를 붙여 넣으세요. 가져오기와 동일한 계정 이름을 사용하세요.')+'</p><button class="choice" id="copyKey">'+l('Copy pairing key','연결 키 복사')+'</button>');
 const button=$('importCodex'),result=$('codexImportResult');
 button.onclick=async()=>{
  button.disabled=true;
  result.textContent=l('Importing Codex history…','Codex 기록을 가져오는 중…');
  try{
   const {added,already_indexed,failed}=await api('import-codex',{});
   result.textContent=l('Added '+added+' · Already indexed '+already_indexed+' · Unavailable '+failed,
    '추가 '+added+'개 · 기존 '+already_indexed+'개 · 실패 '+failed+'개');
   await refresh(true);
  }catch(error){
   result.textContent=error.message;
  }finally{
   button.disabled=false;
  }
 };
 $('alerts').onchange=async e=>{await api('notifications',{enabled:e.target.checked});await refresh(true);};
 $('copyKey').onclick=async()=>{await navigator.clipboard.writeText(info.port+':'+info.token);toast(l('Pairing key copied','연결 키 복사됨'));};
 $('file').onchange=async e=>{
  const file=e.target.files[0];if(!file)return;$('importResult').textContent=l('Importing…','가져오는 중…');
  try{const parameters=new URLSearchParams({provider:$('provider').value,account:$('account').value});
   const response=await fetch('/import?'+parameters,{method:'POST',headers:{'Content-Type':'application/octet-stream'},body:file});const d=await response.json();if(!response.ok)throw Error(d.error||'Import failed');
   $('importResult').textContent=l('Imported ','가져온 세션 ')+d.count;await refresh(true);
  }catch(err){$('importResult').textContent=err.message;}
 };
}
document.addEventListener('click',async e=>{
 const b=e.target.closest('button');if(!b)return;const k=b.dataset;
 try{
  if(k.lang){lang=k.lang;save('lang',lang);applyLanguage();}
  if(k.view){view=k.view;filter=null;redraw();}
  if(k.project){filter=filter===k.project?null:k.project;expandProject(k.project);view='map';redraw();}
  if(k.fold){
   const scope=b.closest('#canvas')?'canvas':'projectList';
   folded.has(k.fold)?folded.delete(k.fold):folded.add(k.fold);
   save('folded',[...folded]);redraw();
   $(scope).querySelector('[data-fold="'+k.fold+'"]')?.focus({preventScroll:true});
  }
  if(k.projectOptions)showProjectOptions(k.projectOptions);
  if(k.newSubproject)await createProject(k.newSubproject);
  if(k.card)await detail(k.card);
  if(k.pick){if($('dialog').open)$('dialog').close();pin(k.pick,true);$('mode').value='send';preview();$('prompt').focus();}
  if(k.restore)await edit(k.restore,{workspace_status:'default'});
  if(k.restoreProject)await updateProjectStatus(k.restoreProject,'default');
  if('clear' in k){focus=null;locked=false;save('target',null);save('locked',false);$('mode').value='auto';redraw();}
  if(k.select){const m=messages[Number(k.n)];if(m.chosen)return;m.chosen=k.select;renderChat();try{await handle(item(k.select),m.body,m.action,m.key);save('chat',messages);}catch(err){delete m.chosen;renderChat();throw err;}}
  if('copy' in k)await navigator.clipboard.writeText(messages[Number(k.copy)].body);
  if(b.id==='closeDialog')$('dialog').close();
  if(b.id==='connections')await settings();
  if(b.id==='addGroup')await createProject();
  if(b.id==='viewAction'){
   const ids=treeProjects(projectCounts(treeSessions())).map(({group})=>group.id),collapse=!ids.every(id=>folded.has(id));
   for(const id of ids)collapse?folded.add(id):folded.delete(id);
   save('folded',[...folded]);redraw();
  }
 }catch(err){toast(err.message);}
});
$('mode').onchange=preview;$('prompt').oninput=()=>{draftID=null;save('draftID',null);save('draft',$('prompt').value);preview();};
$('showHidden').onchange=event=>{
 showHidden=event.target.checked;
 save('showHidden',showHidden);
 redraw();
};
$('prompt').onkeydown=e=>{if(e.key==='Enter'&&!e.shiftKey&&!e.isComposing&&e.keyCode!==229){e.preventDefault();$('composer').requestSubmit();}};
const PROJECT_DRAG='application/x-thread-atlas-project',CARD_DRAG='application/x-thread-atlas';
let highlightedDrop=null;
function highlightDrop(target){
 if(highlightedDrop===target)return;
 highlightedDrop?.classList.remove('dropping');
 highlightedDrop=target;
 highlightedDrop?.classList.add('dropping');
 const project=target&&dragging?.type===PROJECT_DRAG?projects.get(dragging.id):null;
 const status=$('projectDropPreview');
 if(!project){
  status.textContent='';
  return;
 }
 const destination=projectPath(target.dataset.group);
 status.textContent=destination
  ?l('Move '+project.name+' under '+destination,project.name+'을(를) '+destination+' 아래로 이동')
  :l('Move '+project.name+' to the top level',project.name+'을(를) 최상위로 이동');
}
function dropZone(event){
 const types=event.dataTransfer.types;
 if(types.includes(PROJECT_DRAG)){
  if(dragging?.type!==PROJECT_DRAG)return null;
  const target=event.target.closest('.treeProjectRow,.projectRow,[data-view="map"]');
  if(!target)return null;
  const parent=target.dataset.group??null;
  if(projects.get(dragging.id)?.parent_id===parent)return null;
  for(let group=projects.get(parent);group;group=projects.get(group.parent_id)){
   if(group.id===dragging.id)return null;
  }
  return target;
 }
 if(!types.includes(CARD_DRAG))return null;
 const target=event.target.closest('#hiddenDrop,#composer,[data-group]');
 return target?.id==='composer'&&item(dragging?.id)?.hidden?null:target;
}
function endDrag(){
 dragging=null;
 highlightDrop(null);
}
document.addEventListener('dragstart',event=>{
 const source=event.target.closest('[data-drag-project],.card[data-card]');
 if(!source)return;
 const project=source.dataset.dragProject;
 dragging=project?{type:PROJECT_DRAG,id:project}:{type:CARD_DRAG,id:source.dataset.card};
 event.dataTransfer.setData(dragging.type,dragging.id);
 event.dataTransfer.effectAllowed='move';
});
document.addEventListener('dragover',event=>{
 const target=dropZone(event);
 highlightDrop(target);
 if(!target)return;
 event.preventDefault();
 event.dataTransfer.dropEffect='move';
});
document.addEventListener('dragleave',event=>{
 if(event.target===highlightedDrop&&!highlightedDrop.contains(event.relatedTarget))highlightDrop(null);
});
document.addEventListener('dragend',endDrag);
document.addEventListener('drop',async event=>{
 const target=dropZone(event);
 const project=event.dataTransfer.getData(PROJECT_DRAG);
 const card=event.dataTransfer.getData(CARD_DRAG);
 endDrag();
 if(!target)return;
 event.preventDefault();
 try{
  if(project)await moveProject(project,target.dataset.group??null);
  else if(!item(card))return;
  else if(target.id==='hiddenDrop')await edit(card,{workspace_status:'hidden'});
  else if(target.id==='composer'){
   if(item(card).hidden)return;
   pin(card,true);$('mode').value='send';preview();$('prompt').focus();
  }
  else await edit(card,{group:target.dataset.group,before:event.target.closest('[data-card]')?.dataset.card});
 }catch(error){toast(error.message);}
});
async function openNotification() {
 const match=/^#reply-([1-9][0-9]{0,18})$/.exec(location.hash);
 if(!match && location.hash!=='#inbox')return;
 view='inbox';filter=null;redraw();
 if(!match)return;
 try {
  const reply=await api('reply?seq='+match[1]);
  const thread=reply&&item(reply.session);
  if(!thread||thread.hidden){toast(l('This reply is no longer visible.','이 답변은 현재 볼 수 없습니다.'));return;}
  dialog(name(thread),'<p class="sub">'+escape(thread.provider)+' · '+stamp(reply.at)+'</p><div class="history"><section><p>'+escape(reply.body)+'</p></section></div><div class="actions"><button class="choice" data-card="'+thread.id+'">'+l('View source','원문 보기')+'</button><button class="choice" data-pick="'+thread.id+'">'+l('Reply here','이어서 답장')+'</button></div>');
 } catch(err){toast(err.message);}
}
$('prompt').value=load('draft','');applyLanguage();await refresh(true);
window.addEventListener('hashchange',openNotification);await openNotification();setInterval(()=>refresh(),2000);
