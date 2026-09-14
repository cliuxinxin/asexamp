import {JSDOM} from 'jsdom';
import {afterEach,test} from 'node:test';
import assert from 'node:assert/strict';

const dom=new JSDOM('<!doctype html><html><body></body></html>',{url:'http://localhost/',pretendToBeVisual:true});
for(const key of ['window','document','HTMLElement','HTMLDialogElement','MutationObserver','Node','Event','MouseEvent','CustomEvent','File','FormData'])Object.defineProperty(globalThis,key,{value:(dom.window as any)[key],configurable:true,writable:true});
Object.defineProperty(globalThis,'navigator',{value:dom.window.navigator,configurable:true});
(globalThis as any).IS_REACT_ACT_ENVIRONMENT=true;
dom.window.HTMLElement.prototype.scrollIntoView=function(){};
dom.window.HTMLDialogElement.prototype.showModal=function(){this.open=true;};
dom.window.HTMLDialogElement.prototype.close=function(){this.open=false;};
const React=await import('react');
const {render,fireEvent,screen,waitFor,cleanup,within,act}=await import('@testing-library/react');
const {ArtifactCard}=await import('../src/ArtifactCard');
const {ArtifactWorkspaceContext}=await import('../src/workspaceContext');
const originalFetch=globalThis.fetch;
afterEach(async()=>{await act(async()=>{});cleanup();globalThis.fetch=originalFetch;});
const json=(value:unknown,status=200)=>new Response(JSON.stringify(value),{status,headers:{'Content-Type':'application/json'}});
const before={id:'TC1',title:'普通登录',type:'Business',priority:'P1',preconditions:'已注册',scenario_id:'S1',steps:[{action:'点击登录',expected:'成功'}],refs:['src#P1']};
const after={...before,title:'认证用户登录',steps:[{action:'输入有效账号，再点击登录',expected:'显示用户首页'}]};
const second={...before,id:'TC2',title:'退出登录'};
const artifact={id:'cases',chat_id:'chat',project_id:'project',revision:3,type:'cases',title:'登录测试用例',items:[before,second],report:{}};
const prompt={id:'gate:review:3',kind:'case_result_review',title:'确认评审建议',message:'请核对 AI 修改。',run_id:'run',artifact_id:'cases',artifact_revision:3,review_proposal_id:'review_3'};
const columns=[{field:'id',header:'Case ID'},{field:'title',header:'标题'},{field:'steps',header:'操作步骤'},{field:'expected',header:'预期结果'}];
const rows=(items:any[])=>items.map(item=>({item_id:item.id,step_index:null,cells:[item.id,item.title,item.steps.map((step:any,index:number)=>`${index+1}. ${step.action}`).join('\n'),item.steps.map((step:any,index:number)=>`${index+1}. ${step.expected}`).join('\n')]}));
const reviewData={artifact_id:'cases',artifact_type:'cases',mode:'ai_proposal',title:artifact.title,artifact_revision:3,layout:'case',columns,original_items:artifact.items,proposed_items:[after,second],original_rows:rows(artifact.items),proposed_rows:rows([after,second]),issues:[{case_ids:['TC1'],field:'expected',title:'补充可验证预期'}],read_only:false,run_id:'run',proposal_id:'review_3',prompt_id:prompt.id};

function appFixture(){
 const reads:string[]=[];const turns:any[]=[];const mutations:{path:string;body:any}[]=[];
 const currentArtifact=structuredClone(artifact);
 const proposal={id:'review_3',artifact_id:'cases',artifact_revision:3,expected_revision:3,status:'pending',items:[after,second],before_items:artifact.items,report:{summary:'已补充登录操作与可验证预期'},changes:[{op:'update',id:'TC1',before,after,fields:['title','steps']}]};
 const state:any={chat:{id:'chat',project_id:'project',profile_id:'profile',title:'登录测试'},sources:[],messages:[{id:'review-message',role:'assistant',content:'AI 评审已完成，请核对修改建议。',metadata:{review_proposal_id:'review_3',run_id:'run'}}],runs:[{id:'run',chat_id:'chat',status:'waiting',mode:'hitp',node:'case_result_review',updated_at:'2026-09-14T10:00:00Z'}],conversation_prompt:prompt};
 globalThis.fetch=(async(input:any,init:any={})=>{
  const url=new URL(String(input),'http://localhost');const path=url.pathname.replace(/^\/api/,'');
  if(init.method&&init.method!=='GET')mutations.push({path,body:init.body?JSON.parse(init.body):undefined});else reads.push(path+url.search);
  if(path==='/projects')return json([{id:'project',name:'项目'}]);
  if(path==='/settings')return json({model:'mock'});
  if(path==='/projects/project/profiles')return json([{id:'profile',name:'默认',version:2,config:{}}]);
  if(path==='/projects/project/chats')return json([state.chat]);
  if(path==='/chats/chat')return json(state);
  if(path==='/chats/chat/workspace-state')return json({pending_proposal:null});
  if(path==='/runs/run/review-proposals/review_3')return json(proposal);
  if(path==='/artifacts/cases')return json(currentArtifact);
  if(path.includes('/workspace-grid')){
   const body=init.body?JSON.parse(init.body):{};
   return json(body.items?{...reviewData,rows:rows(body.items)}:reviewData);
  }
  if(path==='/chats/chat/turns'){
   const body=JSON.parse(init.body);turns.push(body);
   const response={id:'turn:side',status:'succeeded',message:'该用例验证登录成功后进入用户首页。',parts:[],pending:[],actions:[]};
   state.messages.push({id:'side-user',role:'user',content:body.content,metadata:{client_message_id:body.client_message_id}},{id:'side-answer',role:'assistant',content:response.message,metadata:{turn_response:response}});
   return json(response);
  }
  if(path.includes('/workspace'))return json({lineage_rows:[]});
  return json([]);
 }) as typeof fetch;
 return {reads,turns,mutations,state,currentArtifact};
}

