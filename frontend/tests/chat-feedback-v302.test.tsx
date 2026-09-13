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
const {ConversationPrompt}=await import('../src/ConversationPrompt');
const {ConversationSuggestions}=await import('../src/ConversationSuggestions');
const {withLocalTurnReceipts}=await import('../src/chatFeedback');
const originalFetch=globalThis.fetch;
afterEach(async()=>{await act(async()=>{});cleanup();globalThis.fetch=originalFetch;});
const json=(v:unknown,status=200)=>new Response(JSON.stringify(v),{status,headers:{'Content-Type':'application/json'}});
// native_views.current_prompt projects these pending entries from pipeline._questions.
const clarification={id:'gate:clarification:1',kind:'clarification',run_id:'run',artifact_id:'analysis',artifact_revision:1,title:'请确认需求中的待确认规则',message:'请确认这些问题与建议假设。',questions:[{id:'Q1',question:'连续失败后多久解锁？',suggestion:'锁定 15 分钟后解锁',basis:'未确认假设',confidence:'assumption',refs:[]},{id:'Q2',question:'错误提示应显示什么？',suggestion:'请稍后重试',basis:'未确认假设',confidence:'assumption',refs:[]}]};
function fixture(options:{prompt?:any;onTurn?:(body:any,state:any)=>Promise<Response>|Response}={}){
 const calls:any[]=[];const state:any={chat:{id:'chat',project_id:'project',title:'登录测试'},sources:[],messages:[],runs:[],conversation_prompt:options.prompt??null};
 const other:any={chat:{id:'other',project_id:'project',title:'另一个会话'},sources:[],messages:[],runs:[],conversation_prompt:null};
 globalThis.fetch=(async(input:any,init:any={})=>{
  const path=String(input).replace(/^\/api/,''),body=init.body?JSON.parse(init.body):undefined;calls.push({path,body});
  if(path==='/projects')return json([{id:'project',name:'登录项目'}]);
  if(path==='/settings')return json({model:'mock'});
  if(path==='/projects/project/profiles')return json([{id:'profile',name:'Default',version:1,config:{}}]);
  if(path==='/projects/project/chats')return json([state.chat,other.chat]);
  if(path==='/chats/chat')return json(state);
  if(path==='/chats/other')return json(other);
  if(path==='/chats/chat/turns')return options.onTurn?.(body,state)??json({id:'turn',client_message_id:body.client_message_id,status:'succeeded',message:'已记录',parts:[],pending:[],actions:[]});
  if(path.endsWith('/workspace-state'))return json({});
  return json([]);
 }) as typeof fetch;return{calls,state,other};
}
async function ready(){render(<App/>);await waitFor(()=>assert.equal((screen.getByRole('button',{name:'上传需求文档'}) as HTMLButtonElement).disabled,false));}
function submit(text:string){fireEvent.change(screen.getByLabelText('聊天输入'),{target:{value:text}});fireEvent.click(screen.getByRole('button',{name:'发送消息'}));}
function persist(state:any,body:any,response:any){state.messages.push({id:'input:'+response.id,role:'user',content:body.content,created_at:'2026-09-13T00:00:00Z',metadata:{turn_id:response.id,client_message_id:body.client_message_id}},{id:'reply:'+response.id,role:'assistant',content:response.message,created_at:'2026-09-13T00:00:01Z',metadata:{turn_response:response}});}

