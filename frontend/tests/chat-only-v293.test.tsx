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
const {RunCard}=await import('../src/RunCard');
const originalFetch=globalThis.fetch;
afterEach(()=>{cleanup();globalThis.fetch=originalFetch;});
const json=(v:unknown)=>new Response(JSON.stringify(v),{status:200,headers:{'Content-Type':'application/json'}});
const scenePrompt={id:'prompt:scenes-3',kind:'workflow_gate',run_id:'run',artifact_id:'scenes',artifact_revision:3,title:'请确认测试场景',message:'场景已保存。回复同意，将使用当前场景生成用例。'};
function fixture(options:{onTurn?:(number:number,state:any)=>Promise<void>|void;proposal?:boolean}={}){
 const calls:any[]=[];
 const artifact:any={id:'scenes',chat_id:'chat',project_id:'project',type:'scenarios',title:'登录场景',revision:3,items:[{id:'S-2',title:'锁定登录',type:'Negative',description:'等待解锁'},{id:'S-1',title:'成功登录',type:'Business'}]};
 const state:any={chat:{id:'chat',project_id:'project',title:'登录测试'},sources:[],messages:[{id:'current',role:'assistant',content:'场景已保存。',metadata:{turn_response:{id:'t1',parts:[{type:'artifact',artifact_id:'scenes',revision:3}],pending:[{message:'这是以前的确认提示'}],status:'succeeded'}}}],runs:[{id:'run',chat_id:'chat',status:'waiting',mode:'hitp',intent:'generate_case',stage:'scenario_review',updated_at:'2026-09-12T00:00:00Z',artifact_ids:['scenes'],interrupt:{type:'scenario_review',artifact_id:'scenes',artifact_revision:3}}],conversation_prompt:scenePrompt};
 globalThis.fetch=(async(input:any,init:any={})=>{
  const path=String(input).replace(/^\/api/,''),body=init.body?JSON.parse(init.body):undefined;calls.push({path,body});
  if(path==='/projects')return json([{id:'project',name:'登录项目'}]);
  if(path==='/settings')return json({model:'mock'});
  if(path==='/projects/project/profiles')return json([{id:'profile',name:'Default',version:1,config:{}}]);
  if(path==='/projects/project/chats')return json([state.chat]);
  if(path==='/chats/chat')return json(state);
  if(path==='/artifacts/scenes'||path==='/artifacts/scenes/revisions/3')return json(artifact);
  if(path==='/artifacts/scenes/workspace')return json({coverage:{totals:{},scenarios:[]},parents:[],related_artifacts:[],sources:[]});
  if(path.startsWith('/chats/chat/workspace-state'))return json({artifact_id:'scenes',current_gate:{run_id:'run',artifact_id:'scenes'},stages:[{key:'scenarios',artifact_id:'scenes',revision:3,count:2,status:'current'}],impact:{status:options.proposal?'pending':'current',summary:'当前场景已保存。',affected:[],source_ids:[]},next_action:{kind:'confirm',label:'确认场景，继续生成用例',arguments:{run_id:'run'}},...(options.proposal?{pending_proposal:{id:'p1',artifact_id:'scenes',summary:'仅修改锁定规则',changes:[{artifact_id:'scenes',expected_revision:3,before_items:artifact.items,operations:[{op:'update',id:'S-2',item:{description:'等待管理员解锁'}}]}]}}:{})});
  if(path==='/chats/chat/turns'){await options.onTurn?.(calls.filter(c=>c.path===path).length,state);return json({id:'turn'+calls.length,status:'succeeded',message:'已处理',parts:[],pending:[],actions:[]});}
  return json([]);
 }) as typeof fetch;
 return {calls,state};
}
async function ready(){render(<App/>);return screen.findByRole('button',{name:/查看当前成果/});}
function send(text:string){fireEvent.change(screen.getByLabelText('聊天输入'),{target:{value:text}});fireEvent.click(screen.getByRole('button',{name:'发送消息'}));}

test('one stage summary and one composer replace scattered workflow controls',async()=>{
 fixture();await ready();const stage=screen.getByRole('region',{name:'当前工作流'});
 assert.equal(screen.getAllByRole('region',{name:'当前工作流'}).length,1);assert.equal(document.querySelectorAll('textarea').length,1);
 assert.ok(screen.queryByRole('region',{name:'当前对话提示'})===null);
 assert.ok(within(stage).getByText('请确认测试场景'));
 for(const label of ['确认场景，继续生成用例','停止','补充资料','编辑','导出 Excel','让 AI 修改此结果'])assert.equal(screen.queryByRole('button',{name:label,exact:true}),null,label);
 assert.equal((screen.getByLabelText('历史对话提示') as HTMLDetailsElement).open,false);
 assert.equal(screen.getByLabelText('聊天输入').getAttribute('rows'),'2');
 const composer=screen.getByLabelText('聊天输入').closest('.composer-wrap')!;for(const label of ['运行模式','附件用途','运行 Profile'])assert.ok(composer.contains(screen.getByLabelText(label)));
});

test('chat assent carries the shown prompt identity while retaining selected rows',async()=>{
 const {calls}=fixture();const open=await ready();fireEvent.click(open);await screen.findByRole('dialog',{name:/查看成果/});fireEvent.change(screen.getByLabelText('筛选类型'),{target:{value:'Business'}});fireEvent.click(screen.getByRole('checkbox',{name:'选择 S-1',exact:true}));fireEvent.click(screen.getByRole('button',{name:'返回对话'}));send('同意');
 await waitFor(()=>assert.equal(calls.filter(c=>c.path==='/chats/chat/turns').length,1));const body=calls.find(c=>c.path==='/chats/chat/turns').body;
 assert.equal(body.reply_to,scenePrompt.id);assert.equal(body.command,undefined);assert.equal(body.artifact_id,'scenes');assert.equal(body.artifact_revision,3);assert.deepEqual(body.selected_ids,['S-1']);assert.deepEqual(body.view_order,['S-1']);
});