test('historical ArtifactCard opens full-screen using the exact visible revision and read-only context',async()=>{
 const opened:any[]=[];const requests:string[]=[];
 globalThis.fetch=(async(input:any)=>{requests.push(String(input));return json({lineage_rows:[]});}) as typeof fetch;
 render(<ArtifactWorkspaceContext.Provider value={{open:request=>opened.push(request)}}><ArtifactCard id="cases" snapshot={{...artifact,revision:2}} readOnly chatOnly simplified onTarget={()=>assert.fail('launch must not retarget chat')} onChanged={()=>assert.fail('launch must not mutate')}/></ArtifactWorkspaceContext.Provider>);
 fireEvent.click(screen.getByRole('button',{name:'打开成果工作区',exact:true}));
 assert.deepEqual(opened,[{artifactId:'cases',revision:2,readOnly:true}]);
 assert.equal(screen.queryByRole('button',{name:'编辑',exact:true}),null);
 await act(async()=>{});
 assert.equal(requests.length,0);
});

test('pending review opens full-screen from its assistant card and returning preserves the main composer draft',async()=>{
 const request=appFixture();const {App}=await import('../src/App');render(<App/>);
 const card=await screen.findByRole('region',{name:'评审建议预览'});
 const entry=await within(card).findByRole('button',{name:'打开成果工作区'});
 const input=screen.getByLabelText('聊天输入') as HTMLTextAreaElement;
 fireEvent.change(input,{target:{value:'主对话保留的问题草稿'}});
 fireEvent.click(entry);
 const reviewer=await screen.findByRole('dialog');
 await within(reviewer).findByRole('table');
 const opened=new URL(request.reads.find(path=>path.includes('/workspace-grid'))!,'http://localhost');
 assert.equal(opened.pathname,'/artifacts/cases/workspace-grid');
 assert.equal(opened.searchParams.get('run_id'),'run');
 assert.equal(opened.searchParams.get('proposal_id'),'review_3');
 assert.match(within(reviewer).getByRole('heading',{level:2}).textContent??'',/登录测试用例.*v3/);
 assert.equal(request.turns.length,0);
 assert.equal(input.value,'主对话保留的问题草稿');
 fireEvent.click(within(reviewer).getByRole('button',{name:/返回对话/}));
 await waitFor(()=>assert.equal(screen.queryByRole('dialog'),null));
 assert.equal((screen.getByLabelText('聊天输入') as HTMLTextAreaElement).value,'主对话保留的问题草稿');
 assert.equal(request.turns.length,0);
});

test('side assistant uses the existing chat and selected cases without starting a workflow or consuming the main draft',async()=>{
 const request=appFixture();const {App}=await import('../src/App');render(<App/>);
 const entry=await screen.findByRole('button',{name:'打开成果工作区'});
 const input=screen.getByLabelText('聊天输入') as HTMLTextAreaElement;
 fireEvent.change(input,{target:{value:'下一轮要补充的需求'}});fireEvent.click(entry);
 const reviewer=await screen.findByRole('dialog');
 await within(reviewer).findByRole('table');
 fireEvent.click(within(reviewer).getByRole('checkbox',{name:'选择 TC1',exact:true}));
 const sideInput=within(reviewer).getByRole('textbox') as HTMLTextAreaElement;
 fireEvent.change(sideInput,{target:{value:'解释所选用例，不要应用评审建议'}});
 fireEvent.click(within(reviewer).getByRole('button',{name:'发送成果消息'}));
 await waitFor(()=>assert.equal(request.turns.length,1));
 const sent=request.turns[0];
 assert.equal(sent.content,'解释所选用例，不要应用评审建议');
 assert.equal(sent.artifact_id,'cases');assert.equal(sent.artifact_revision,3);
 assert.deepEqual(sent.selected_ids,['TC1']);assert.deepEqual(sent.view_order,['TC1','TC2']);
 assert.equal(sent.reply_to,prompt.id);assert.equal(sent.intent_hint,'auto');
 assert.equal(sent.command,undefined);assert.equal(sent.as_requirement,false);
 assert.equal(sent.mode,'hitp');assert.equal(sent.profile_id,'profile');
 assert.equal(input.value,'下一轮要补充的需求');
 assert.ok(request.mutations.filter(item=>item.path.endsWith('/turns')).every(item=>item.path==='/chats/chat/turns'));
 await waitFor(()=>assert.equal(sideInput.value,''));
 fireEvent.click(within(reviewer).getByRole('button',{name:/返回对话/}));
 await waitFor(()=>assert.equal(screen.queryByRole('dialog'),null));
 assert.equal((screen.getByLabelText('聊天输入') as HTMLTextAreaElement).value,'下一轮要补充的需求');
 assert.equal(request.turns.length,1);
});

