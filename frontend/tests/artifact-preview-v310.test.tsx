import {JSDOM} from 'jsdom';
import {afterEach,test} from 'node:test';
import assert from 'node:assert/strict';
const dom=new JSDOM('<!doctype html><html><body></body></html>',{url:'http://localhost/'});
for(const key of ['window','document','HTMLElement','HTMLDialogElement','MutationObserver','Node','Event','MouseEvent','CustomEvent','File','FormData'])Object.defineProperty(globalThis,key,{value:(dom.window as any)[key],configurable:true,writable:true});
Object.defineProperty(globalThis,'navigator',{value:dom.window.navigator,configurable:true});
(globalThis as any).IS_REACT_ACT_ENVIRONMENT=true;
dom.window.HTMLElement.prototype.scrollIntoView=function(){};
dom.window.HTMLDialogElement.prototype.showModal=function(){this.open=true;};
dom.window.HTMLDialogElement.prototype.close=function(){this.open=false;};
const React=await import('react');
const {render,fireEvent,screen,waitFor,cleanup,within,act}=await import('@testing-library/react');
const {App}=await import('../src/App');
const {InlineChangeCard}=await import('../src/InlineChangeCard');
const originalFetch=globalThis.fetch;
afterEach(async()=>{await act(async()=>{});cleanup();globalThis.fetch=originalFetch;});
const json=(value:unknown,status=200)=>new Response(JSON.stringify(value),{status,headers:{'Content-Type':'application/json'}});
const prompt={id:'revision:p1:2',kind:'artifact_proposal',title:'确认修改预览',message:'场景修改预览已准备好。',proposal_id:'p1',artifact_id:'scenes',artifact_revision:2};
const proposal={id:'p1',prompt_id:prompt.id,summary:'仅修改登录场景',changes:[{artifact_id:'scenes',type:'scenarios',title:'登录场景',expected_revision:2,before_items:[{id:'S1',title:'普通登录',requirement_ids:['R1']}],items:[{id:'S1',title:'并发登录',requirement_ids:[]}]}]};
function receipt(id:string,proposalId:string,changes=proposal.changes){return {id,role:'assistant',content:'请查看本次修改。',metadata:{turn_response:{id:'turn:'+id,status:'needs_confirmation',message:'请查看本次修改。',parts:[{type:'diff',proposal_id:proposalId,changes}],pending:[],actions:[]}}};}
function fixture(options:{stale?:boolean;needsInput?:boolean;history?:boolean}={}){
 const writes:any[]=[];const reads:string[]=[];
 const state:any={chat:{id:'chat',project_id:'project',title:'登录测试'},sources:[],messages:[...(options.history?[receipt('history','old')]:[]),receipt('assistant','p1')],runs:[],conversation_prompt:prompt};
 globalThis.fetch=(async(input:any,init:any={})=>{
  const path=String(input).replace(/^\/api/,'');
  if(path==='/projects')return json([{id:'project',name:'项目'}]);
  if(path==='/settings')return json({model:'mock'});
  if(path==='/projects/project/profiles')return json([{id:'profile',name:'默认',version:1,config:{}}]);
  if(path==='/projects/project/chats')return json([state.chat,{id:'other',project_id:'project',title:'其他会话'}]);
  if(path==='/chats/chat')return json(state);
  if(path==='/chats/other')return json({chat:{id:'other',project_id:'project',title:'其他会话'},sources:[],messages:[],runs:[]});
  if(path==='/chats/chat/workspace-state'){reads.push(path);return json({pending_proposal:state.conversation_prompt?proposal:null});}
  if(path==='/chats/chat/turns'){
   const body=JSON.parse(init.body);writes.push(body);
   if(options.stale||options.needsInput)return json({id:'turn',status:options.needsInput?'needs_input':'failed',message:'修改预览已改变，请查看当前预览。',parts:[],pending:[],actions:[]});
   state.conversation_prompt=null;return json({id:'turn',status:'succeeded',message:'已保存本次修改。',parts:[],pending:[],actions:[]});
  }
  return json([]);
 }) as typeof fetch;
 return {writes,reads,state};
}

test('artifact changes render inside their assistant message without a modal and preserve the composer draft',async()=>{
 const request=fixture();render(<App/>);
 const card=await screen.findByRole('region',{name:'建议修改预览'});
 await within(card).findByRole('table',{name:'S1 修改对比'});
 const input=screen.getByLabelText('聊天输入') as HTMLTextAreaElement;
 fireEvent.change(input,{target:{value:'还有一句问题'}});
 assert.ok(card.closest('article.message.assistant'));
 assert.ok(card.closest('.message-body'));
 assert.ok(card.compareDocumentPosition(input)&Node.DOCUMENT_POSITION_FOLLOWING);
 assert.equal(card.closest('.composer-wrap'),null);
 assert.equal(screen.queryByRole('button',{name:'查看修改预览'}),null);
 assert.equal(screen.queryByRole('dialog'),null);
 assert.ok(card.querySelector('ins'));assert.ok(card.querySelector('del'));
 assert.ok(within(card).getByText('N/A'));
 fireEvent.click(within(card).getByRole('button',{name:'补充修改意见'}));
 assert.equal(document.activeElement,input);assert.equal(input.value,'还有一句问题');
 assert.equal(request.writes.length,0);
});