test('a failed model response refreshes the saved assistant receipt after its user and keeps the draft',async()=>{
 const failure='本次操作未完成：模型请求失败；请检查服务连接后重试。';
 const {calls}=fixture({onTurn:(body,state)=>{const response={id:'failed-turn',client_message_id:body.client_message_id,status:'failed',message:failure,parts:[{type:'diagnostic',reference_id:'call-302',category:'connection',message:'模型服务连接失败',hints:['检查模型服务地址'],log_path:'logs/tcg.log'}],pending:[],actions:[]};persist(state,body,response);return json(response);}});
 await ready();submit('生成登录测试用例');
 await waitFor(()=>assert.ok(document.querySelector('.message.assistant')?.textContent?.includes(failure)));
 assert.equal(screen.getAllByText(failure).length,1);assert.equal(screen.getByText(failure).closest('.composer-wrap'),null);
 assert.deepEqual(Array.from(document.querySelectorAll('.message')).map(node=>node.className),['message user','message assistant']);
 assert.equal((screen.getByLabelText('聊天输入') as HTMLTextAreaElement).value,'生成登录测试用例');
 assert.ok(screen.getByText('call-302').closest('.message.assistant'));assert.ok(screen.getByText('logs/tcg.log'));
 assert.equal(calls.filter(call=>call.path.endsWith('/turns')).length,1);await waitFor(()=>assert.equal((screen.getByLabelText('运行模式') as HTMLSelectElement).disabled,false));
});

test('a lost HTTP response stays in its original conversation and later saved messages replace its local receipt',async()=>{
 let reject!:(reason:Error)=>void;let request:any;const responseWait=new Promise<Response>((_resolve,fail)=>{reject=fail;});
 const {state,calls}=fixture({onTurn:body=>{request=body;return responseWait;}});await ready();submit('保留这份原始要求');
 await waitFor(()=>assert.ok(request));fireEvent.click(screen.getByRole('button',{name:'另一个会话'}));
 await act(async()=>reject(new Error('连接中断')));
 assert.equal(screen.queryByText('连接中断'),null);
 fireEvent.click(screen.getByRole('button',{name:'登录测试'}));
 await waitFor(()=>assert.ok(screen.getByText('连接中断').closest('.message.assistant')));
 assert.ok(within(screen.getByRole('region',{name:'测试设计对话'})).getByText('保留这份原始要求').closest('.message.user'));assert.match(screen.getByText(/尚未确认服务器/).textContent??'',/手动重试/);
 assert.equal((screen.getByLabelText('聊天输入') as HTMLTextAreaElement).value,'保留这份原始要求');
 assert.equal(calls.filter(call=>call.path.endsWith('/turns')).length,1);
 const response={id:'late-turn',client_message_id:request.client_message_id,status:'failed',message:'服务器已记录失败',parts:[],pending:[],actions:[]};persist(state,request,response);
 fireEvent.click(screen.getByRole('button',{name:'另一个会话'}));await waitFor(()=>assert.equal(document.querySelectorAll('.message').length,0));fireEvent.click(screen.getByRole('button',{name:'登录测试'}));
 await screen.findByText('服务器已记录失败');assert.equal(screen.queryByText('连接中断'),null);assert.equal(document.querySelectorAll('.message.user').length,1);assert.equal(document.querySelectorAll('.message.assistant').length,1);
});

test('clarification replies are outside the composer, send one answer and preserve an existing draft',async()=>{
 const {calls}=fixture({prompt:clarification,onTurn:(body,state)=>{
  state.conversation_prompt={...clarification,id:'gate:clarification:2',questions:[clarification.questions[1]]};
  return json({id:'turn',status:'succeeded',message:'已记录第一题答案',parts:[],pending:[],actions:[]});
 }});await ready();
 const input=screen.getByLabelText('聊天输入') as HTMLTextAreaElement;
 fireEvent.change(input,{target:{value:'保留我的额外要求。'}});
 const choices=await screen.findByRole('group',{name:'快捷回复'});
 assert.equal(input.closest('.composer')?.contains(choices),false);
 assert.ok(choices.compareDocumentPosition(input)&dom.window.Node.DOCUMENT_POSITION_FOLLOWING);
 assert.ok(within(choices).getByRole('group',{name:'澄清答复'}));
 assert.equal(within(choices).queryByRole('group',{name:'流程确认'}),null);
 fireEvent.click(within(choices).getByRole('button',{name:'采用 Q1 建议：锁定 15 分钟后解锁'}));
 await waitFor(()=>assert.equal(calls.filter(call=>call.path.endsWith('/turns')).length,1));
 const body=calls.find(call=>call.path.endsWith('/turns')).body;
 assert.equal(body.reply_to,clarification.id);assert.equal(body.command,undefined);assert.equal(body.reply_kind,'clarification');
 assert.match(body.content,/Q1（连续失败后多久解锁？）：锁定 15 分钟后解锁/);
 assert.match(body.content,/仅提交以下澄清答案/);assert.doesNotMatch(body.content,/Q2/);
 await waitFor(()=>assert.equal(screen.queryByRole('button',{name:/采用 Q1 建议/}),null));
 assert.ok(screen.getByRole('button',{name:/采用 Q2 建议/}));assert.equal(input.value,'保留我的额外要求。');
});

