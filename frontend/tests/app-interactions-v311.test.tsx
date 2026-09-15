import {JSDOM} from 'jsdom';
import {afterEach,test} from 'node:test';
import assert from 'node:assert/strict';
const dom=new JSDOM('<!doctype html><html><body></body></html>',{url:'http://localhost/'});
for(const key of ['window','document','HTMLElement','HTMLDialogElement','MutationObserver','Node','Event','MouseEvent','CustomEvent','File','FormData'])Object.defineProperty(globalThis,key,{value:(dom.window as any)[key],configurable:true,writable:true});
Object.defineProperty(globalThis,'navigator',{value:dom.window.navigator,configurable:true});
(globalThis as any).IS_REACT_ACT_ENVIRONMENT=true;
(globalThis as any).EventSource=class{addEventListener(){}close(){}};
dom.window.HTMLElement.prototype.scrollIntoView=function(){};
dom.window.HTMLDialogElement.prototype.showModal=function(){this.open=true;};
dom.window.HTMLDialogElement.prototype.close=function(){this.open=false;};
const React=await import('react');
const {render,fireEvent,screen,waitFor,cleanup,within,act}=await import('@testing-library/react');
const {App}=await import('../src/App');
const originalFetch=globalThis.fetch;
afterEach(async()=>{await act(async()=>{});cleanup();globalThis.fetch=originalFetch;});
const json=(value:unknown)=>new Response(JSON.stringify(value),{status:200,headers:{'Content-Type':'application/json'}});
const yesAnswer='发生登录错误后保留用户已输入的账号。';
const question={id:'Q1',question:'错误后保留输入吗？',suggestion:'建议保留输入。',options:[{id:'keep',label:'是，保留',answer:yesAnswer},{id:'clear',label:'否，清空',answer:'发生登录错误后清空用户已输入的账号。'}]};
const clarification={id:'clarify:run:2',kind:'clarification',title:'请补充登录规则',message:'选择仅回答当前问题；不会确认需求理解。',run_id:'run',questions:[question,{id:'Q2',question:'登录失败时显示什么提示？',suggestion:'显示账号或密码错误。'}]};
function fixture(options:{failed?:boolean}={}){
 const turns:any[]=[];const reads:string[]=[];
 const state:any={chat:{id:'chat',project_id:'project',title:'登录测试'},sources:[],messages:[{id:'assistant',role:'assistant',content:'需求已理解，请补充下面的规则。',metadata:{}}],runs:[{id:'run',chat_id:'chat',intent:'generate_case',mode:'hitp',stage:'clarification',status:'waiting',artifact_ids:[],updated_at:'2026-09-14T00:00:00Z'}],conversation_prompt:structuredClone(clarification)};
 globalThis.fetch=(async(input:any,init:any={})=>{
  const path=String(input).replace(/^\/api/,'');reads.push(path);
  if(path==='/projects')return json([{id:'project',name:'登录项目'}]);
  if(path==='/settings')return json({model:'mock'});
  if(path==='/projects/project/profiles')return json([{id:'profile',name:'Default',version:1,config:{}}]);
  if(path==='/projects/project/chats')return json([state.chat]);
  if(path==='/chats/chat')return json(state);
  if(path==='/chats/chat/turns'){
   const body=JSON.parse(init.body);turns.push(body);
   if(options.failed)return json({id:'turn',status:'failed',message:'本题答案未保存，请重试。',parts:[],pending:[],actions:[]});
   state.conversation_prompt.questions[0].answer=yesAnswer;
   return json({id:'turn',status:'needs_confirmation',message:'已保存本题答案，仍需回答其他问题。',parts:[],pending:[],actions:[]});
  }
  if(path.endsWith('/workspace-state'))return json({});
  return json([]);
 }) as typeof fetch;
 return {turns,reads,state};
}

