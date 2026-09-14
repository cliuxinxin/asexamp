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
const {ArtifactChangeDialog}=await import('../src/ArtifactChangeDialog');
const originalFetch=globalThis.fetch;
afterEach(async()=>{await act(async()=>{});cleanup();globalThis.fetch=originalFetch;});
const json=(value:unknown,status=200)=>new Response(JSON.stringify(value),{status,headers:{'Content-Type':'application/json'}});
const prompt={id:'revision:p1:2',kind:'artifact_proposal',title:'确认修改预览',message:'场景修改预览已准备好。',proposal_id:'p1',artifact_id:'scenes',artifact_revision:2};
const proposal={id:'p1',prompt_id:prompt.id,summary:'仅修改登录场景',changes:[{artifact_id:'scenes',type:'scenarios',title:'登录场景',expected_revision:2,before_items:[{id:'S1',title:'普通登录',requirement_ids:['R1']}],items:[{id:'S1',title:'并发登录',requirement_ids:[]}]}]};
function fixture(options:{stale?:boolean;needsInput?:boolean}={}){
 const writes:any[]=[];const reads:string[]=[];
 const state:any={chat:{id:'chat',project_id:'project',title:'登录测试'},sources:[],messages:[{id:'assistant',role:'assistant',content:proposal.summary,metadata:{}}],runs:[],conversation_prompt:prompt};
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

test('artifact changes open above the composer as highlighted rows and never consume its draft',async()=>{
 const request=fixture();render(<App/>);
 const button=await screen.findByRole('button',{name:'查看修改预览'});
 const input=screen.getByLabelText('聊天输入') as HTMLTextAreaElement;
 fireEvent.change(input,{target:{value:'还有一句问题'}});
 assert.ok(button.compareDocumentPosition(input)&Node.DOCUMENT_POSITION_FOLLOWING);
 assert.equal(button.closest('.composer'),null);
 assert.equal(screen.queryByLabelText('待应用修改'),null);
 assert.equal(request.writes.length,0);
 fireEvent.click(button);
 const dialog=await screen.findByRole('dialog',{name:'查看修改预览'});
 await within(dialog).findByRole('table',{name:'S1 修改对比'});
 assert.ok(dialog.querySelector('ins'));assert.ok(dialog.querySelector('del'));
 assert.ok(within(dialog).getByText('N/A'));
 fireEvent.click(within(dialog).getByRole('button',{name:'返回对话修改'}));
 assert.equal(screen.queryByRole('dialog'),null);assert.equal(input.value,'还有一句问题');
 assert.equal(request.writes.length,0);
});

test('applying a preview anchors the actual prompt and removes the suggestion',async()=>{
 const request=fixture();render(<App/>);
 fireEvent.click(await screen.findByRole('button',{name:'查看修改预览'}));
 fireEvent.click(await screen.findByRole('button',{name:'确认应用修改'}));
 await waitFor(()=>assert.equal(request.writes.length,1));
 assert.equal(request.writes[0].reply_to,prompt.id);
 assert.equal(request.writes[0].command.name,'artifact.apply');
 assert.equal(request.writes[0].command.arguments.proposal_id,'p1');
 await waitFor(()=>assert.equal(screen.queryByRole('dialog'),null));
 assert.equal(screen.queryByRole('button',{name:'查看修改预览'}),null);
});

test('discard is a scoped chat command and stale rejection keeps the preview open',async()=>{
 const request=fixture({stale:true});render(<App/>);
 fireEvent.click(await screen.findByRole('button',{name:'查看修改预览'}));
 fireEvent.click(await screen.findByRole('button',{name:'取消这项修改'}));
 await screen.findByRole('alert');
 assert.equal(request.writes[0].reply_to,prompt.id);
 assert.equal(request.writes[0].command.name,'artifact.discard');
 assert.ok(screen.getByRole('dialog',{name:'查看修改预览'}));
 fireEvent.click(screen.getByRole('button',{name:/其他会话/}));
 await waitFor(()=>assert.equal(screen.queryByRole('dialog'),null));
 assert.equal(request.writes.length,1);
});

test('review preview shows proposed case steps and confirms the pipeline only after inspection',async()=>{
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
 render(<ArtifactChangeDialog chatId="chat" prompt={gate} proposalId="review_1" onClose={()=>{}} onRevise={()=>{}} onResolved={()=>{resolved=true;}}/>);
 await screen.findByRole('table',{name:'TC1 修改对比'});
 assert.ok(screen.getByText('输入有效账号，再点击登录'));assert.ok(screen.getByText('显示用户首页'));
 assert.equal(turns.length,0);assert.equal(screen.queryByRole('button',{name:'取消这项修改'}),null);
 fireEvent.click(screen.getByRole('button',{name:'确认评审建议并修改用例'}));
 await waitFor(()=>assert.ok(resolved));
 assert.equal(turns[0].reply_to,gate.id);assert.equal(turns[0].command.name,'workflow.resume');assert.equal(turns[0].command.arguments.action,'approved');
});

test('a needs-input response keeps the preview and shows its correction message',async()=>{
 const request=fixture({needsInput:true});render(<App/>);
 fireEvent.click(await screen.findByRole('button',{name:'查看修改预览'}));
 fireEvent.click(await screen.findByRole('button',{name:'确认应用修改'}));
 await screen.findByRole('alert');
 assert.equal(request.writes.length,1);
 assert.ok(screen.getByRole('dialog',{name:'查看修改预览'}));
 assert.match(screen.getByRole('alert').textContent??'',/修改预览已改变/);
});