test('adopting all clarification suggestions submits answers without approving the resulting requirements',async()=>{
 const {calls}=fixture({prompt:clarification,onTurn:(body,state)=>{
  state.conversation_prompt={id:'gate:strategy:2',kind:'strategy_review',title:'确认需求理解',message:'请确认当前需求理解'};
  return json({id:'turn',status:'succeeded',message:'已更新理解，请确认',parts:[],pending:[],actions:[]});
 }});await ready();
 fireEvent.click(await screen.findByRole('button',{name:'采用全部建议并更新理解'}));
 await waitFor(()=>assert.equal(calls.filter(call=>call.path.endsWith('/turns')).length,1));
 const body=calls.find(call=>call.path.endsWith('/turns')).body;
 assert.match(body.content,/Q1（/);assert.match(body.content,/Q2（/);assert.match(body.content,/不确认需求理解/);
 await screen.findByRole('button',{name:'确认需求理解并继续'});
 assert.equal(screen.queryByRole('group',{name:'澄清答复'}),null);
 assert.ok(screen.getByRole('button',{name:'先解释需求理解'}));
 assert.equal(calls.filter(call=>call.path.endsWith('/turns')).length,1);
});

test('prompt changes retire previous suggestions and busy prompts cannot fill a gate reply',async()=>{
 const {state}=fixture({prompt:clarification,onTurn:(body,state)=>{state.conversation_prompt={...clarification,id:'gate:next',kind:'busy',busy:true,title:'正在处理当前步骤'};return json({id:'turn',client_message_id:body.client_message_id,status:'succeeded',message:'继续处理',parts:[],pending:[],actions:[]});}});
 state.messages=[{id:'old',role:'assistant',content:'旧的提示',metadata:{turn_response:{id:'old-turn',status:'succeeded',parts:[],pending:[{questions:[{id:'old',question:'历史问题',suggestion:'旧建议'}]}],actions:[]}}}];
 await ready();await screen.findByRole('group',{name:'快捷回复'});assert.equal(screen.queryByRole('button',{name:/采用.*旧建议/}),null);
 submit('请先解释当前问题');await within(screen.getByRole('region',{name:'当前对话提示'})).findByText('正在处理当前步骤');assert.equal(Boolean(screen.queryByRole('group',{name:'快捷回复'})),false);
});

test('confirmed answers have no active recommendation text',()=>{
 render(<ConversationPrompt prompt={{...clarification,questions:[{...clarification.questions[0],answer:'由管理员解锁'}]}}/>);
 assert.ok(screen.getByText('已确认：由管理员解锁'));assert.equal(screen.queryByText(/^建议假设：/),null);
});


