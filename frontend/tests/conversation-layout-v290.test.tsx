import {JSDOM} from 'jsdom';
import {afterEach,test} from 'node:test';
import assert from 'node:assert/strict';

const dom=new JSDOM('<!doctype html><html><body></body></html>',{url:'http://localhost/'});
for(const key of ['window','document','HTMLElement','HTMLDialogElement','MutationObserver','Node','Event','MouseEvent','CustomEvent'])Object.defineProperty(globalThis,key,{value:(dom.window as any)[key],configurable:true,writable:true});
Object.defineProperty(globalThis,'navigator',{value:dom.window.navigator,configurable:true});
(globalThis as any).IS_REACT_ACT_ENVIRONMENT=true;
(globalThis as any).EventSource=class{addEventListener(){}close(){}};
dom.window.HTMLElement.prototype.scrollIntoView=function(){};
dom.window.HTMLDialogElement.prototype.showModal=function(){this.open=true;};
dom.window.HTMLDialogElement.prototype.close=function(){this.open=false;};
const React=await import('react');
const {render,fireEvent,screen,waitFor,cleanup,within}=await import('@testing-library/react');
const {App}=await import('../src/App');
const originalFetch=globalThis.fetch;
afterEach(()=>{cleanup();globalThis.fetch=originalFetch;});
const json=(value:unknown)=>new Response(JSON.stringify(value),{status:200,headers:{'Content-Type':'application/json'}});

function fixture(empty=false,editing=false){
 const calls:{path:string;body:any}[]=[];
 const current={id:'scenes',chat_id:'chat',project_id:'project',type:'scenarios',title:'登录场景',revision:3,items:[{id:'S-2',title:'锁定登录',type:'Negative',description:'等待解锁'},{id:'S-1',title:'成功登录',type:'Business'}]};
 const historical={...current,revision:1,items:[{id:'S-2',title:'旧版锁定规则'}]};
 const other={...historical,id:'archived',title:'归档支付场景',items:[{id:'S-OLD',title:'旧支付规则'}]};
 const state:any={chat:{id:'chat',project_id:'project',title:'测试设计对话'},sources:[],messages:empty?[]:[
  {id:'old',role:'assistant',content:'第一版场景。',metadata:{turn_response:{id:'old-turn',status:'succeeded',parts:[{type:'artifact',artifact_id:'scenes',revision:1}],pending:[],actions:[]}}},
  {id:'other',role:'assistant',content:'另一个历史分支。',metadata:{turn_response:{id:'other-turn',status:'succeeded',parts:[{type:'artifact',artifact_id:'archived',revision:1}],pending:[],actions:[]}}},
  {id:'new',role:'assistant',content:'当前场景已保存。',metadata:{turn_response:{id:'new-turn',status:'succeeded',parts:[{type:'artifact',artifact_id:'scenes',revision:3}],pending:[],actions:[]}}},
 ],runs:empty?[]:[{id:'run',chat_id:'chat',status:'waiting',intent:'generate_case',mode:'hitp',stage:'scenario_review',updated_at:'2026-09-12T00:00:00Z',edit_in_progress:editing,artifact_ids:['scenes'],interrupt_id:'gate-3',control_version:4,interrupt:{type:'scenario_review',artifact_id:'scenes',artifact_revision:3}}],conversation_prompt:empty?null:{id:'prompt:gate-3',kind:editing?'busy':'workflow_gate',title:'确认当前场景',message:'请在聊天中回复同意。',busy:editing}};
 globalThis.fetch=(async(input:any,init:RequestInit={})=>{
  const path=String(input).replace(/^\/api/,''),body=init.body?JSON.parse(String(init.body)):undefined;calls.push({path,body});
  if(path==='/projects')return json([{id:'project',name:'订单项目'}]);
  if(path==='/settings')return json({model:'mock'});
  if(path==='/projects/project/profiles')return json([{id:'profile',name:'Default',version:1,config:{}}]);
  if(path==='/projects/project/chats')return json([state.chat]);
  if(path==='/chats/chat')return json(state);
  if(path.startsWith('/chats/chat/workspace-state'))return json({artifact_id:empty?null:'scenes',current_gate:empty?null:{run_id:'run',artifact_id:'scenes',revision:3},stages:empty?[]:[{key:'scenarios',artifact_id:'scenes',revision:3,count:2,status:'current'}],impact:{status:'current',summary:'当前场景已保存。',affected:[],source_ids:[]},next_action:empty?{kind:'none'}:{kind:'confirm',label:'确认场景，继续生成用例',arguments:{run_id:'run',artifact_id:'scenes',expected_revision:3}}});
  if(path==='/artifacts/scenes/revisions/1')return json(historical);
  if(path==='/artifacts/scenes'||path==='/artifacts/scenes/revisions/3')return json(current);
  if(path==='/artifacts/archived'||path==='/artifacts/archived/revisions/1')return json(other);
  if(/^\/artifacts\/[^/]+\/workspace/.test(path))return json({coverage:{totals:{},scenarios:[]},parents:[],related_artifacts:[],sources:[]});
  if(path==='/chats/chat/turns')return json({id:'turn',status:'succeeded',message:'已处理',parts:[],pending:[],actions:[]});
  return json([]);
 }) as typeof fetch;
 return {calls,current,state};
}