test('side assistant refuses a stale table revision and retains both text drafts',async()=>{
 const request=appFixture();const {App}=await import('../src/App');render(<App/>);
 const entry=await screen.findByRole('button',{name:'打开成果工作区'});
 const input=screen.getByLabelText('聊天输入') as HTMLTextAreaElement;
 fireEvent.change(input,{target:{value:'主输入仍未发送'}});fireEvent.click(entry);
 const reviewer=await screen.findByRole('dialog');
 await within(reviewer).findByRole('table');
 request.currentArtifact.revision=4;
 const sideInput=within(reviewer).getByRole('textbox') as HTMLTextAreaElement;
 fireEvent.change(sideInput,{target:{value:'把前置条件改为已认证'}});
 fireEvent.click(within(reviewer).getByRole('button',{name:'发送成果消息'}));
 await within(reviewer).findByRole('alert');
 assert.equal(request.turns.length,0);
 assert.equal(input.value,'主输入仍未发送');
 assert.equal(sideInput.value,'把前置条件改为已认证');
 assert.match(within(reviewer).getByRole('alert').textContent??'',/版本|更新|改变/);
});

test('all current business artifacts open the workspace without a historical revision guard',async()=>{
 const opened:any[]=[];
 for(const type of ['cases','scenarios','analysis']){
  const view=render(<ArtifactWorkspaceContext.Provider value={{open:request=>opened.push(request)}}><ArtifactCard id={type} snapshot={{...artifact,id:type,type}} compact onTarget={()=>{}} onChanged={()=>{}}/></ArtifactWorkspaceContext.Provider>);
  fireEvent.click(screen.getByRole('button',{name:/打开成果 ·/}));view.unmount();
 }
 assert.deepEqual(opened,[{artifactId:'cases'},{artifactId:'scenarios'},{artifactId:'analysis'}]);
});

test('unified proposal and legacy diff receipts only navigate, with historical receipts immutable',async()=>{
 const {ConversationParts}=await import('../src/ConversationParts');const opened:any[]=[];const requests:string[]=[];
 globalThis.fetch=(async(input:any)=>{requests.push(String(input));throw new Error('navigation must not write or load diff content');}) as typeof fetch;
 const active={id:'revision:scenario:3',kind:'artifact_proposal',title:'修改场景',message:'请核对修改',artifact_id:'scenarios',artifact_revision:3,proposal_id:'proposal'};
 const response:any={id:'turn',status:'needs_confirmation',message:'',pending:[],actions:[],parts:[{type:'artifact_proposal',artifact_id:'scenarios',artifact_revision:3,proposal_id:'proposal'},{type:'diff',proposal_id:'history',changes:[{artifact_id:'scenarios',expected_revision:1,items:[]}]}]};
 const view=render(<ArtifactWorkspaceContext.Provider value={{open:request=>opened.push(request)}}><ConversationParts response={response} currentPrompt={active} onTarget={()=>{}} onChanged={()=>{}}/></ArtifactWorkspaceContext.Provider>);
 fireEvent.click(screen.getByRole('button',{name:'打开成果工作区'}));
 fireEvent.click(screen.getByRole('button',{name:'查看历史工作区'}));
 assert.deepEqual(opened,[{artifactId:'scenarios',readOnly:false,promptId:active.id,proposalId:'proposal'},{artifactId:'scenarios',revision:1,readOnly:true,proposalId:'history'}]);
 assert.equal(screen.queryByRole('button',{name:/接受修改|拒绝修改|确认应用/}),null);assert.deepEqual(requests,[]);
 view.unmount();
 render(<ArtifactWorkspaceContext.Provider value={{open:request=>opened.push(request)}}><ConversationParts response={{...response,parts:[response.parts[0]]}} onTarget={()=>{}} onChanged={()=>{}}/></ArtifactWorkspaceContext.Provider>);
 fireEvent.click(screen.getByRole('button',{name:'查看历史工作区'}));
 assert.deepEqual(opened.at(-1),{artifactId:'scenarios',revision:3,readOnly:true,proposalId:'proposal'});
});
