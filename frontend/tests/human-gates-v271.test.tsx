import {JSDOM} from 'jsdom';
import {afterEach,test} from 'node:test';
import assert from 'node:assert/strict';

const dom=new JSDOM('<!doctype html><html><body></body></html>',{url:'http://localhost:8000/'});
for(const key of ['window','document','HTMLElement','HTMLDialogElement','MutationObserver','Node','Event','MouseEvent','CustomEvent'])Object.defineProperty(globalThis,key,{value:(dom.window as any)[key],configurable:true,writable:true});
Object.defineProperty(globalThis,'navigator',{value:dom.window.navigator,configurable:true});
(globalThis as any).IS_REACT_ACT_ENVIRONMENT=true;
(globalThis as any).EventSource=class{addEventListener(){}close(){}};
dom.window.HTMLElement.prototype.scrollIntoView=function(){};
dom.window.HTMLDialogElement.prototype.showModal=function(){this.open=true;};
dom.window.HTMLDialogElement.prototype.close=function(){this.open=false;};
const React=await import('react');
const {render,fireEvent,screen,waitFor,cleanup}=await import('@testing-library/react');
const {RunCard}=await import('../src/RunCard');
const {App}=await import('../src/App');
const {ConversationContext}=await import('../src/conversation');
const originalFetch=globalThis.fetch;
afterEach(()=>{cleanup();globalThis.fetch=originalFetch;});
const json=(value:any)=>new Response(JSON.stringify(value),{headers:{'Content-Type':'application/json'}});
const result=(status='succeeded',message='已确认当前步骤')=>({id:'turn',client_message_id:'client',status,message,parts:[],pending:[],actions:[]});
const cases={id:'artifact',chat_id:'chat',project_id:'project',type:'cases',title:'账号登录用例',revision:8,items:[{id:'C1',title:'有效账号登录',description:'验证登录结果',preconditions:'账号已启用',steps:[{action:'输入有效密码并登录',expected:'显示账号首页'}]}]};
function fixture(gate='scenario_review',extra:any={},response=result()){
 const artifact={...cases,type:gate==='strategy_review'?'analysis':gate==='scenario_review'?'scenarios':'cases'};
 const run:any={id:'run',chat_id:'chat',status:'waiting',intent:'generate_case',mode:'hitp',stage:gate,stop_after:'complete',updated_at:'2026-09-11T00:00:00Z',artifact_ids:['artifact'],interrupt_id:'gate-2',control_version:5,artifact_revision:8,interrupt:{type:gate,artifact_id:'artifact',artifact_revision:8,title:'请检查当前阶段',confirm_label:'采用此阶段结果，执行下一步',next_stage:'review'},...extra};
 const posts:any[]=[];
 globalThis.fetch=(async(input:any,init:any={})=>{
  const path=String(input).replace('/api','');
  if(path==='/chats/chat/turns'){posts.push(JSON.parse(init.body));return json(response);}
  if(path==='/artifacts/artifact')return json(artifact);
  if(path==='/projects')return json([{id:'project',name:'演示项目'}]);
  if(path==='/settings')return json({model:'demo'});
  if(path==='/projects/project/profiles')return json([{id:'profile',name:'默认方案',version:1,config:{}}]);
  if(path==='/projects/project/chats')return json([{id:'chat',project_id:'project',title:'登录设计'}]);
  if(path==='/chats/chat')return json({chat:{id:'chat',project_id:'project',title:'登录设计'},sources:[],messages:[{id:'seed',role:'user',content:'设计登录用例',metadata:{}}],runs:[run]});
  throw new Error('Unexpected request: '+path);
 }) as typeof fetch;
 return {run,artifact,posts};
}
function card(run:any,onPrompt?:(text:string,artifactId?:string)=>void){render(<ConversationContext.Provider value={{chatId:'chat',onChanged:()=>{}}}><RunCard run={run} onChanged={()=>{}} onTarget={()=>{}} {...(onPrompt?{onPrompt}:{})}/></ConversationContext.Provider>);}