test('a native review gate directly sends assent once, bound to the displayed gate, without overwriting draft',async()=>{
 let release!:(response:Response)=>void;const waiting=new Promise<Response>(resolve=>{release=resolve;});
 const prompt={id:'interrupt:scenario-3',kind:'scenario_review',run_id:'run',artifact_id:'scenes',artifact_revision:3,title:'确认当前测试场景',message:'回复同意继续，或直接说明修改意见。'};
 const {calls}=fixture({prompt,onTurn:()=>waiting});await ready();
 const input=screen.getByLabelText('聊天输入') as HTMLTextAreaElement;
 fireEvent.change(input,{target:{value:'尚未发送的修改意见'}});
 const suggestion=await screen.findByRole('button',{name:'确认场景并继续'});
 fireEvent.click(suggestion);fireEvent.click(suggestion);
 try{
  await waitFor(()=>assert.equal(calls.filter(call=>call.path.endsWith('/turns')).length,1));
  const body=calls.find(call=>call.path.endsWith('/turns')).body;
  assert.equal(body.reply_to,prompt.id);assert.equal(body.command,undefined);assert.equal(body.reply_kind,'confirm');assert.equal(body.content,'确认当前测试场景并继续流程。');
  assert.equal(input.value,'尚未发送的修改意见');assert.equal((suggestion as HTMLButtonElement).disabled,true);
 }finally{await act(async()=>release(json({id:'gate-turn',status:'succeeded',message:'已记录',parts:[],pending:[],actions:[]})));}
 assert.equal(input.value,'尚未发送的修改意见');
});

test('explanation quick replies remain distinct from approval and retain the current gate',async()=>{
 const prompt={id:'gate:strategy',kind:'strategy_review',title:'确认需求理解',message:'请确认'};
 const {calls}=fixture({prompt});await ready();
 fireEvent.click(await screen.findByRole('button',{name:'先解释需求理解'}));
 await waitFor(()=>assert.equal(calls.filter(call=>call.path.endsWith('/turns')).length,1));
 const body=calls.find(call=>call.path.endsWith('/turns')).body;
 assert.equal(body.reply_kind,'question');assert.equal(body.content,'解释当前需求理解，暂不确认也不继续流程。');assert.equal(body.reply_to,prompt.id);assert.equal(body.command,undefined);
 await waitFor(()=>assert.equal((screen.getByRole('button',{name:'确认需求理解并继续'}) as HTMLButtonElement).disabled,false));
});

test('failed quick replies keep the draft and allow a deliberate retry',async()=>{
 const {calls}=fixture({prompt:clarification,onTurn:()=>json({id:'failed',status:'failed',message:'模型服务连接失败',parts:[],pending:[],actions:[]})});await ready();
 const input=screen.getByLabelText('聊天输入') as HTMLTextAreaElement;fireEvent.change(input,{target:{value:'不要丢失我的草稿'}});
 fireEvent.click(await screen.findByRole('button',{name:/采用 Q1 建议/}));
 await screen.findByText('模型服务连接失败');assert.equal(input.value,'不要丢失我的草稿');
 await waitFor(()=>assert.equal((screen.getByRole('button',{name:/采用 Q1 建议/}) as HTMLButtonElement).disabled,false));
 assert.equal(calls.filter(call=>call.path.endsWith('/turns')).length,1);
});

test('a local failure follows its already saved user even when the client clock is earlier',()=>{
 const input:any={id:'input:turn',role:'user',content:'原始要求',created_at:'2026-09-13T10:00:00Z',metadata:{turn_id:'turn',client_message_id:'client'}};
 const messages=withLocalTurnReceipts([input],[{clientMessageId:'client',content:'原始要求',createdAt:'2026-09-13T09:00:00Z',error:'连接中断'}]);
 assert.deepEqual(messages.map(message=>message.role),['user','assistant']);assert.equal(messages[0].id,input.id);assert.equal(messages[1].content,'连接中断');
});

test('busy replies disable individual quick sends',()=>{
 const replies:string[]=[];const view=render(<ConversationSuggestions prompt={clarification} disabled onChoose={text=>replies.push(text)}/>);
 const button=screen.getByRole('button',{name:/采用 Q1 建议/}) as HTMLButtonElement;assert.equal(button.disabled,true);fireEvent.click(button);assert.deepEqual(replies,[]);
 view.rerender(<ConversationSuggestions prompt={clarification} onChoose={text=>replies.push(text)}/>);fireEvent.click(screen.getByRole('button',{name:/采用 Q1 建议/}));assert.equal(replies.length,1);
});