test('new server prompt replaces the old one and binds the next turn',async()=>{
 const next={...scenePrompt,id:'prompt:cases-1',title:'请确认用例草稿',message:'用例已生成。回复同意后进行评审。'};
 const {calls}=fixture({onTurn:(_n,state)=>{state.conversation_prompt=next;}});await ready();send('同意');await screen.findByText(next.title);
 assert.equal(screen.queryByText(scenePrompt.title),null);assert.equal(screen.getAllByRole('region',{name:'当前工作流'}).length,1);send('先解释这个用例，不进入评审');
 await waitFor(()=>assert.equal(calls.filter(c=>c.path==='/chats/chat/turns').length,2));const turns=calls.filter(c=>c.path==='/chats/chat/turns');assert.equal(turns[0].body.reply_to,scenePrompt.id);assert.equal(turns[1].body.reply_to,next.id);
});

test('in-flight turns retain the seen prompt when a concurrent question refreshes the next gate',async()=>{
 let release!:()=>void;const first=new Promise<void>(resolve=>{release=resolve;});const next={...scenePrompt,id:'prompt:cases-1',title:'请确认用例草稿'};
 const {calls}=fixture({onTurn:async(n,state)=>{if(n===1)await first;else state.conversation_prompt=next;}});await ready();send('同意');
 try{
  await waitFor(()=>assert.equal(calls.filter(c=>c.path==='/chats/chat/turns').length,1));send('现在在做哪一步？');
  await waitFor(()=>assert.equal(calls.filter(c=>c.path==='/chats/chat/turns').length,2));await waitFor(()=>assert.equal((screen.getByLabelText('聊天输入') as HTMLTextAreaElement).value,''));
  assert.ok(screen.getByText(scenePrompt.title));assert.equal(screen.queryByText(next.title),null);send('同意');await act(async()=>{await new Promise(resolve=>setTimeout(resolve,20));});
  assert.equal(calls.filter(c=>c.path==='/chats/chat/turns').length,2,'duplicate assent must not address the unseen next gate');
 }finally{release();}await screen.findByText(next.title);
});

test('clarification suggestions are text only and disappear when answered',()=>{
 const prompt={id:'p1',kind:'clarification',title:'需要补充两项信息',message:'可以直接采用建议，或者回复自己的答案。',questions:[{id:'q1',question:'多久解锁？',suggestion:'建议 15 分钟'},{id:'q2',question:'错误提示？',suggestion:'建议提示稍后重试'}]};
 const view=render(<ConversationPrompt prompt={prompt}/>);assert.equal(screen.getAllByText(/建议假设/).length,2);assert.equal(screen.queryByRole('button'),null);assert.equal(screen.queryByRole('textbox'),null);
 view.rerender(<ConversationPrompt prompt={{...prompt,id:'p2',questions:[{...prompt.questions[1],answer:'提示联系管理员'}]}}/>);
 assert.equal(screen.queryByText('多久解锁？'),null);assert.equal(screen.queryByText(/建议提示稍后重试/),null);assert.ok(screen.getByText('已确认：提示联系管理员'));
});

test('source clarification and failure run cards stay read-only in conversation mode',()=>{
 globalThis.fetch=(async()=>json([])) as typeof fetch;const base:any={id:'run',status:'waiting',stage:'source_review',updated_at:'now',mode:'hitp',artifact_ids:[],interrupt:{type:'source_review',sources:[{id:'source',name:'样例.xlsx'}]}};
 const view=render(<RunCard run={base} chatOnly compact onChanged={()=>{}} onTarget={()=>{}}/>);assert.equal(screen.queryByRole('textbox'),null);assert.equal(screen.queryByRole('button'),null);
 view.rerender(<RunCard run={{...base,status:'failed',error:'模型连接失败'}} chatOnly compact onChanged={()=>{}} onTarget={()=>{}}/>);assert.ok(screen.getByText('模型连接失败'));assert.equal(screen.queryByRole('button',{name:/重试/}),null);
});

test('change details remain readable while proposal application uses chat',async()=>{
 fixture({proposal:true});await ready();const preview=await screen.findByLabelText('待应用修改');assert.ok(within(preview).getByText('可在聊天中说明是否采用或如何调整。'));assert.equal((within(preview).getByText('查看修改明细').closest('details') as HTMLDetailsElement).open,false);assert.equal(within(preview).queryByRole('button'),null);assert.equal(document.querySelectorAll('textarea').length,1);
});


test('opening source details does not introduce another requirement input or write controls',async()=>{
 const {state}=fixture();state.sources=[{id:'source',name:'新需求.md',role:'supplement',characters:42}];await ready();
 fireEvent.click(screen.getByRole('button',{name:/需求资料 · 1/}));
 const dialog=await screen.findByRole('dialog');
 assert.ok(within(dialog).getByText('新需求.md'));
 assert.equal(within(dialog).queryByRole('textbox'),null);
 assert.equal(within(dialog).queryByRole('combobox'),null);
 assert.equal(within(dialog).queryByRole('button',{name:/粘贴一份需求|移除|添加来源/}),null);
 assert.equal(document.querySelectorAll('textarea').length,1);
});