async function ready(){render(<App/>);return screen.findByRole('button',{name:/查看当前成果/});}
function submit(text:string){fireEvent.change(screen.getByLabelText('聊天输入'),{target:{value:text}});fireEvent.click(screen.getByRole('button',{name:'发送消息'}));}

test('results stay closed until opened and return-to-chat retains selection and draft',async()=>{
 fixture();const open=await ready();
 assert.equal(screen.queryByRole('table',{name:'场景与需求'}),null);
 assert.ok(screen.getByRole('region',{name:'当前工作流'}));
 assert.equal(screen.getByLabelText('聊天输入').getAttribute('rows'),'2');
 fireEvent.click(open);const viewer=await screen.findByRole('dialog',{name:/查看成果/});
 const checkbox=within(viewer).getByRole('checkbox',{name:'选择 S-2',exact:true});fireEvent.click(checkbox);
 fireEvent.click(within(viewer).getByRole('button',{name:'在对话中修改所选'}));
 assert.equal(screen.queryByRole('dialog',{name:/查看成果/}),null);
 assert.match((screen.getByLabelText('聊天输入') as HTMLTextAreaElement).value,/修改所选/);
 assert.ok(screen.getByText(/当前对象：登录场景.*S-2/));
});

test('history opens frozen and assent still binds the current gate',async()=>{
 const {calls}=fixture();await ready();
 const receipt=await screen.findByRole('button',{name:/登录场景.*v1/});fireEvent.click(receipt);
 const history=await screen.findByRole('dialog',{name:/历史成果/});
 assert.ok(within(history).getByText('旧版锁定规则'));
 assert.ok(within(history).getByText(/只读/));
 fireEvent.click(within(history).getByRole('button',{name:'关闭'}));
 submit('同意');
 await waitFor(()=>assert.equal(calls.filter(call=>call.path==='/chats/chat/turns').length,1));
 const confirmed=calls.filter(call=>call.path==='/chats/chat/turns')[0].body;
 assert.equal(confirmed.reply_to,'prompt:gate-3');assert.equal(confirmed.command,undefined);
});

test('viewing a current result without choosing a mutation target does not scope chat',async()=>{
 const {calls}=fixture();const open=await ready();fireEvent.click(open);await screen.findByRole('dialog',{name:/查看成果/});fireEvent.click(screen.getByRole('button',{name:'返回对话'}));
 submit('只解释当前进度。');await waitFor(()=>assert.ok(calls.find(call=>call.path==='/chats/chat/turns')));const sent=calls.find(call=>call.path==='/chats/chat/turns')!.body;
 assert.equal(sent.artifact_id,undefined);assert.equal(sent.view_order,undefined);assert.equal(sent.selected_ids,undefined);
});

test('reopening the current result preserves an existing selected target',async()=>{
 fixture();const open=await ready();fireEvent.click(open);await screen.findByRole('dialog',{name:/查看成果/});fireEvent.click(screen.getByLabelText('选择 S-2'));fireEvent.click(screen.getByRole('button',{name:'在对话中修改所选'}));
 fireEvent.click(screen.getByRole('button',{name:/查看当前成果/}));await screen.findByRole('dialog',{name:/查看成果/});assert.equal((screen.getByLabelText('选择 S-2') as HTMLInputElement).checked,true);
 fireEvent.click(screen.getByRole('button',{name:'返回对话'}));assert.ok(screen.getByText(/当前对象：登录场景.*S-2/));
});