test('the option next to a clarification submits its full answer to that prompt and preserves an unrelated draft',async()=>{
 const request=fixture();render(<App/>);
 const group=await screen.findByRole('group',{name:'Q1 错误后保留输入吗？'});
 const input=screen.getByLabelText('聊天输入') as HTMLTextAreaElement;
 fireEvent.change(input,{target:{value:'我还有一个关于权限的问题'}});
 const yes=within(group).getByRole('button',{name:'是，保留'});
 assert.equal(yes.closest('.composer-wrap'),null);
 assert.ok(group.closest('[aria-label="当前对话提示"]'));
 assert.equal(screen.getAllByRole('button',{name:'是，保留'}).length,1);
 fireEvent.click(yes);
 await waitFor(()=>assert.equal(request.turns.length,1));
 assert.equal(request.turns[0].reply_to,clarification.id);
 assert.equal(request.turns[0].reply_kind,'clarification');
 assert.equal(request.turns[0].content,`仅提交以下澄清答案并更新需求理解，不确认需求理解。\nQ1（错误后保留输入吗？）：${yesAnswer}`);
 assert.deepEqual(request.turns[0].command,{name:'clarification.answer',arguments:{answers:{Q1:yesAnswer}}});
 assert.equal(request.turns[0].selected_ids,undefined);
 assert.equal(request.turns[0].mode,'hitp');
 await waitFor(()=>assert.equal(within(group).queryByRole('button',{name:'是，保留'}),null));
 assert.ok(within(group).getByText('已确认：'+yesAnswer));
 assert.ok(screen.getByRole('button',{name:'采用 Q2 建议：显示账号或密码错误。'}));
 assert.equal(input.value,'我还有一个关于权限的问题');
});

test('failed local clarification submission leaves both choices available and keeps the composer intact',async()=>{
 const request=fixture({failed:true});render(<App/>);
 const group=await screen.findByRole('group',{name:'Q1 错误后保留输入吗？'});
 const input=screen.getByLabelText('聊天输入') as HTMLTextAreaElement;
 fireEvent.change(input,{target:{value:'这段草稿不能被快捷回复清掉'}});
 fireEvent.click(within(group).getByRole('button',{name:'否，清空'}));
 await waitFor(()=>assert.equal(request.turns.length,1));
 assert.equal(request.turns[0].reply_to,clarification.id);
 assert.equal(request.turns[0].reply_kind,'clarification');
 assert.match(request.turns[0].content,/发生登录错误后清空用户已输入的账号。$/);
 assert.deepEqual(request.turns[0].command,{name:'clarification.answer',arguments:{answers:{Q1:'发生登录错误后清空用户已输入的账号。'}}});
 await screen.findByText('本题答案未保存，请重试。');
 await waitFor(()=>assert.equal((within(group).getByRole('button',{name:'否，清空'}) as HTMLButtonElement).disabled,false));
 assert.ok(within(group).getByRole('button',{name:'是，保留'}));
 assert.equal(within(group).queryByText(/^已提交：/),null);
 assert.equal(input.value,'这段草稿不能被快捷回复清掉');
});

