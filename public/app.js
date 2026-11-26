import {isActiveThread,route} from './router.js';
const $=id=>document.getElementById(id);
const escape=value=>String(value??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const load=(key,def)=>{try{return JSON.parse(localStorage.getItem('atlas:'+key))??def;}catch{return def;}};
const save=(key,obj)=>localStorage.setItem('atlas:'+key,JSON.stringify(obj));
let lang=load('lang','en'),view='map',filter=null,focus=load('target',null),locked=load('locked',false);
let folded=new Set(load('folded',[])),messages=load('chat',[]),state={sessions:[],groups:[],inbox:[],deliveries:[]};
let activityMarkup='',threadEditor=null;
const updateFolds=new Set();
let todoMarkup='',todoOptions='',addingTodo=false,todoMemoProject=null;
let showHidden=load('showHidden',false);
let projects=new Map(),children=new Map();
const PROJECT_INDENT_LIMIT=5;
const WORKSPACE_STATUSES=['default','active','hidden','excluded'];
let received=null,ready=false,polling=false,requestSeq=0,appliedSeq=0,dragging=null,lastPaint='',timer,undoing=false,draftID=load('draftID',null);
const l=(en,ko)=>lang==='ko'?ko:en;
const labels={map:['Work map','작업 지도'],inbox:['Inbox','받은 답변'],default:['Default','기본'],active:['Active','진행 중'],hidden:['Hidden','숨김'],excluded:['Will not consider','고려 안 함'],trash:['Trash','휴지통'],undo:['Undo','실행 취소'],showHidden:['Show hidden','숨긴 세션 표시'],projects:['Projects','프로젝트'],connections:['Connections','연결 범위'],newGroup:['New project','새 프로젝트'],workspace:['Workspace','워크스페이스'],myWork:['My work','내 작업'],mainChat:['Main chat','메인 대화'],message:['Message','메시지'],send:['Send','보내기'],close:['Close','닫기'],
 updates:['Updates','업데이트'],todos:['To-dos','할 일'],showProjects:['Show projects','프로젝트 표시'],
 activity:['Activity','활동'],conversation:['Conversation','대화'],source:['Source','서비스'],thread:['Thread','스레드'],destination:['Destination','보낼 곳'],
 project:['Project','프로젝트'],todo:['To-do','할 일'],addTodo:['Add to-do','할 일 추가'],allTodos:['All to-dos','모든 할 일'],
 projectFolders:['Project folders','프로젝트 폴더'],setupFolders:['Set up folders','폴더 설정'],skipSetup:['Skip','건너뛰기'],
 setupIntro:['Use starter folders for Research, Life, and Study, or make your own.','연구, 일상, 공부용 기본 폴더를 사용하거나 원하는 폴더를 만드세요.'],
 redBorder:['Red · Unread reply','빨간색 · 읽지 않은 답변'],
 blueBorder:['Blue · Newly added thread','파란색 · 새로 추가된 스레드'],
 markRead:['Mark as read','읽음으로 표시'],markedRead:['Marked as read','읽음으로 표시됨'],
 borderOrder:['Parent folders show the highest priority inside. Red items come first, then blue. Open a thread or use Mark as read to clear its border.',
  '상위 폴더에는 내부 항목 중 가장 높은 우선순위가 표시됩니다. 빨간색이 먼저, 다음은 파란색입니다. 스레드를 열거나 읽음으로 표시하면 테두리 표시가 사라집니다.'],
};
const text=key=>l(...labels[key]);
const item=id=>state.sessions.find(i=>i.id===id);
const todoById=id=>(state.todos||[]).find(todo=>todo.id===id);
const name=i=>i?.title||l('Untitled session','제목 없는 세션');
const path=i=>(projectPath(i.group_id)||l('Unclassified','미분류'))+' › '+name(i);
const stamp=n=>n?new Date(n*1000).toLocaleString(lang==='ko'?'ko-KR':'en-US',{month:'short',day:'numeric',hour:'numeric',minute:'2-digit'}):'—';
const inMap=entity=>['default','active'].includes(entity.effective_status)||(showHidden&&entity.effective_status==='hidden');
const mapSessions=()=>state.sessions.filter(inMap);
const updateSessions=()=>state.sessions.filter(i=>!i.hidden&&(i.unread||i.is_new));
function treeSessions(){
 if(view==='updates')return updateSessions();
 if(view==='active')return state.sessions.filter(isActiveThread);
 return mapSessions();
}
const treeFolds=()=>view==='updates'?updateFolds:folded;
const status=i=>i.owned_terminal?l('Open in terminal','다른 터미널에서 실행 중'):i.state==='run'?l('Running','실행 중'):i.state==='wait'?l('Needs reply','답변 대기'):i.direct?l('Connected','연결됨'):l('Read-only','읽기 전용');
const attentionLevel=(unread,newThreads)=>unread?2:newThreads?1:0;
const attentionClass=level=>['',' newThread',' unreadReply'][level];
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
function projectRows(root=null,respectFolded=false,include=()=>true,compare=null){
 const roots=(root?[projects.get(root)]:(children.get(null)||[])).filter(include);
 const pending=roots.map(group=>({group,depth:0,ancestorLines:[],lastSibling:true})).reverse(),rows=[];
 while(pending.length){
  const row=pending.pop();
  rows.push(row);
  if(respectFolded&&folded.has(row.group.id))continue;
  const subprojects=(children.get(row.group.id)||[]).filter(include);
  if(compare)subprojects.sort(compare);
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
 const counts=new Map(state.groups.map(g=>[g.id,{sessions:[],total:0,unread:0,newThreads:0,hasActiveFolder:g.effective_status==='active'}]));
 for(const session of sessions){
  const count=counts.get(session.group_id);
  if(!count)continue;
  count.sessions.push(session);
  count.total++;
  if(session.unread)count.unread++;
  else if(session.is_new)count.newThreads++;
 }
 for(const {group} of projectRows().reverse()){
  const parent=counts.get(group.parent_id);
  if(!parent)continue;
  const count=counts.get(group.id);
  parent.total+=count.total;
  parent.unread+=count.unread;
  parent.newThreads+=count.newThreads;
  parent.hasActiveFolder||=count.hasActiveFolder;
 }
 return counts;
}
function projectInTree(group,counts){
 if(view==='updates')return counts.get(group.id).total>0;
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
function dialog(title,html){threadEditor=null;$('dialogTitle').textContent=title;$('dialogContent').innerHTML=html;if(!$('dialog').open)$('dialog').showModal();}
function applyLanguage(){
 document.documentElement.lang=lang;document.querySelectorAll('[data-label]').forEach(e=>e.textContent=text(e.dataset.label));
 document.querySelectorAll('[data-lang]').forEach(e=>e.setAttribute('aria-pressed',e.dataset.lang===lang));
 $('mode').options[0].textContent=l('Auto','자동');$('mode').options[1].textContent=l('Find','찾기');$('mode').options[2].textContent=l('Send to thread','스레드에 전송');
 $('prompt').placeholder=l('Find a thread or give it a follow-up…','하던 일을 찾거나 다음 요청을 입력하세요');
 $('canvas').ariaLabel=text('map');
 redraw();
}
function card(i){
 const category=i.effective_status;
 const badge=category===i.workspace_status?'':workspaceBadge(category);
 const actionsLabel=escape(l('Actions for '+name(i),name(i)+' 작업'));
 const title=escape(name(i)),provider=escape(i.provider+' · '+i.account),connection=escape(status(i));
 const date=escape(stamp(i.latest_at));
 const level=attentionLevel(i.unread,i.is_new);
 const attention=level?'<span class="attentionLabel cardLine">'+(i.unread?l('New reply','새 답변'):l('New thread','새 스레드'))+'</span>':'';
 const read=level?markReadButton(i.id,i.unread_through??0,'cardRead'):'';
 return '<div class="threadCard'+(focus===i.id?' focus':'')+(category==='hidden'?' hiddenSession':'')+attentionClass(level)+'">'+
  '<button draggable="true" class="card" data-card="'+i.id+'" title="'+title+'">'+
   '<span class="ctop"><span class="provider cardLine" title="'+provider+'">'+provider+'</span>'+
    '<span class="state '+(i.state==='wait'?'wait':'')+'" title="'+connection+'"><span class="cardLine">'+connection+'</span></span>'+attention+'</span>'+
   '<span class="ctitle"><span class="cardLine">'+title+'</span>'+badge+
    (focus===i.id?'<span class="target cardLine">↳ '+l('Reply target','대화 대상')+'</span>':'')+'</span>'+
   '<span class="recent cardLine">'+escape(i.latest||l('No assistant reply yet','아직 답변 없음'))+'</span></button>'+
  '<div class="cardFooter"><span class="sub cardLine" title="'+date+'">'+date+'</span><div class="cardActions" role="group" aria-label="'+actionsLabel+'">'+
    read+statusButton(i,'active')+statusButton(i,'hidden')+trashButton(i,'cardTrash')+'</div></div></div>';
}
function markReadButton(id,through,style){
 const label=text('markRead');
 return '<button type="button" class="'+style+'" data-mark-read="'+id+'" data-read-through="'+through+
  '" aria-label="'+label+'" title="'+label+'"><span class="markReadIcon" aria-hidden="true">✓</span>'+
  '<span class="markReadLabel">'+label+'</span></button>';
}
function statusButton(i,status){
 const selected=i.workspace_status===status;
 const next=text(selected?'default':status);
 const description=escape(l('Set '+name(i)+' to '+next,name(i)+' 상태를 '+next+'(으)로 변경'));
 return '<button class="cardStatus '+status+'" data-session-status="'+status+'" aria-pressed="'+selected+'" title="'+description+'">'+text(status)+'</button>';
}
function trashButton(i,style,label=''){
 const description=escape(l('Move '+name(i)+' to Trash',name(i)+'을(를) 휴지통으로 이동'));
 return '<button class="'+style+'" data-trash-session="'+i.id+'" aria-label="'+description+'" title="'+description+'">'+
  '<span class="trashIcon" aria-hidden="true"></span>'+(label?' '+label:'')+'</button>';
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
 return '<div class="projectRow'+rootClass+openClass+hiddenClass+attentionClass(attentionLevel(count.unread,count.newThreads))+'" data-group="'+group.id+'" style="--project-depth:'+Math.min(depth,PROJECT_INDENT_LIMIT)+'">'+guides+toggle+
  '<button class="projectButton '+(group.id===filter?'active':'')+'" draggable="true" data-drag-project="'+group.id+'" data-project="'+group.id+'" title="'+escape(projectPath(group.id))+'"><span class="projectName">'+escape(group.name)+'</span><small>'+count.total+'</small></button></div>';
}
function treeProjectRow(group,count,depth,openTodos){
 const isOpen=!treeFolds().has(group.id),path=projectPath(group.id);
 const optionsLabel=l('Project options for ','프로젝트 옵션 ')+group.name;
 const todosLabel=escape(l('To-dos for ','할 일 ')+path+(openTodos?' · '+l(openTodos+' open','남은 할 일 '+openTodos+'개'):''));
 const memoExpanded=todoMemoProject===group.id;
 const memoControls=memoExpanded?' aria-controls="'+todoMemoId(group.id)+'"':'';
 const deepPath=depth>PROJECT_INDENT_LIMIT?'<small>'+escape(projectPath(group.parent_id))+'</small>':'';
 const sessions=l(count.total+' '+(count.total===1?'session':'sessions'),'세션 '+count.total+'개');
 const level=attentionLevel(count.unread,count.newThreads);
 const attention=level?'<span class="treeAttention">'+(count.unread
  ?l(count.unread+' unread','미확인 '+count.unread+'개')
  :l(count.newThreads+' new','신규 '+count.newThreads+'개'))+'</span>':'';
 const badge=group.workspace_status===group.effective_status?workspaceBadge(group.workspace_status):'';
 const hiddenClass=group.effective_status==='hidden'?' hiddenFolder':'';
 return '<div class="treeProjectRow'+hiddenClass+attentionClass(level)+'" data-group="'+group.id+'">'+
  '<button class="treeFolder" draggable="true" data-drag-project="'+group.id+'" data-fold="'+group.id+'" title="'+escape(path)+'" aria-expanded="'+isOpen+'">'+
   '<span class="treeChevron" aria-hidden="true"></span><span class="treeFolderIcon" aria-hidden="true"></span>'+
   '<span class="treeFolderTitle">'+deepPath+'<span>'+escape(group.name)+badge+'</span></span></button>'+
  '<span class="treeCount">'+sessions+attention+'</span>'+
  '<button class="projectTodoButton" data-todo-memo="'+group.id+'" aria-expanded="'+memoExpanded+'"'+memoControls+' title="'+todosLabel+'" aria-label="'+todosLabel+'">'+
   '<span class="projectTodoIcon" aria-hidden="true"></span>'+(openTodos?'<small aria-hidden="true">'+openTodos+'</small>':'')+'</button>'+
  '<button class="groupOptions" data-project-options="'+group.id+'" title="'+escape(optionsLabel)+'" aria-label="'+escape(optionsLabel)+'">⋯</button></div>';
}
function todoList(items){
 return '<ul class="todoList" role="list">'+items.map(todo=>{
  const id=escape(todo.id),body=escape(todo.body);
  return '<li class="todoRow" data-todo-row="'+id+'">'+
   '<button class="todoDragHandle" type="button" draggable="true" data-drag-todo="'+id+'" aria-label="'+
    l('Move to-do · ','할 일 이동 · ')+body+'" title="'+l('Drag to move or click for options','드래그하여 이동하거나 클릭하여 옵션 열기')+'"><span aria-hidden="true">⠿</span></button>'+
   '<label class="todoItem"><input type="checkbox" data-todo-check="'+id+'" '+(todo.completed?'checked':'')+'><span>'+body+'</span></label></li>';
 }).join('')+'</ul>';
}
function indexTodos(){
 const byGroup=new Map(),openByBranch=new Map(),populated=new Set();
 let open=0;
 for(const todo of state.todos||[]){
  if(!byGroup.has(todo.group_id))byGroup.set(todo.group_id,[]);
  byGroup.get(todo.group_id).push(todo);
  populated.add(todo.group_id);
  if(!todo.completed){
   open++;
   openByBranch.set(todo.group_id,(openByBranch.get(todo.group_id)||0)+1);
  }
 }
 for(const {group} of projectRows().reverse()){
  if(group.parent_id===null||!populated.has(group.id))continue;
  populated.add(group.parent_id);
  openByBranch.set(group.parent_id,(openByBranch.get(group.parent_id)||0)+(openByBranch.get(group.id)||0));
 }
 return {byGroup,openByBranch,populated,open};
}
function todoHierarchy(root,todos,compact=false){
 const rows=projectRows(root).filter(({group})=>todos.populated.has(group.id));
 if(!rows.length){
  const message=root?l('No to-dos in this project or its subprojects.','이 프로젝트와 하위 프로젝트에 할 일이 없습니다.'):
   l('No to-dos yet.','아직 할 일이 없습니다.');
  return '<p class="'+(compact?'sub':'empty')+'">'+message+'</p>';
 }
 return rows.map(({group,depth})=>{
  const items=todos.byGroup.get(group.id)||[];
  const list=items.length?todoList(items):'';
  if(compact&&group.id===root)return list;
  const path=escape(projectPath(group.id)),heading=compact?'h4':'h2';
  const name=compact&&depth<=PROJECT_INDENT_LIMIT?escape(group.name):path;
  return '<section class="todoGroup" data-todo-project="'+group.id+'" style="--todo-depth:'+Math.min(depth,PROJECT_INDENT_LIMIT)+'">'+
   '<'+heading+' title="'+path+'">'+name+'</'+heading+'>'+list+'</section>';
 }).join('');
}
const todoMemoId=id=>id?'projectTodoMemo-'+id:'allProjectTodoMemo';
function projectTodoMemo(id,todos){
 const root=id||null;
 const title=root?l('To-dos for ','할 일 ')+projectPath(root):text('allTodos');
 return '<section class="projectTodoMemo'+(root?'':' rootTodoMemo')+'" id="'+todoMemoId(id)+'" aria-label="'+escape(title)+'"'+
  (root?' data-todo-project="'+escape(root)+'"':'')+'>'+
  '<header><h3>'+text(root?'todos':'allTodos')+'</h3><button class="closeTodoMemo" data-todo-memo="'+id+'" aria-label="'+
   l('Close checklist','할 일 목록 닫기')+'">×</button></header><div class="todoMemoBody">'+todoHierarchy(root,todos,true)+'</div>'+
  '<button class="openFullTodos" data-project-todos="'+id+'">'+l('Open in To-dos','할 일 탭에서 열기')+'</button></section>';
}
function workTree(root,counts,todos){
 const tree=document.createElement('ul');
 tree.className='workTree';
 tree.setAttribute('role','list');
 const roots=(root?[projects.get(root)]:(children.get(null)||[])).filter(group=>projectInTree(group,counts));
 const pending=[{list:tree,groups:roots,sessions:[],depth:0}];
 while(pending.length){
  const {list,groups,sessions,depth}=pending.pop();
  const entries=[...groups.map(group=>({group,count:counts.get(group.id)})),...sessions.map(session=>({session}))];
  const priority=({count,session})=>count?attentionLevel(count.unread,count.newThreads):attentionLevel(session.unread,session.is_new);
  if(depth)entries.sort((a,b)=>priority(b)-priority(a));
  for(const {group,count,session} of entries){
   if(session){
    list.insertAdjacentHTML('beforeend','<li class="treeSession" data-group="'+session.group_id+'" data-session="'+session.id+'">'+card(session)+'</li>');
    continue;
   }
   const node=document.createElement('li');
   node.className='treeProject';
   node.innerHTML=treeProjectRow(group,count,depth,todos.openByBranch.get(group.id)||0);
   if(todoMemoProject===group.id)node.insertAdjacentHTML('beforeend',projectTodoMemo(group.id,todos));
   list.append(node);
   const subprojects=(children.get(group.id)||[]).filter(child=>projectInTree(child,counts));
   if(treeFolds().has(group.id)||(!subprojects.length&&!count.sessions.length))continue;
   const branch=document.createElement('ul');
   branch.className=depth>=PROJECT_INDENT_LIMIT?'treeBranch compactBranch':'treeBranch';
   branch.setAttribute('role','list');
   node.append(branch);
   pending.push({list:branch,groups:subprojects,sessions:count.sessions,depth:depth+1});
  }
 }
 return tree;
}
const todoProjectOptions=groups=>groups.map(group=>'<option value="'+escape(group.id)+'">'+escape(projectPath(group.id))+'</option>').join('');
function renderTodos(area,todos){
 const groups=projectRows().map(({group})=>group);
 const options=todoProjectOptions(groups);
 const scope=$('todoScope'),destination=$('todoProject'),previous=destination.value;
 const optionsKey=lang+options;
 if(todoOptions!==optionsKey){
  scope.innerHTML='<option value="">'+l('All projects','모든 프로젝트')+'</option>'+options;
  destination.innerHTML='<option value="">'+l('Choose a project','프로젝트 선택')+'</option>'+options;
  todoOptions=optionsKey;
 }
 scope.value=filter||'';
 destination.value=filter||(projects.has(previous)?previous:'');
 $('todoDestination').hidden=Boolean(filter);
 $('addTodo').disabled=addingTodo||!groups.length;

 const html=groups.length?todoHierarchy(filter,todos):'<p class="empty">'+
  l('Create a project to add a to-do.','할 일을 추가하려면 프로젝트를 만드세요.')+'</p>';
 if(todoMarkup!==html){todoMarkup=html;area.innerHTML=html;}
}
async function moveTodo(id,group,neighbor=null,side='before'){
 await api('todo-move',{id,group,neighbor,side});
 await refresh(true);
}
function showTodoMove(id){
 const todo=todoById(id);
 if(!todo)return;
 const label=l('Move to-do','할 일 이동');
 dialog(label,'<p class="sub">'+escape(todo.body)+'</p><form id="moveTodoForm">'+
  '<label class="modalfield">'+text('project')+'<select id="todoMoveProject">'+todoProjectOptions(projectRows().map(({group})=>group))+'</select></label>'+
  '<label class="modalfield">'+l('Place before','앞에 놓을 할 일')+'<select id="todoMoveBefore"></select></label>'+
  '<button class="send" type="submit">'+label+'</button><p id="moveTodoError" class="sub" role="alert"></p></form>');
 const form=$('moveTodoForm'),project=$('todoMoveProject'),before=$('todoMoveBefore');
 const button=form.querySelector('[type=submit]'),error=$('moveTodoError');
 function positions(){
  before.innerHTML='<option value="">'+l('End of list','목록 끝')+'</option>'+(state.todos||[])
   .filter(other=>other.id!==id&&other.group_id===project.value&&other.completed===todo.completed)
   .map(other=>'<option value="'+escape(other.id)+'">'+escape(other.body)+'</option>').join('');
 }
 project.value=todo.group_id;positions();
 project.onchange=positions;
 form.onsubmit=async event=>{
  event.preventDefault();button.disabled=true;error.textContent='';
  try{await moveTodo(id,project.value,before.value||null);if(form.isConnected)$('dialog').close();}
  catch(e){error.textContent=e.message;}
  finally{button.disabled=false;}
 };
}
function redraw(){
 if(filter&&(!projects.has(filter)||(view!=='todos'&&!inMap(projects.get(filter)))))filter=null;
 const undoCount=state.undo?.count??0;
 $('undoCount').textContent=undoCount;
 $('undoWorkspace').disabled=undoing||undoCount===0;
 $('undoWorkspace').title=undoCount?l('Undo the last workspace edit','마지막 워크스페이스 편집 실행 취소'):l('No edits to undo','취소할 편집이 없습니다');
 const isTree=['map','active','updates'].includes(view);
 const showRootTodo=view==='map'&&!filter;
 if(!isTree||(!showRootTodo&&todoMemoProject===''))todoMemoProject=null;
 const sessions=treeSessions(),counts=projectCounts(sessions),groups=isTree?treeProjects(counts):[];
 const todos=indexTodos();
 const totals={active:0,hidden:0,excluded:0};
 for(const i of state.sessions)if(i.effective_status==='active')totals.active++;
 for(const i of [...state.sessions,...state.groups]){
  if(i.workspace_status===i.effective_status&&['hidden','excluded'].includes(i.workspace_status))totals[i.workspace_status]++;
 }
 totals.active+=state.groups.filter(g=>g.workspace_status==='active'&&g.effective_status==='active').length;
 $('mapCount').textContent=mapSessions().length;
 $('updatesCount').textContent=updateSessions().length;
 $('todosCount').textContent=todos.open;
 $('activeCount').textContent=totals.active;
 $('hiddenCount').textContent=totals.hidden;
 $('excludedCount').textContent=totals.excluded;
 const trashed=state.sessions.filter(i=>i.trashed_at!=null);
 $('trashCount').textContent=trashed.length;
 $('inboxCount').textContent=state.sessions.filter(i=>i.unread).length;
 const sidebarCounts=view==='active'||view==='updates'?projectCounts(mapSessions()):counts;
 const folderPriority=group=>{const count=sidebarCounts.get(group.id);return attentionLevel(count.unread,count.newThreads);};
 $('projectList').innerHTML=projectRows(null,true,inMap,(a,b)=>folderPriority(b)-folderPriority(a)).map(row=>sidebarProject(row,sidebarCounts.get(row.group.id))).join('');
 document.querySelectorAll('[data-view]').forEach(b=>b.classList.toggle('active',b.dataset.view===view));
 $('heading').textContent=view==='map'&&filter?projects.get(filter).name:text(view);
 const trail=view==='map'&&filter?projectPath(projects.get(filter).parent_id):'';
 $('projectTrail').textContent=trail?' › '+trail:'';
 const area=$('canvas');area.className=isTree?'board':'';$('viewAction').hidden=!isTree||!groups.length;
 $('visibilityFilter').hidden=view!=='map';
 $('folderSetupPrompt').hidden=!(state.folder_setup&&view==='map'&&!filter);
 $('borderLegend').hidden=!isTree;
 $('rootTodoBar').hidden=!showRootTodo;
 $('rootTodoCount').hidden=todos.open===0;
 $('rootTodoCount').textContent=todos.open;
 const rootButton=$('rootTodoButton');
 rootButton.setAttribute('aria-expanded',todoMemoProject==='');
 rootButton.setAttribute('aria-label',text('allTodos')+(todos.open?' · '+l(todos.open+' open','남은 할 일 '+todos.open+'개'):''));
 if(todoMemoProject==='')rootButton.setAttribute('aria-controls',todoMemoId(''));
 else rootButton.removeAttribute('aria-controls');
 $('todoTools').hidden=view!=='todos';
 if(view!=='todos')todoMarkup='';
 $('showHidden').checked=showHidden;
 $('viewAction').textContent=groups.every(({group})=>treeFolds().has(group.id))?l('Expand all','모두 펼치기'):l('Collapse all','모두 접기');
 if(isTree){
  const total=filter?counts.get(filter).total:sessions.length;
  $('summary').textContent=l('Projects','프로젝트')+' '+groups.length+' · '+l('Sessions','세션')+' '+total;
  const memoId=todoMemoProject===null?null:todoMemoId(todoMemoProject);
  const previousScroll=$(memoId)?.querySelector('.todoMemoBody')?.scrollTop??0;
  if(groups.length)area.replaceChildren(workTree(filter,counts,todos));
  else area.innerHTML='<p class="empty">'+(view==='updates'
   ?l('No new threads or unread replies.','새 스레드나 읽지 않은 답변이 없습니다.')
   :view==='active'?l('Mark a folder or session Active to see it here.','폴더나 세션을 진행 중으로 표시하면 여기에 나타납니다.')
    :l('Looking for local sessions. Open Connections to import web conversations or pair a browser.','로컬 세션을 찾는 중입니다. 연결 범위에서 웹 대화를 가져오거나 브라우저를 연결하세요.'))+'</p>';
  if(todoMemoProject==='')area.insertAdjacentHTML('afterbegin',projectTodoMemo('',todos));
  const memo=$(memoId);
  if(!memo)todoMemoProject=null;
  else memo.querySelector('.todoMemoBody').scrollTop=previousScroll;
  $('hint').textContent=groups.length&&view!=='updates'?l(
   'Drag a project onto another project to nest it, or onto Work map to move it to the top level. Drop a session card on a project, Hidden, or the message box.',
   '프로젝트를 다른 프로젝트에 놓으면 하위로, 작업 지도에 놓으면 최상위로 이동합니다. 세션 카드는 프로젝트, 숨김 또는 입력창에 놓으세요.'
  ):'';
 }else if(view==='todos'){
  const open=filter?(todos.openByBranch.get(filter)||0):todos.open;
  $('summary').textContent=l(open+(open===1?' open to-do':' open to-dos'),'남은 할 일 '+open+'개');
  renderTodos(area,todos);
  $('hint').textContent='';
 }else if(view==='inbox'){
  $('summary').textContent=l('Latest replies from visible sessions','표시 중인 세션의 최신 답변');
  area.innerHTML=state.inbox.map(m=>{
   const i=item(m.session);if(!i)return '';
   return '<article class="listitem"><span class="sub">'+(m.unread?'● ':'')+escape(i.provider)+' · '+stamp(m.at)+'</span>'+
    '<h3>'+escape(name(i))+'</h3><p>'+escape(m.body.slice(0,500))+'</p><div class="actions">'+
    (m.unread?markReadButton(i.id,m.seq,'choice'):'')+
    '<button class="choice" data-card="'+i.id+'">'+l('View source','원문 보기')+'</button>'+
    '<button class="choice" data-thread-reply="'+i.id+'">'+l('Reply here','이어서 답장')+'</button></div></article>';
  }).join('')||'<p class="empty">'+l('No replies yet.','받은 답변이 없습니다.')+'</p>';
  $('hint').textContent='';
 }else if(view==='trash'){
  $('summary').textContent=l('Restore a thread to its previous folder and status.','복구하면 스레드가 이전 폴더와 상태로 돌아갑니다.');
  area.innerHTML=trashed.sort((a,b)=>b.trashed_at-a.trashed_at).map(i=>{
   const location=i.workspace_status==='excluded'?text('excluded'):projectPath(i.group_id);
   return '<article class="listitem"><span class="sub">'+escape(location||i.provider)+'</span><h3>'+escape(name(i))+'</h3><div class="actions">'+
    '<button class="choice" data-card="'+i.id+'">'+l('View session','세션 보기')+'</button>'+
    '<button class="choice" data-restore-trash="'+i.id+'">'+l('Restore','복구')+'</button></div></article>';
  }).join('')||'<p class="empty">'+l('Trash is empty.','휴지통이 비어 있습니다.')+'</p>';
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
    '<button class="choice" data-restore="'+i.id+'">'+l('Restore','복구')+'</button>'+trashButton(i,'choice',text('trash'))+'</div></article>';
  });
  area.innerHTML=[...folders,...cards].join('')||'<p class="empty">'+(isHidden
   ?l('No hidden folders or sessions.','숨긴 폴더나 세션이 없습니다.')
   :l('Folders and sessions marked Will not consider will appear here.','고려 안 함으로 표시한 폴더와 세션이 여기에 나타납니다.'))+'</p>';
  $('hint').textContent='';
 }
 $('statusline').textContent=(state.scan==='Ready'?l('Synced locally','로컬 동기화됨'):state.scan||l('Connecting…','연결 중…'))+' · '+stamp(state.lastScan);
 renderChatControls();renderChat();preview();updateThreadEditor();
}
function setChatView(next){
 const conversation=next==='conversation';
 $('activity').hidden=conversation;$('chat').hidden=!conversation;
 document.querySelectorAll('[data-chat-view]').forEach(button=>button.setAttribute('aria-pressed',button.dataset.chatView===next));
 if(conversation)$('chat').scrollTop=$('chat').scrollHeight;
}
function selectOptions(select,html){
 if(select.dataset.options===html)return;
 const previous=select.value;
 select.innerHTML=html;select.dataset.options=html;
 select.value=Array.from(select.options).some(option=>option.value===previous)?previous:'';
}
function activityScope(){
 const params=new URLSearchParams();
 if($('activitySource').value)params.set('activity_provider',$('activitySource').value);
 if($('activityThread').value)params.set('activity_thread',$('activityThread').value);
 return params.toString();
}
function renderChatControls(){
 const sessions=state.sessions.filter(i=>!i.hidden).sort((a,b)=>name(a).localeCompare(name(b)));
 const sources=[...new Set(sessions.map(i=>i.provider))].sort();
 const option=(value,label)=>'<option value="'+escape(value)+'">'+escape(label)+'</option>';
 const threadOption=i=>option(i.id,name(i)+' · '+i.provider+' · '+(projectPath(i.group_id)||i.account));
 selectOptions($('chatTarget'),option('',l('Match active threads','진행 중인 스레드에서 찾기'))+sessions.map(threadOption).join(''));
 $('chatTarget').value=locked&&focus?focus:'';
 selectOptions($('activitySource'),option('',l('All sources','모든 서비스'))+sources.map(source=>option(source,source)).join(''));
 const provider=$('activitySource').value,threads=provider?sessions.filter(i=>i.provider===provider):sessions;
 selectOptions($('activityThread'),option('',l('All threads','모든 스레드'))+threads.map(threadOption).join(''));
 const scope=activityScope();
 const replies=!scope?state.inbox:state.activityScope===scope?state.activity??null:null;
 const html=replies===null?'<p class="sub">'+l('Loading replies…','답변을 불러오는 중…')+'</p>':replies.map(reply=>{
  const i=item(reply.session);
  if(!i||i.hidden)return '';
  return '<article class="activityReply'+(reply.unread?' unreadReply':'')+'"><p class="sub">'+
   (reply.unread?l('New reply · ','새 답변 · '):'')+escape(i.provider)+' · '+stamp(reply.at)+'</p>'+
   '<button class="activityThread" type="button" data-card="'+i.id+'">'+escape(name(i))+'</button>'+
   '<p class="sub">'+escape(projectPath(i.group_id)||i.account)+'</p><p class="activityExcerpt">'+escape(reply.body)+'</p>'+
   '<div class="activityActions">'+(reply.unread?markReadButton(i.id,reply.seq,'linkbtn'):'')+
   '<button class="linkbtn" type="button" data-chat-target="'+i.id+'">'+l('Reply in Main chat','메인 대화에서 답장')+'</button></div></article>';
 }).join('')||'<p class="sub">'+l('No replies for this selection.','선택한 범위에 답변이 없습니다.')+'</p>';
 if(activityMarkup!==html){
  activityMarkup=html;
  $('activityFeed').innerHTML=html;
 }
}
function plan(){return route($('prompt').value,state.sessions,focus,$('mode').value,locked);}
const noActiveMatch=()=>l('No matching active thread. Mark a thread or folder Active, or choose a destination.',
 '일치하는 진행 중 스레드가 없습니다. 스레드나 폴더를 진행 중으로 표시하거나 보낼 곳을 선택하세요.');
function preview(){
 const value=$('prompt').value.trim(),target=item(focus);
 if(!value&&target&&!target.hidden&&(locked||isActiveThread(target))){$('dest').innerHTML='↳ '+escape(path(target))+' · '+escape(target.provider)+' <button type="button" data-clear>'+l('Clear','해제')+'</button>';return;}
 const p=value?plan():null;
 $('dest').textContent=!p?'':!p.candidates.length?l('No matching active thread','일치하는 진행 중 스레드 없음'):
  !p.action?l('Search active threads','진행 중인 스레드 검색'):p.candidates.length!==1?l('Choose a destination','대상 선택'):
  p.confirm?l('Confirm before sending · ','전송 전 확인 · ')+path(p.candidates[0])+' · '+p.candidates[0].provider:
  (p.candidates[0].direct?l('To · ','보낼 곳 · '):l('Read-only · ','읽기 전용 · '))+path(p.candidates[0])+' · '+p.candidates[0].provider;
}
function pin(id,explicit){const i=item(id);if(!i||i.hidden)return;focus=id;locked=explicit;save('target',focus);save('locked',locked);redraw();}
function selectChatTarget(id){
 if(id){$('mode').value='send';pin(id,true);}
 else{focus=null;locked=false;save('target',null);save('locked',false);$('mode').value='auto';redraw();}
}
function push(m){messages.push(m);messages=messages.slice(-70);save('chat',messages);renderChat();}
function renderChat(){
 const box=$('chat'),bottom=box.scrollHeight-box.clientHeight-box.scrollTop<55;
 const html=messages.map((m,n)=>{
  if(m.kind==='user')return '<div class="utter">'+escape(m.body)+'</div>';
  let h='';
  if(m.kind==='source')h='<b>'+escape(m.path)+'</b><div class="quote"><span class="sub">'+escape(m.provider)+' · '+stamp(m.at)+'</span><p>'+escape(m.body)+'</p><button class="linkbtn" data-card="'+m.session+'">'+l('View source','원문 보기')+' ↗</button></div>';
  if(m.kind==='choose'){
   const candidates=m.ids.map(item).filter(i=>m.chosen?i?.id===m.chosen&&!i.hidden:isActiveThread(i));
   h=candidates.length?l('Which thread did you mean?','어느 스레드인가요?')+'<div class="replyopts">'+candidates.map(i=>
    '<button '+(m.chosen?'disabled':'')+' class="choice" data-select="'+i.id+'" data-n="'+n+'"><b>'+escape(path(i))+
    '</b> · '+escape(i.provider)+'<br>'+escape(i.latest.slice(0,125))+'</button>').join('')+'</div>':escape(noActiveMatch());
  }
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
  const req=++requestSeq,scope=activityScope(),params=new URLSearchParams(scope);
  if(received!==null)params.set('after',received);
  const s=await api('state?'+params);
  if(req<appliedSeq)return;appliedSeq=req;
  indexProjects(s.groups);
  for(const i of s.sessions){i.project=projectPath(i.group_id);if(i.state==='idle'&&i.latest.trim().endsWith('?'))i.state='wait';}
  for(const m of s.events||[]){
   if(received!==null&&m.seq<=received)continue;
   const i=s.sessions.find(x=>x.id===m.session);
   if(i){toast(l('New reply · ','새 답변 · ')+name(i));if(messages.some(v=>v.kind==='delivery'&&v.session===i.id)&&!messages.some(v=>v.event===m.seq))
    push({kind:'source',event:m.seq,session:i.id,path:i.project+' › '+name(i),provider:i.provider,at:m.at,body:m.body});}
  }
  received=s.cursor;ready=true;state=s;s.activityScope=scope;
  const target=item(focus);
  if(focus&&(!target||target.hidden||(!locked&&!isActiveThread(target)))){
   focus=null;locked=false;save('target',null);save('locked',false);
  }
  const signature=JSON.stringify([s.scan,s.codex,s.claude,s.undo,s.folder_setup,s.groups,s.todos,s.inbox,scope,s.activity,s.deliveries,s.sessions.map(({live_at,synced_at,...i})=>i)]);
  if(!dragging&&(force||signature!==lastPaint)){lastPaint=signature;redraw();}

 }catch(e){$('statusline').textContent=l('Disconnected · ','연결 끊김 · ')+e.message;}finally{polling=false;}
}
const historyMarkup=history=>history.map(m=>'<section><strong>'+escape(m.role)+'</strong> <small>'+stamp(m.at)+'</small><p>'+escape(m.body)+'</p></section>').join('');
const threadRevision=(session,delivery)=>JSON.stringify([session.latest_at,session.latest,delivery?.status,delivery?.updated]);
function updateThreadEditor(){
 const editor=threadEditor;
 if(!editor||!$('dialog').open)return;
 const session=item(editor.id);
 const connected=session?.direct&&!session.hidden;
 editor.send.disabled=!connected||editor.sending;
 editor.copy.hidden=Boolean(connected);
 const receipt=editor.receipt;
 const delivery=receipt&&(state.deliveries.find(entry=>entry.id===receipt.id)||receipt);
 const status=delivery&&{
  queued:l('Queued','대기 중'),sending:l('Sending…','전송 중…'),claimed:l('Sending…','전송 중…'),
  delivered:l('Sent · Waiting for reply','전송됨 · 답변 대기 중'),answered:l('Reply received','답변 수신'),
  failed:l('Send failed','전송 실패'),uncertain:l('Delivery unconfirmed','전송 확인 안 됨'),
 }[delivery.status];
 const failure=delivery&&['failed','uncertain'].includes(delivery.status)&&delivery.detail;
 const notice=l('Read-only · Copy your draft to continue in the original conversation.','읽기 전용 · 원본 대화에서 이어가려면 초안을 복사하세요.');
 editor.status.textContent=failure?status+' · '+delivery.detail:status||(!connected?notice:'');
 if(!session)return;
 const revision=threadRevision(session,delivery);
 if(editor.loading||editor.revision===revision)return;
 editor.loading=true;
 api('history?id='+encodeURIComponent(editor.id)).then(history=>{
  if(threadEditor!==editor||!$('dialog').open)return;
  const list=editor.history;
  const bottom=list.scrollHeight-list.clientHeight-list.scrollTop<25;
  const markup=historyMarkup(history);
  if(editor.markup!==markup){
   editor.markup=markup;list.innerHTML=markup;
   if(bottom)list.scrollTop=list.scrollHeight;
  }
  editor.revision=revision;
 }).catch(()=>{}).finally(()=>{editor.loading=false;});
}
function showThreadReply(){
 const editor=threadEditor;
 editor.form.hidden=false;
 $('dialogContent').querySelector('[data-thread-reply]').setAttribute('aria-expanded','true');
 editor.input.focus({preventScroll:true});
 editor.form.scrollIntoView({block:'nearest',inline:'nearest'});
}
function wireThreadReply(session,markup){
 const form=$('threadReply'),input=$('threadReplyText'),errorBox=$('threadReplyError');
 const editor={id:session.id,form,input,send:$('threadReplySend'),copy:$('threadReplyCopy'),status:$('threadReplyStatus'),
  history:$('threadHistory'),markup,revision:threadRevision(session,null)};
 threadEditor=editor;
 editor.copy.onclick=async()=>{
  if(!input.value.trim()){input.focus();return;}
  try{await navigator.clipboard.writeText(input.value);errorBox.textContent=l('Draft copied','초안 복사됨');}
  catch(error){errorBox.textContent=error.message;}
 };
 input.oninput=()=>{editor.key=null;};
 form.onsubmit=async event=>{
  event.preventDefault();
  const body=input.value.trim(),target=item(editor.id);
  if(!body||!target?.direct||target.hidden||editor.sending)return;
  editor.key||=crypto.randomUUID();
  editor.sending=true;editor.send.disabled=true;errorBox.textContent='';
  try{
   editor.receipt=await queueReply(target,body,editor.key);
   if(input.value.trim()===body){input.value='';editor.key=null;}
   await refresh(true);
  }catch(error){errorBox.textContent=error.message;}
  finally{editor.sending=false;updateThreadEditor();}
 };
 updateThreadEditor();
}
function wireThreadRename(id){
 const toggle=$('renameThread'),form=$('threadRename'),input=$('threadName'),cancel=$('cancelThreadRename');
 const saveButton=$('saveThreadName'),original=$('originalThreadName'),errorBox=$('threadNameError');
 const controls=[toggle,input,cancel,saveButton,original];
 function closeForm(){
  form.hidden=true;toggle.setAttribute('aria-expanded','false');toggle.focus({preventScroll:true});
 }
 toggle.onclick=()=>{
  if(!form.hidden){closeForm();return;}
  const thread=item(id);
  input.value=thread?.title||'';
  original.hidden=!thread?.custom_title;
  errorBox.textContent='';form.hidden=false;toggle.setAttribute('aria-expanded','true');
  input.focus();input.select();
 };
 cancel.onclick=closeForm;
 form.onkeydown=event=>{
  if(event.key==='Escape'&&!input.disabled){event.preventDefault();closeForm();}
 };
 async function saveTitle(title){
  for(const control of controls)control.disabled=true;
  errorBox.textContent='';
  try{
   await edit(id,{title});
   if(form.isConnected&&$('dialog').open){
    $('dialogTitle').textContent=name(item(id));
    toggle.disabled=false;closeForm();
   }
  }catch(error){errorBox.textContent=error.message;}
  finally{for(const control of controls)control.disabled=false;}
 }
 original.onclick=()=>saveTitle(null);
 form.onsubmit=event=>{
  event.preventDefault();
  const title=input.value.trim();
  if(!title){
   errorBox.textContent=l('Enter a thread name.','스레드 이름을 입력하세요.');input.focus();return;
  }
  if(title===item(id)?.title){closeForm();return;}
  return saveTitle(title);
 };
}
async function detail(id,replyNow=false){
 const i=item(id);if(!i)return;const history=await api('history?id='+encodeURIComponent(id));
 const trashed=i.trashed_at!=null;
 if(!trashed)await api('edit',{id,read:true,read_through:history.reduce((latest,m)=>Math.max(latest,m.seq??0),0)});
 const excluded=i.effective_status==='excluded';
 const location=i.workspace_status==='excluded'?text('excluded'):projectPath(i.group_id);
 const inherited=!trashed&&i.effective_status!==i.workspace_status
  ?'<p class="sub">'+l('From folder: ','폴더에서 적용: ')+text(i.effective_status)+'</p>':'';
 const reply=i.hidden?'':'<button class="choice" data-thread-reply="'+i.id+'" aria-expanded="false" aria-controls="threadReply">'+l('Reply here','이어서 답장')+'</button>';
 const original=i.url?'<a class="choice" '+(i.url.startsWith('codex:')?'':'target="_blank" rel="noopener noreferrer" ')+
  'href="'+escape(i.url)+'">'+l('Open original','원본 열기')+'</a>':'';
 const rename='<button class="choice" type="button" id="renameThread" aria-expanded="false" aria-controls="threadRename">'+l('Rename','이름 변경')+'</button>';
 const nameForm='<form class="threadRename" id="threadRename" hidden><label class="modalfield" for="threadName">'+
  l('Thread name','스레드 이름')+'<input id="threadName" type="text" required maxlength="240" autocomplete="off"></label>'+
  '<div class="actions"><button class="send" type="submit" id="saveThreadName">'+l('Save','저장')+'</button>'+
  '<button class="choice" type="button" id="cancelThreadRename">'+l('Cancel','취소')+'</button>'+
  '<button class="choice" type="button" id="originalThreadName" hidden>'+l('Use original title','원본 이름 사용')+'</button></div>'+
  '<p class="sub" id="threadNameError" role="alert"></p></form>';
 const move=excluded?'':'<label class="modalfield">'+l('Move to project','프로젝트 이동')+'<select id="moveSelect">'+projectSelectOptions(i.group_id)+'</select></label>';
 const trashAction=trashed?'<button class="choice" data-restore-trash="'+id+'">'+l('Restore','복구')+'</button>'
  :trashButton(i,'choice',l('Move to Trash','휴지통으로 이동'));
 const workspace=trashed?'':'<label class="modalfield">'+l('Workspace status','워크스페이스 상태')+
  '<select id="workspaceStatus" aria-describedby="workspaceStatusHelp">'+workspaceStatusOptions(i.workspace_status)+'</select></label>'+
  '<div id="workspaceStatusHelp" class="sub">'+l('Hidden stays in its folder. Will not consider removes it from the tree and routing.','숨긴 세션은 프로젝트에 남고, 고려 안 함은 트리와 라우팅에서 빠집니다.')+'</div>'+
  inherited+move+'<p id="sessionEditError" class="sub" role="alert"></p>';
 const historyHtml=historyMarkup(history);
 const composer=i.hidden?'':'<form class="threadReply" id="threadReply" hidden>'+
  '<label for="threadReplyText">'+l('Reply to this thread','이 스레드에 답장')+'</label>'+
  '<div class="inputbox"><textarea id="threadReplyText" required maxlength="50000" rows="2"></textarea></div>'+
  '<div class="actions"><button class="send" type="submit" id="threadReplySend">'+l('Send reply','답장 보내기')+'</button>'+
  '<button class="choice" type="button" id="threadReplyCopy" hidden>'+l('Copy draft','초안 복사')+'</button></div>'+
  '<p class="sub" id="threadReplyStatus" role="status"></p><p class="sub" id="threadReplyError" role="alert"></p></form>';
 dialog(name(i),'<p class="sub">'+escape(i.provider)+' · '+escape(i.account)+' · '+escape(location)+'</p>'+
  '<p>'+escape(status(i))+' · '+l('Last seen ','마지막 확인 ')+stamp(i.synced_at)+'</p>'+
  '<div class="actions">'+rename+reply+original+trashAction+'</div>'+nameForm+workspace+
  '<div class="history" id="threadHistory">'+historyHtml+'</div>'+composer);
 wireThreadRename(id);
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
 const hist=$('threadHistory');
 hist.scrollTop=hist.scrollHeight;
 if(composer){
  wireThreadReply(i,historyHtml);
  if(replyNow)showThreadReply();
 }
 await refresh(true);
}
async function edit(id,changes){await api('edit',{id,...changes});await refresh(true);}
async function toggleCardStatus(button){
 const id=button.closest('.treeSession').dataset.session;
 const status=button.dataset.sessionStatus;
 const workspace_status=item(id).workspace_status===status?'default':status;
 button.disabled=true;
 try{
  await edit(id,{workspace_status});
  const updated=$('canvas').querySelector('[data-session="'+id+'"] [data-session-status="'+status+'"]');
  if(updated)updated.focus({preventScroll:true});
  else if(workspace_status==='hidden')toast(l('Thread hidden. Find it in Hidden.','스레드를 숨겼습니다. 숨김에서 찾을 수 있습니다.'));
 }finally{button.disabled=false;}
}
async function changeTrash(id,trashed,button){
 button.disabled=true;
 try{
  await api('trash-session',{id,trashed});
  if(button.closest('#dialog'))$('dialog').close();
  await refresh(true);
  toast(trashed?l('Thread moved to Trash','스레드를 휴지통으로 이동했습니다'):l('Thread restored','스레드를 복구했습니다'));
 }finally{button.disabled=false;}
}
async function undoWorkspace(){
 const button=$('undoWorkspace');
 undoing=true;
 button.disabled=true;
 try{
  await api('undo',{id:state.undo.id});
  toast(l('Last change undone','마지막 변경을 취소했습니다'));
 }finally{
  undoing=false;
  await refresh(true);
  if(!button.disabled)button.focus({preventScroll:true});
 }
}
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
function showFolderSetup(){
 const roots=['Research','Life','Study','Unclassified'].map(name=>({name,children:[],fixed:name==='Unclassified'}));
 dialog(text('setupFolders'),'<form id="folderSetupForm"><p class="sub">'+l(
  'Rename the starter folders or add subfolders. Unmatched imports go into Unclassified.',
  '기본 폴더의 이름을 바꾸거나 하위 폴더를 추가하세요. 분류할 수 없는 항목은 Unclassified에 들어갑니다.'
 )+'</p><div id="folderSetupTree" class="folderSetupTree"></div><div class="actions">'+
  '<button class="choice" id="setupAddRoot" type="button">'+l('Add a folder','폴더 추가')+'</button>'+
  '<button class="send" id="setupSave" type="submit">'+l('Create folders','폴더 만들기')+'</button></div>'+
  '<p id="folderSetupError" class="sub" role="alert"></p></form>');
 const tree=$('folderSetupTree'),form=$('folderSetupForm'),submit=$('setupSave'),error=$('folderSetupError');
 let rows=[];
 function draw(focusFolder=null){
  rows=[];
  const pending=roots.map(folder=>({folder,siblings:roots,parentPath:[]})).reverse();
  while(pending.length){
   const {folder,siblings,parentPath}=pending.pop();
   const path=[...parentPath,folder.name.trim()];
   rows.push({folder,siblings,path});
   for(let i=folder.children.length-1;i>=0;i--){
    pending.push({folder:folder.children[i],siblings:folder.children,parentPath:path});
   }
  }
  tree.innerHTML=rows.map(({folder,path},index)=>{
   const parent=escape(path.filter(Boolean).join(' › '));
   return '<div class="setupFolder" data-setup-row="'+index+'" style="--setup-depth:'+Math.min(path.length-1,PROJECT_INDENT_LIMIT)+'">'+
    '<input data-setup-name required maxlength="160" aria-label="'+l('Folder name','폴더 이름')+'" value="'+escape(folder.name)+'" '+(folder.fixed?'readonly':'')+'>'+
    '<div class="setupRowActions"><button class="choice" type="button" data-setup-child aria-label="'+l('Add subfolder under ','하위 폴더 추가 ')+parent+'">'+
    l('Add subfolder','하위 폴더 추가')+'</button>'+(folder.fixed?'':'<button class="setupRemove" type="button" data-setup-remove aria-label="'+
     l('Remove folder ','폴더 제거 ')+parent+'">×</button>')+'</div></div>';
  }).join('');
  if(focusFolder){
   const index=rows.findIndex(row=>row.folder===focusFolder);
   tree.querySelector('[data-setup-row="'+index+'"] [data-setup-name]')?.focus();
  }
 }
 function addFolder(siblings){
  const folder={name:'',children:[]};
  siblings.push(folder);draw(folder);
 }
 draw();
 tree.oninput=event=>{
  const row=event.target.closest('[data-setup-row]');
  if(row&&event.target.matches('[data-setup-name]'))rows[Number(row.dataset.setupRow)].folder.name=event.target.value;
 };
 tree.onclick=event=>{
  const button=event.target.closest('button'),row=button?.closest('[data-setup-row]');
  if(!row)return;
  const {folder,siblings}=rows[Number(row.dataset.setupRow)];
  if(button.hasAttribute('data-setup-child'))addFolder(folder.children);
  if(button.hasAttribute('data-setup-remove')){
   const index=siblings.indexOf(folder);
   siblings.splice(index,1);draw(siblings[Math.min(index,siblings.length-1)]);
  }
 };
 $('setupAddRoot').onclick=()=>addFolder(roots);
 form.onsubmit=async event=>{
  event.preventDefault();
  draw();
  const paths=rows.map(row=>row.path);
  error.textContent='';submit.disabled=true;
  try{
   await api('folder-setup',{paths});
   if(form.isConnected)$('dialog').close();
   await refresh(true);
  }catch(e){error.textContent=e.message;}
  finally{submit.disabled=false;}
 };
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
  '<button class="choice" data-project-todos="'+id+'">'+text('todos')+'</button>'+
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
async function queueReply(i,body,key,activeOnly=false){
 const delivery=await api('send',{id:key,session:i.id,body,active_only:activeOnly});
 if(!messages.some(message=>message.kind==='delivery'&&message.key===key)){
  push({kind:'delivery',session:i.id,key,body,path:path(i),status:delivery.status});
 }
 return delivery;
}
async function handle(i,body,action,key,explicit=false){
 if(!i||i.hidden)throw Error(l('Session unavailable','세션을 사용할 수 없습니다'));
 if(!explicit&&!isActiveThread(i))throw Error(noActiveMatch());
 if(!action){if(!locked)pin(i.id,false);push({kind:'source',session:i.id,path:path(i),provider:i.provider,at:i.latest_at,body:i.latest||l('No assistant reply yet.','아직 답변이 없습니다.')});return;}
 if(!i.direct){push({kind:'readonly',path:path(i),session:i.id,body});return;}
 await queueReply(i,body,key,!explicit);if(!locked)pin(i.id,false);await refresh(true);
}
$('composer').onsubmit=async e=>{
 e.preventDefault();const body=$('prompt').value.trim();if(!body)return;const p=plan();
 const key=draftID||crypto.randomUUID();draftID=key;save('draftID',key);
 const submit=$('composer').querySelector('[type=submit]');if(submit.disabled)return;submit.disabled=true;
 try{setChatView('conversation');
  if(!messages.some(m=>m.kind==='user'&&m.key===key))push({kind:'user',body,key});
  if(!p.candidates.length)push({kind:'info',body:noActiveMatch()});
  else if(p.candidates.length===1&&!p.waiting&&!p.confirm)await handle(p.candidates[0],body,p.action,key,p.explicit);
  else push({kind:'choose',ids:p.candidates.map(i=>i.id),body,action:p.action,key});
  if($('prompt').value.trim()===body){$('prompt').value='';save('draft','');draftID=null;save('draftID',null);}preview();
 }catch(err){toast(err.message);}finally{submit.disabled=false;}
};
async function settings(){
 const info=await api('pairing');
 const connectionCounts=()=>['Codex','Claude Code','ChatGPT','Claude'].map(p=>{const list=state.sessions.filter(i=>i.provider===p);return '<p><b>'+p+'</b> · '+list.length+' '+l('indexed','색인됨')+' · '+list.filter(i=>i.direct).length+' '+l('writable','전송 가능')+'</p>';}).join('');
 const accounts=[...new Set(state.sessions.filter(i=>i.provider==='ChatGPT').map(i=>i.account))];
 const account=escape(load('chatgptAccount',accounts.length===1?accounts[0]:'Personal'));
 const codexImport='<button class="choice" id="importCodex">'+l('Import Codex history','Codex 기록 가져오기')+'</button><p id="codexImportResult" class="sub" role="status"></p>';
 const chatgptRefresh='<div id="chatgptRefresh"><button class="choice" id="refreshChatGPT">'+l('Refresh ChatGPT','ChatGPT 새로 고침')+'</button><p class="sub">'+
  l('Download a ChatGPT export ZIP to Downloads. Refresh imports the latest ZIP using the account label above.','ChatGPT 내보내기 ZIP을 다운로드 폴더에 저장하세요. 새로 고침을 누르면 위 계정 이름으로 최신 ZIP을 가져옵니다.')+' '+
  '<a class="exportGuide" href="https://help.openai.com/en/articles/7260999-exporting-your-chatgpt-history-and-data" target="_blank" rel="noopener noreferrer">'+l('How to export ChatGPT chats','ChatGPT 대화 내보내기 방법')+'</a></p></div>';
 dialog(text('connections'),'<div id="providerCounts">'+connectionCounts()+'</div>'+codexImport+
  '<label class="modalfield"><span>'+l('Local notifications','로컬 알림')+'</span><input id="alerts" type="checkbox" '+(state.notifications?'checked':'')+'></label><hr><p><b>'+l('Import conversation history','대화 기록 가져오기')+'</b></p><label class="modalfield">'+l('Service','서비스')+'<select id="provider"><option>ChatGPT</option><option>Claude</option></select></label><label class="modalfield">'+l('Account label','계정 이름')+'<input id="account" value="'+account+'"></label>'+chatgptRefresh+
  '<label class="modalfield">'+l('Export file (.json or .zip)','내보내기 파일 (.json 또는 .zip)')+'<input id="file" type="file" accept=".json,.zip"></label><p id="importResult" role="status"></p><hr><p><b>'+l('Connect a browser tab','브라우저 탭 연결')+'</b></p><p class="sub">'+l('Load the extension folder in Chrome, open a source conversation, then paste the pairing key into the extension. Use the same account label when importing.','Chrome에 extension 폴더를 로드한 뒤 원본 대화에서 확장 프로그램에 연결 키를 붙여 넣으세요. 가져오기와 동일한 계정 이름을 사용하세요.')+'</p><button class="choice" id="copyKey">'+l('Copy pairing key','연결 키 복사')+'</button>');
 const counts=$('providerCounts'),importResult=$('importResult'),refreshChatGPT=$('refreshChatGPT');
 const refreshCounts=async()=>{await refresh(true);counts.innerHTML=connectionCounts();};
 const button=$('importCodex'),result=$('codexImportResult');
 button.onclick=async()=>{
  button.disabled=true;
  result.textContent=l('Importing Codex history…','Codex 기록을 가져오는 중…');
  try{
   const {added,already_indexed,failed}=await api('import-codex',{});
   result.textContent=l('Added '+added+' · Already indexed '+already_indexed+' · Unavailable '+failed,
    '추가 '+added+'개 · 기존 '+already_indexed+'개 · 실패 '+failed+'개');
   await refreshCounts();
  }catch(error){
   result.textContent=error.message;
  }finally{
   button.disabled=false;
  }
 };
 $('alerts').onchange=async e=>{await api('notifications',{enabled:e.target.checked});await refresh(true);};
 $('copyKey').onclick=async()=>{await navigator.clipboard.writeText(info.port+':'+info.token);toast(l('Pairing key copied','연결 키 복사됨'));};
 const provider=$('provider'),accountInput=$('account');
 provider.onchange=()=>$('chatgptRefresh').hidden=provider.value!=='ChatGPT';
 refreshChatGPT.onclick=async()=>{
  const account=accountInput.value.trim();
  refreshChatGPT.disabled=true;
  importResult.title='';
  importResult.textContent=l('Checking Downloads…','다운로드 폴더를 확인하는 중…');
  try{
   const {result}=await api('refresh-chatgpt',{account});
   await refreshCounts();
   if(!result){
    importResult.textContent=l('No ChatGPT export ZIP found in Downloads.','다운로드 폴더에 ChatGPT 내보내기 ZIP이 없습니다.');
    return;
   }
   save('chatgptAccount',account);
   importResult.title=result.file;
   importResult.textContent=result.new_chats||result.new_messages
    ?l('New chats: '+result.new_chats+' · New messages: '+result.new_messages,'새 대화 '+result.new_chats+'개 · 새 메시지 '+result.new_messages+'개')
    :l('No new chats or messages in the latest export.','최신 내보내기에 새 대화나 메시지가 없습니다.');
  }catch(error){importResult.textContent=error.message;}
  finally{refreshChatGPT.disabled=false;}
 };
 $('file').onchange=async e=>{
  const file=e.target.files[0];if(!file)return;
  const selectedProvider=provider.value,account=accountInput.value.trim();
  importResult.title='';importResult.textContent=l('Importing…','가져오는 중…');
  try{const parameters=new URLSearchParams({provider:selectedProvider,account});
   const response=await fetch('/import?'+parameters,{method:'POST',headers:{'Content-Type':'application/octet-stream'},body:file});const d=await response.json();if(!response.ok)throw Error(d.error||'Import failed');
   if(selectedProvider==='ChatGPT')save('chatgptAccount',account);
   importResult.textContent=l('Imported ','가져온 세션 ')+d.count;await refreshCounts();
  }catch(err){importResult.textContent=err.message;}
  finally{e.target.value='';}
 };
}
function toggleTodoMemo(id){
 todoMemoProject=todoMemoProject===id?null:id;
 redraw();
 const trigger=id?$('canvas').querySelector('.projectTodoButton[data-todo-memo="'+id+'"]'):$('rootTodoButton');
 trigger?.focus({preventScroll:true});
 if(todoMemoProject!==null)$(todoMemoId(id))?.scrollIntoView({block:'nearest',inline:'nearest'});
}
document.addEventListener('click',async e=>{
 const b=e.target.closest('button');if(!b)return;const k=b.dataset;
 try{
  if(k.lang){lang=k.lang;save('lang',lang);applyLanguage();}
  if(k.view){view=k.view;filter=null;todoMemoProject=null;redraw();}
  if(k.project){filter=filter===k.project?null:k.project;expandProject(k.project);view='map';todoMemoProject=null;redraw();}
  if(k.fold){
   const scope=b.closest('#canvas')?'canvas':'projectList';
   const set=scope==='canvas'?treeFolds():folded;
   set.has(k.fold)?set.delete(k.fold):set.add(k.fold);
   if(set===folded)save('folded',[...folded]);
   redraw();
   $(scope).querySelector('[data-fold="'+k.fold+'"]')?.focus({preventScroll:true});
  }
  if(k.projectOptions)showProjectOptions(k.projectOptions);
  if(k.dragTodo)showTodoMove(k.dragTodo);
  if(k.todoMemo!==undefined)toggleTodoMemo(k.todoMemo);
  if(k.projectTodos!==undefined){
   view='todos';filter=k.projectTodos||null;
   if($('dialog').open)$('dialog').close();
   redraw();
   window.scrollTo({top:0,left:0});
  }
  if(k.newSubproject)await createProject(k.newSubproject);
  if(k.card)await detail(k.card);
  if(k.sessionStatus)await toggleCardStatus(b);
  if(k.markRead){
   b.disabled=true;
   try{
    await edit(k.markRead,{read:true,read_through:Number(k.readThrough)});
    toast(text('markedRead'));
   }finally{b.disabled=false;}
  }
  if(k.threadReply){
   if(threadEditor?.id===k.threadReply&&$('dialog').open)showThreadReply();
   else await detail(k.threadReply,true);
  }
  if(k.chatView)setChatView(k.chatView);
  if(k.chatTarget){selectChatTarget(k.chatTarget);$('prompt').focus();}
  if(k.restore)await edit(k.restore,{workspace_status:'default'});
  if(k.trashSession||k.restoreTrash)await changeTrash(k.trashSession||k.restoreTrash,Boolean(k.trashSession),b);
  if(k.restoreProject)await updateProjectStatus(k.restoreProject,'default');
  if('clear' in k)selectChatTarget('');
  if(k.select){const m=messages[Number(k.n)];if(m.chosen)return;m.chosen=k.select;renderChat();try{await handle(item(k.select),m.body,m.action,m.key);save('chat',messages);}catch(err){delete m.chosen;renderChat();throw err;}}
  if('copy' in k)await navigator.clipboard.writeText(messages[Number(k.copy)].body);
  if(b.id==='closeDialog')$('dialog').close();
  if(b.id==='jumpToChat')$('chatPanel').scrollIntoView({block:'start',inline:'nearest'});
  if(b.id==='connections')await settings();
  if(b.id==='openFolderSetup')showFolderSetup();
  if(b.id==='skipFolderSetup'){
   b.disabled=true;
   try{await api('folder-setup',{paths:null});await refresh(true);}
   finally{b.disabled=false;}
  }
  if(b.id==='addGroup')await createProject();
  if(b.id==='undoWorkspace')await undoWorkspace();
  if(b.id==='viewAction'){
   const ids=treeProjects(projectCounts(treeSessions())).map(({group})=>group.id),set=treeFolds();
   const collapse=!ids.every(id=>set.has(id));
   for(const id of ids)collapse?set.add(id):set.delete(id);
   if(set===folded)save('folded',[...folded]);
   redraw();
  }
 }catch(err){toast(err.message);}
});
$('todoScope').onchange=event=>{filter=event.target.value||null;redraw();};
$('todoForm').onsubmit=async event=>{
 event.preventDefault();
 if(addingTodo)return;
 const input=$('todoText'),body=input.value.trim(),group=$('todoProject').value;
 if(!body){input.focus();return;}
 addingTodo=true;
 $('addTodo').disabled=true;
 try{
  await api('todo-create',{group,body});
  if(input.value.trim()===body)input.value='';
  await refresh(true);
  if(view==='todos')input.focus();
 }catch(error){toast(error.message);}
 finally{addingTodo=false;$('addTodo').disabled=!projects.size;}
};
$('canvas').addEventListener('change',async event=>{
 const box=event.target,id=box.dataset.todoCheck;
 if(!id)return;
 const completed=box.checked,keepFocus=document.activeElement===box;
 box.disabled=true;
 try{
  await api('todo-completed',{id,completed});
  await refresh(true);
  if(keepFocus)$('canvas').querySelector('[data-todo-check="'+id+'"]')?.focus({preventScroll:true});
 }catch(error){box.checked=!completed;toast(error.message);}
 finally{box.disabled=false;}
});
document.addEventListener('keydown',event=>{
 if(event.key==='Escape'&&todoMemoProject!==null&&event.target.closest('.projectTodoMemo,.projectTodoButton[aria-expanded=true]')){
  event.preventDefault();toggleTodoMemo(todoMemoProject);
 }
});
$('mode').onchange=preview;$('prompt').oninput=()=>{draftID=null;save('draftID',null);save('draft',$('prompt').value);preview();};
$('chatTarget').onchange=event=>selectChatTarget(event.target.value);
$('activitySource').onchange=()=>{
 $('activityThread').value='';renderChatControls();refresh(true);
};
$('activityThread').onchange=()=>{renderChatControls();refresh(true);};
$('showHidden').onchange=event=>{
 showHidden=event.target.checked;
 save('showHidden',showHidden);
 redraw();
};
$('prompt').onkeydown=e=>{if(e.key==='Enter'&&!e.shiftKey&&!e.isComposing&&e.keyCode!==229){e.preventDefault();$('composer').requestSubmit();}};
const PROJECT_DRAG='application/x-thread-atlas-project',CARD_DRAG='application/x-thread-atlas',TODO_DRAG='application/x-thread-atlas-todo';
let highlightedDrop=null,highlightedClass='dropping';
function highlightDrop(target,side){
 const style=side==='before'?'todoDropBefore':side==='after'?'todoDropAfter':'dropping';
 if(highlightedDrop===target&&highlightedClass===style)return;
 highlightedDrop?.classList.remove(highlightedClass);
 highlightedDrop=target;
 highlightedClass=style;
 highlightedDrop?.classList.add(style);
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
function todoDrop(event){
 if(dragging?.type!==TODO_DRAG||!event.dataTransfer.types.includes(TODO_DRAG))return null;
 const todo=todoById(dragging.id);
 if(!todo)return null;
 const row=event.target.closest('[data-todo-row]');
 if(row){
  const neighbor=todoById(row.dataset.todoRow);
  if(!neighbor||neighbor.id===todo.id||neighbor.completed!==todo.completed)return null;
  const bounds=row.getBoundingClientRect();
  return {id:todo.id,target:row,group:neighbor.group_id,neighbor:neighbor.id,side:event.clientY<bounds.top+bounds.height/2?'before':'after'};
 }
 const folder=event.target.closest('[data-todo-project],.treeProjectRow,.projectRow');
 const group=folder?.dataset.todoProject??folder?.dataset.group;
 return group&&group!==todo.group_id?{id:todo.id,target:folder,group}:null;
}
function endDrag(){
 dragging=null;
 highlightDrop(null);
}
document.addEventListener('dragstart',event=>{
 const source=event.target.closest('[data-drag-project],[data-drag-todo],.card[data-card]');
 if(!source)return;
 const {dragProject,dragTodo,card}=source.dataset;
 if(dragProject)dragging={type:PROJECT_DRAG,id:dragProject};
 else if(dragTodo)dragging={type:TODO_DRAG,id:dragTodo};
 else dragging={type:CARD_DRAG,id:card};
 event.dataTransfer.setData(dragging.type,dragging.id);
 event.dataTransfer.effectAllowed='move';
});
document.addEventListener('dragover',event=>{
 const todo=todoDrop(event),target=todo?.target||dropZone(event);
 highlightDrop(target,todo?.side);
 if(!target)return;
 event.preventDefault();
 event.dataTransfer.dropEffect='move';
});
document.addEventListener('dragleave',event=>{
 if(event.target===highlightedDrop&&!highlightedDrop.contains(event.relatedTarget))highlightDrop(null);
});
document.addEventListener('dragend',endDrag);
document.addEventListener('drop',async event=>{
 const todo=todoDrop(event),target=todo?.target||dropZone(event);
 const project=event.dataTransfer.getData(PROJECT_DRAG);
 const card=event.dataTransfer.getData(CARD_DRAG);
 endDrag();
 if(!target)return;
 event.preventDefault();
 try{
  if(todo)await moveTodo(todo.id,todo.group,todo.neighbor,todo.side);
  else if(project)await moveProject(project,target.dataset.group??null);
  else if(!item(card))return;
  else if(target.id==='hiddenDrop')await edit(card,{workspace_status:'hidden'});
  else if(target.id==='composer'){
   if(item(card).hidden)return;
   selectChatTarget(card);$('prompt').focus();
  }
  else await edit(card,{group:target.dataset.group,before:event.target.closest('.treeSession')?.dataset.session});
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
  dialog(name(thread),'<p class="sub">'+escape(thread.provider)+' · '+stamp(reply.at)+'</p><div class="history"><section><p>'+escape(reply.body)+'</p></section></div><div class="actions"><button class="choice" data-card="'+thread.id+'">'+l('View source','원문 보기')+'</button><button class="choice" data-thread-reply="'+thread.id+'">'+l('Reply here','이어서 답장')+'</button></div>');
 } catch(err){toast(err.message);}
}
$('prompt').value=load('draft','');applyLanguage();await refresh(true);
window.addEventListener('hashchange',openNotification);await openNotification();setInterval(()=>refresh(),2000);
