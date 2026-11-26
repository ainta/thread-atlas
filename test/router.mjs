import assert from 'node:assert/strict';
import {route} from '../public/router.js';
const terms=[
 ['token refresh','토큰 갱신'],['refund window','환불 기한'],['checkout timeout','결제 타임아웃'],['staging deployment','스테이징 배포'],['invoice numbering','송장 번호'],
 ['calendar reminders','달력 알림'],['image compression','이미지 압축'],['audit retention','감사 보존'],['password reset','비밀번호 초기화'],['shipment tracking','배송 추적'],
 ['search ranking','검색 순위'],['tax rounding','세금 반올림'],['offline cache','오프라인 캐시'],['badge contrast','배지 대비'],['cart expiry','장바구니 만료'],
 ['email unsubscribe','메일 수신거부'],['receipt layout','영수증 배치'],['retry budget','재시도 한도'],['queue latency','대기열 지연'],['locale fallback','언어 대체']
];
for(const lang of [0,1]){
 const items=terms.map((pair,id)=>({id:String(id),title:(lang?'검토 ':'Review ')+pair[lang],latest:lang?pair[lang]+' 변경이 준비되었습니다.':'The next change concerns '+pair[lang]+'.',project:'Platform',state:'idle'}));
 let correct=0,wrong=0,asked=0;
 for(let n=0;n<50;n++){
  const id=(n*7)%20,term=terms[id][lang];
  const template=lang?['다음 변경에서 @도 적용해줘','@ 수정부터 진행해','아까 말한 @을 다시 확인해줘']:['Please update @ for the next release.','Recheck @ before merging.','Proceed with the changes to @.'];
  const p=route(template[n%3].replace('@',term),items,null);
  if(p.candidates.length!==1)asked++;
  else if(p.action&&p.candidates[0].id===String(id))correct++;
  else wrong++;
 }
 assert.ok(correct>=40);assert.equal(wrong,0);
 for(const text of lang?['진행해','계속해','다시 해줘','이어서 해줘','바꿔줘','확인해줘','그대로 해줘','수정해줘','보내줘','한번 더']:['Go ahead','Do it','Continue','Proceed','Make the change','Fix it','Send that','Try again','One more time','Keep going'])assert.ok(route(text,items,null).candidates.length>1);
 console.log((lang?'Korean':'English')+' synthetic routing: '+correct+'/50 correct, '+wrong+' wrong, '+asked+' prompted; 10/10 ambiguous prompted');
}
const duplicates=[0,1].map(id=>({id,title:'Refund policy',latest:'Update the limit',project:'Work'}));
assert.equal(route('Update refund policy',duplicates,null).candidates.length,2);
assert.equal(route('Where did we leave the refund policy?',duplicates,null).action,false);
assert.equal(route('Go ahead',duplicates,0,'send',true).candidates[0].id,0);