test('the project trace entry navigates to the exact artifact row without generating or changing any data',async()=>{
 const request=fixture();const baseFetch=globalThis.fetch;
 const otherChat={id:'other',project_id:'project',title:'订单测试'};
 const selectedRow={id:'S-order',title:'创建订单',type:'Business',priority:'P1',description:'已登录用户创建订单',requirement_ids:['R-order'],refs:[]};
 const artifact={id:'order-scenes',chat_id:'other',type:'scenarios',title:'订单场景',revision:3,items:[selectedRow,{...selectedRow,id:'S-hidden',title:'其他订单场景'}],report:{}};
 const columns=[{field:'id',header:'场景编号',editable:false},{field:'title',header:'标题'},{field:'description',header:'说明'},{field:'requirement_ids',header:'对应需求'}];
 const row={item_id:selectedRow.id,step_index:null,cells:columns.map(column=>typeof selectedRow[column.field as keyof typeof selectedRow]==='object'?JSON.stringify(selectedRow[column.field as keyof typeof selectedRow]):String(selectedRow[column.field as keyof typeof selectedRow]??''))};
 const workspace={artifact_id:artifact.id,artifact_type:artifact.type,title:artifact.title,artifact_revision:artifact.revision,mode:'manual',layout:'case',columns,original_items:[selectedRow],proposed_items:[selectedRow],original_rows:[row],proposed_rows:[row],issues:[],read_only:false};
 const node={key:'other/order-scenes/S-order',kind:'scenarios',item_id:'S-order',title:'创建订单',artifact_id:'order-scenes',artifact_title:'订单场景',revision:3,chat_id:'other',parent_keys:[],statuses:['missing_parent'],missing:true,stale:false,independent:false,direct:false,basis:[]};
 const paths:{path:string;method:string}[]=[];
 globalThis.fetch=(async(input:any,init:any={})=>{
  const path=String(input).replace(/^\/api/,'');paths.push({path,method:init.method??'GET'});
  if(path==='/projects/project/chats')return json([request.state.chat,otherChat]);
  if(path==='/projects/project/traceability?chat_id=chat')return json({project_id:'project',chat_id:'chat',summary:{requirements:0,scenarios:0,cases:0,missing:0,stale:0,independent:0,direct_cases:0},chats:[],notes:[]});
  if(path==='/projects/project/traceability')return json({project_id:'project',chat_id:null,summary:{requirements:0,scenarios:1,cases:0,missing:1,stale:0,independent:0,direct_cases:0},chats:[{...otherChat,nodes:[node]}],notes:[]});
  if(path==='/chats/other')return json({chat:otherChat,messages:[],sources:[],runs:[]});
  if(path==='/chats/other/workspace-state')return json({});
  if(path==='/artifacts/order-scenes')return json(artifact);
  if(path.startsWith('/artifacts/order-scenes/workspace-grid'))return json(workspace);
  return baseFetch(input,init);
 }) as typeof fetch;
 render(<App/>);
 await screen.findByRole('group',{name:'Q1 错误后保留输入吗？'});
 fireEvent.change(screen.getByLabelText('聊天输入'),{target:{value:'登录会话的未提交草稿'}});
 fireEvent.click(screen.getByRole('button',{name:'追溯矩阵',exact:true}));
 const matrix=await screen.findByRole('dialog',{name:'追溯矩阵'});
 fireEvent.change(within(matrix).getByLabelText('追溯矩阵范围'),{target:{value:'project'}});
 fireEvent.click(await within(matrix).findByRole('button',{name:'S-order 创建订单'}));
 const viewer=await screen.findByRole('dialog',{name:'成果工作区'});
 const table=await within(viewer).findByRole('table',{name:'测试场景工作表'});
 assert.ok(within(table).getByText('创建订单'));
 assert.equal(within(table).queryByText('其他订单场景'),null);
 assert.equal(screen.queryByRole('dialog',{name:'追溯矩阵'}),null);
 fireEvent.click(within(viewer).getByRole('button',{name:'返回对话'}));
 fireEvent.click(within(screen.getByRole('navigation',{name:'对话历史'})).getByRole('button',{name:'登录测试'}));
 await screen.findByRole('group',{name:'Q1 错误后保留输入吗？'});
 assert.equal((screen.getByLabelText('聊天输入') as HTMLTextAreaElement).value,'登录会话的未提交草稿');
 assert.ok(paths.some(({path})=>path==='/projects/project/traceability'));
 assert.ok(paths.some(({path})=>path==='/artifacts/order-scenes'));
 assert.ok(paths.some(({path})=>path.startsWith('/artifacts/order-scenes/workspace-grid')));
 assert.equal(paths.some(({method})=>method!=='GET'),false);
 assert.equal(request.turns.length,0);
});