test('empty conversations keep compact input and shortcuts append to an editable draft',async()=>{
 fixture(true);render(<App/>);await screen.findByText('你想测试什么？');
 assert.equal(screen.queryByRole('region',{name:'用例工作区'}),null);
 const input=screen.getByLabelText('聊天输入') as HTMLTextAreaElement;
 fireEvent.change(input,{target:{value:'保留我写好的要求。'}});
 fireEvent.click(screen.getByRole('button',{name:'学习模板',exact:true}));
 assert.match(input.value,/^保留我写好的要求。\n\n请学习/);
 assert.equal(input.getAttribute('rows'),'2');
 assert.equal(input.style.maxHeight,'144px');
 fireEvent.click(screen.getByRole('button',{name:'收起侧栏'}));
 assert.ok(document.querySelector('.sidebar-collapsed'));
 fireEvent.click(screen.getByRole('button',{name:'展开侧栏'}));
 assert.equal(document.querySelector('.sidebar-collapsed'),null);
});

test('editing shows the server busy prompt without competing confirmation or editor controls',async()=>{
 fixture(false,true);await ready();
 assert.equal(screen.queryByRole('button',{name:'确认场景，继续生成用例'}),null);
 assert.equal(screen.queryByRole('button',{name:'编辑',exact:true}),null);
 assert.equal(screen.getByRole('region',{name:'当前对话提示'}).getAttribute('aria-busy'),'true');
 assert.ok(screen.getByRole('region',{name:'当前工作流'}).textContent?.includes('AI 正在处理'));
 assert.equal(screen.getByRole('region',{name:'当前工作流'}).textContent?.includes('等待你的确认'),false);
 assert.equal(document.querySelectorAll('textarea').length,1);
});

test('clearing the scope chip clears row selection and removes the next turn scope',async()=>{
 const {calls}=fixture();const open=await ready();fireEvent.click(open);
 fireEvent.click((await screen.findByRole('dialog',{name:/查看成果/})).querySelector('[aria-label="选择 S-2"]')!);
 fireEvent.click(screen.getByRole('button',{name:'在对话中修改所选'}));
 fireEvent.click(screen.getByRole('button',{name:'取消修改目标'}));
 await waitFor(()=>assert.equal(screen.queryByText(/当前对象：/),null));
 submit('总结当前场景。');
 await waitFor(()=>assert.ok(calls.find(call=>call.path==='/chats/chat/turns')));
 assert.equal(calls.find(call=>call.path==='/chats/chat/turns')!.body.selected_ids,undefined);
});

test('a checkbox scopes direct chat and preserves the visible filtered row order',async()=>{
 const {calls}=fixture();const open=await ready();fireEvent.click(open);await screen.findByRole('dialog',{name:/查看成果/});
 fireEvent.change(screen.getByLabelText('筛选类型'),{target:{value:'Business'}});
 fireEvent.click(screen.getByRole('checkbox',{name:'选择 S-1',exact:true}));
 fireEvent.click(screen.getByRole('button',{name:'返回对话'}));
 submit('说明这一条的成功条件。');
 await waitFor(()=>assert.ok(calls.find(call=>call.path==='/chats/chat/turns')));
 const sent=calls.find(call=>call.path==='/chats/chat/turns')!.body;
 assert.deepEqual(sent.selected_ids,['S-1']);assert.deepEqual(sent.view_order,['S-1']);assert.equal(sent.artifact_revision,3);
 await waitFor(()=>assert.equal((screen.getByLabelText('聊天输入') as HTMLTextAreaElement).value,''));
 submit('继续解释这条场景，不修改。');
 await waitFor(()=>assert.equal(calls.filter(call=>call.path==='/chats/chat/turns').length,2));
 assert.deepEqual(calls.filter(call=>call.path==='/chats/chat/turns')[1].body.selected_ids,['S-1']);
});