for(const gate of ['strategy_review','scenario_review','case_draft_review','case_result_review'])test(gate+' displays its artifact and dispatches the server-labelled confirmation with version bindings',async()=>{
 const {run,posts}=fixture(gate);card(run);
 await screen.findByRole('heading',{name:'账号登录用例'});
 if(gate.startsWith('case_'))assert.ok(screen.getByRole('table',{name:'C1 步骤与预期结果'}));
 assert.ok(screen.getByText(/下一步：/));
 fireEvent.click(screen.getByRole('button',{name:'采用此阶段结果，执行下一步'}));
 await waitFor(()=>assert.equal(posts.length,1));
 assert.equal(posts[0].command.name,'workflow.continue');
 assert.deepEqual(posts[0].command.arguments,{run_id:'run',interrupt_id:'gate-2',expected_control_version:5,expected_revision:8,approved:true});
});

for(const [gate,stopAfter,label,next] of [
 ['strategy_review','analysis','确认理解，允许生成场景','scenarios'],
 ['scenario_review','scenarios','确认场景，允许生成用例','cases'],
 ['case_draft_review','cases','确认用例，允许评审','review'],
 ['case_result_review','review','确认评审，完成任务','complete'],
])test(gate+' expands a saved goal only through the explicitly labelled next-stage button',async()=>{
 const {run,posts}=fixture(gate,{stop_after:stopAfter});card(run);
 fireEvent.click(screen.getByRole('button',{name:label}));
 await waitFor(()=>assert.equal(posts.length,1));
 assert.equal(posts[0].command.arguments.stop_after,next);
});

test('a rejected source confirmation retains the draft and displays the returned reason',async()=>{
 const {run}=fixture('source_review',{interrupt:{type:'source_review',sources:[],message:'请补充需求'}},result('needs_input','当前来源已更新，请重新确认'));card(run);
 fireEvent.change(screen.getByLabelText('补充需求正文'),{target:{value:'用户可以使用有效账号登录'}});
 fireEvent.click(screen.getByRole('button',{name:'确认资料，继续当前任务'}));
 await screen.findByText('当前来源已更新，请重新确认');
 assert.equal((screen.getByLabelText('补充需求正文') as HTMLTextAreaElement).value,'用户可以使用有效账号登录');
});

test('a deferred pause explains that the task is still reaching its safe boundary',async()=>{
 const {run}=fixture('cases',{status:'running'},result('deferred','已登记暂停，当前批次保存后等待'));card(run);
 fireEvent.click(screen.getByRole('button',{name:'暂停',exact:true}));
 await screen.findByText('已登记暂停，当前批次保存后等待');
});

test('a legacy task stopped after cases can explicitly authorize review without restarting',async()=>{
 const {run,posts}=fixture('workflow_paused',{stop_after:'cases',interrupt:{type:'workflow_paused',reason:'stop_after',artifact_id:'artifact',message:'用例已保存，等待明确允许评审。'}});card(run);
 fireEvent.click(screen.getByRole('button',{name:'允许评审，继续当前任务'}));
 await waitFor(()=>assert.equal(posts.length,1));
 assert.equal(posts[0].command.name,'workflow.continue');
 assert.equal(posts[0].command.arguments.stop_after,'review');
});

test('gate chat shortcuts prepare a scoped draft without sending or continuing',async()=>{
 const {run,posts}=fixture();const drafts:any[]=[];card(run,(text,artifactId)=>drafts.push({text,artifactId}));
 fireEvent.click(screen.getByRole('button',{name:'在聊天中预览修改'}));
 assert.deepEqual(drafts,[{text:'请先预览当前成果的修改，不应用，也不继续任务。',artifactId:'artifact'}]);
 assert.equal(posts.length,0);
});

test('reopening a human task hydrates both mode controls, locks them, and preserves that mode in chat requests',async()=>{
 const {posts}=fixture();render(<App/>);
 const select=await screen.findByLabelText('运行模式');
 await waitFor(()=>assert.equal((select as HTMLSelectElement).value,'hitp'));
 assert.equal(select.hasAttribute('disabled'),true);
 fireEvent.click(screen.getByRole('button',{name:/本次方案/}));
 const human=screen.getByLabelText('人工确认关键节点') as HTMLInputElement;
 assert.equal(human.checked,true);assert.equal(human.disabled,true);
 const auto=screen.getByRole('radio',{name:'自动执行'}) as HTMLInputElement;
 assert.equal(auto.checked,false);assert.equal(auto.disabled,true);
 fireEvent.change(screen.getByLabelText('聊天输入'),{target:{value:'只解释当前场景，不继续'}});
 fireEvent.click(screen.getByRole('button',{name:'发送消息'}));
 await waitFor(()=>assert.equal(posts.length,1));
 assert.equal(posts[0].mode,'hitp');
});