test('accepting an inline preview anchors the actual prompt and leaves a read-only historical receipt',async()=>{
 const request=fixture();render(<App/>);
 await screen.findByRole('table',{name:'S1 修改对比'});
 const input=screen.getByLabelText('聊天输入') as HTMLTextAreaElement;
 fireEvent.change(input,{target:{value:'下一步的问题草稿'}});
 fireEvent.click(screen.getByRole('button',{name:'接受修改'}));
 await waitFor(()=>assert.equal(request.writes.length,1));
 assert.equal(request.writes[0].reply_to,prompt.id);
 assert.equal(request.writes[0].reply_kind,'confirm');
 assert.equal(request.writes[0].command.name,'artifact.apply');
 assert.equal(request.writes[0].command.arguments.proposal_id,'p1');
 assert.equal(request.writes[0].command.arguments.expected_revision,2);
 await waitFor(()=>assert.equal(screen.queryByRole('button',{name:'接受修改'}),null));
 assert.equal(screen.queryByRole('dialog'),null);
 assert.ok(screen.getByText('修改预览 · 查看当时的差异'));
 assert.equal(input.value,'下一步的问题草稿');
});

test('reject is a scoped chat command and a stale response preserves its inline diff until navigation',async()=>{
 const request=fixture({stale:true});render(<App/>);
 await screen.findByRole('table',{name:'S1 修改对比'});
 fireEvent.click(screen.getByRole('button',{name:'拒绝修改'}));
 await screen.findByRole('alert');
 assert.equal(request.writes[0].reply_to,prompt.id);
 assert.equal(request.writes[0].reply_kind,'confirm');
 assert.equal(request.writes[0].command.name,'artifact.discard');
 assert.ok(screen.getByRole('region',{name:'建议修改预览'}));
 assert.ok(screen.getByRole('table',{name:'S1 修改对比'}));
 assert.ok(screen.getByRole('button',{name:'刷新当前提示'}));
 fireEvent.click(screen.getByRole('button',{name:/其他会话/}));
 await waitFor(()=>assert.equal(screen.queryByRole('region',{name:'建议修改预览'}),null));
 assert.equal(request.writes.length,1);
});

test('inline review shows proposed case steps and confirms the pipeline only after explicit acceptance',async()=>{
 const turns:any[]=[];let resolved=false;
 const before={id:'TC1',title:'登录',steps:[{action:'点击登录',expected:'成功'}]};
 const after={...before,steps:[{action:'输入有效账号，再点击登录',expected:'显示用户首页'}]};
 const gate={id:'gate:review:1',kind:'case_result_review',title:'评审建议',message:'请核对建议',run_id:'run',artifact_id:'cases',artifact_revision:1,review_proposal_id:'review_1'};
 globalThis.fetch=(async(input:any,init:any={})=>{
  const path=String(input).replace(/^\/api/,'');
  if(path==='/runs/run/review-proposals/review_1')return json({id:'review_1',artifact_id:'cases',expected_revision:1,report:{summary:'补充操作与预期'},changes:[{op:'update',id:'TC1',before,after,fields:['steps']}]});
  if(path==='/chats/chat/turns'){turns.push(JSON.parse(init.body));return json({id:'turn',status:'succeeded',message:'开始应用评审修改',parts:[],pending:[],actions:[]});}
  throw new Error('Unexpected '+path);
 }) as typeof fetch;
 render(<InlineChangeCard chatId="chat" prompt={gate} proposalId="review_1" onChanged={()=>{}} onTurnResolved={()=>{resolved=true;}}/>);
 await screen.findByRole('table',{name:'TC1 修改对比'});
 assert.ok(screen.getByText('输入有效账号，再点击登录'));assert.ok(screen.getByText('显示用户首页'));
 assert.equal(screen.queryByRole('dialog'),null);assert.equal(turns.length,0);
 fireEvent.click(screen.getByRole('button',{name:'接受评审建议'}));
 await waitFor(()=>assert.ok(resolved));
 assert.equal(turns[0].reply_to,gate.id);assert.equal(turns[0].reply_kind,'confirm');
 assert.equal(turns[0].command.name,'workflow.resume');assert.equal(turns[0].command.arguments.action,'approved');
});

test('a needs-input response keeps the inline diff and composer draft while showing its correction message',async()=>{
 const request=fixture({needsInput:true});render(<App/>);
 await screen.findByRole('table',{name:'S1 修改对比'});
 const input=screen.getByLabelText('聊天输入') as HTMLTextAreaElement;
 fireEvent.change(input,{target:{value:'请稍后解释关联规则'}});
 fireEvent.click(screen.getByRole('button',{name:'接受修改'}));
 await screen.findByRole('alert');
 assert.equal(request.writes.length,1);
 assert.ok(screen.getByRole('region',{name:'建议修改预览'}));
 assert.ok(screen.getByRole('table',{name:'S1 修改对比'}));
 assert.match(screen.getByRole('alert').textContent??'',/修改预览已改变/);
 assert.equal(input.value,'请稍后解释关联规则');
});

test('historical diffs can be inspected but only the currently pending message has accept or reject actions',async()=>{
 const request=fixture({history:true});render(<App/>);
 await screen.findByRole('table',{name:'S1 修改对比'});
 const historical=screen.getByText('修改预览 · 查看当时的差异').closest('details')!;
 fireEvent.click(within(historical).getByText('修改预览 · 查看当时的差异'));
 // jsdom does not always dispatch toggle when details.open changes through a click.
 historical.open=true;fireEvent(historical,new Event('toggle'));
 await within(historical).findByRole('table',{name:'S1 修改对比'});
 assert.equal(within(historical).queryByRole('button',{name:'接受修改'}),null);
 assert.equal(within(historical).queryByRole('button',{name:'拒绝修改'}),null);
 assert.equal(screen.getAllByRole('button',{name:'接受修改'}).length,1);
 assert.equal(screen.getAllByRole('button',{name:'拒绝修改'}).length,1);
 assert.equal(request.writes.length,0);
});
