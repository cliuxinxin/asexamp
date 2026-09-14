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
const originalFetch=globalThis.fetch;
afterEach(async()=>{await act(async()=>{});cleanup();globalThis.fetch=originalFetch;});
const json=(value:unknown)=>new Response(JSON.stringify(value),{status:200,headers:{'Content-Type':'application/json'}});
const review={summary:'已补充锁定后的错误提示，仍需确认锁定时长。',issues:[{title:'锁定时长待确认',detail:'需求未明确自动解锁时间，请补充实际规则。',case_ids:['TC-2'],refs:['source#P2']}],scope:{reviewed_count:3,total_count:3},notes:['已保留人工填写的执行结果。']};
function fixture(prompt:any,status='waiting'){
 const turns:any[]=[];
 const state:any={chat:{id:'chat',project_id:'project',title:'登录测试'},sources:[],messages:[{id:'assistant',role:'assistant',content:'已保存当前成果。',metadata:{}}],runs:[{id:'run',chat_id:'chat',intent:'generate_case',mode:'hitp',stage:prompt.kind==='busy'?'case_review':prompt.kind,status,artifact_ids:[],updated_at:'2026-09-13T00:00:00Z'}],conversation_prompt:prompt};
 globalThis.fetch=(async(input:any,init:any={})=>{
  const path=String(input).replace(/^\/api/,'');
  if(path==='/projects')return json([{id:'project',name:'登录项目'}]);
  if(path==='/settings')return json({model:'mock'});
  if(path==='/projects/project/profiles')return json([{id:'profile',name:'Default',version:1,config:{}}]);
  if(path==='/projects/project/chats')return json([state.chat]);
  if(path==='/chats/chat')return json(state);
  if(path==='/chats/chat/turns'){turns.push(JSON.parse(init.body));return json({id:'turn',status:'succeeded',message:'已收到意见。',parts:[],pending:[],actions:[]});}
  if(path.endsWith('/workspace-state'))return json({});
  return json([]);
 }) as typeof fetch;
 return {turns,state};
}
async function ready(){render(<App/>);await screen.findByRole('region',{name:'当前工作流'});}
const gate={id:'prompt:review:2',kind:'case_result_review',run_id:'run',artifact_id:'cases',artifact_revision:2,title:'请确认评审后的测试用例',message:'确认后完成本轮测试设计。',review};

test('processing has one stage indicator above the composer and no duplicate reply card',async()=>{
 fixture({id:'busy',kind:'busy',busy:true,title:'正在处理当前步骤',message:'后台正在执行任务。'},'running');await ready();
 assert.equal(screen.getAllByRole('region',{name:'当前工作流'}).length,1);
 assert.ok(screen.getByLabelText('聊天输入').closest('.composer-wrap')?.contains(screen.getByRole('region',{name:'当前工作流'})));
 assert.ok(screen.queryByRole('region',{name:'当前对话提示'})===null);
 assert.equal(screen.queryByText('正在处理当前步骤'),null);
 assert.equal(screen.queryByRole('group',{name:'快捷回复'}),null);
});

test('plain confirmation remains above the input without a duplicate stage card',async()=>{
 fixture({...gate,kind:'scenario_review',title:'请确认当前测试场景',review:undefined});await ready();
 assert.ok(screen.queryByRole('region',{name:'当前对话提示'})===null);
 assert.equal(screen.queryByText('请确认当前测试场景'),null);
 const reply=screen.getByRole('button',{name:'确认场景并继续'});
 const input=screen.getByLabelText('聊天输入');
 assert.equal(reply.closest('.composer'),null);
 assert.ok(screen.getByRole('region',{name:'当前工作流'}).textContent?.includes('确认场景'));
 assert.ok(reply.compareDocumentPosition(input)&Node.DOCUMENT_POSITION_FOLLOWING);
});

test('review gate opens opinions in the conversation and keeps confirmation explicit',async()=>{
 const {turns}=fixture(gate);await ready();
 const table=screen.getByRole('table',{name:'AI 评审意见'});
 assert.ok(within(table).getByText('锁定时长待确认'));
 assert.ok(within(table).getByText('需求未明确自动解锁时间，请补充实际规则。'));
 assert.ok(within(table).getByText('TC-2'));
 assert.ok(within(table).getByRole('button',{name:'查看依据 · 1'}));
 assert.ok(screen.getByText('本次评审 3 / 3 条用例'));
 assert.ok(screen.getByText('已保留人工填写的执行结果。'));
 assert.ok(screen.getByText(/直接在输入框中补充意见/));
 assert.equal(screen.queryByRole('dialog'),null);
 assert.equal(turns.length,0);
 assert.ok(screen.getByRole('region',{name:'当前工作流'}).textContent?.includes('确认评审建议'));
 const input=screen.getByLabelText('聊天输入') as HTMLTextAreaElement;
 fireEvent.change(input,{target:{value:'保留我的草稿'}});
 fireEvent.click(screen.getByRole('button',{name:'确认评审建议并修改用例'}));
 await waitFor(()=>assert.equal(turns.length,1));
 assert.equal(turns[0].reply_kind,'confirm');assert.equal(turns[0].reply_to,gate.id);
 assert.equal(input.value,'保留我的草稿');
});

test('additional review feedback is an ordinary message bound to the current review',async()=>{
 const {turns}=fixture(gate);await ready();
 fireEvent.change(screen.getByLabelText('聊天输入'),{target:{value:'锁定时长改为 20 分钟，请更新相关用例。'}});
 fireEvent.click(screen.getByRole('button',{name:'发送消息'}));
 await waitFor(()=>assert.equal(turns.length,1));
 assert.equal(turns[0].reply_to,gate.id);assert.equal(turns[0].reply_kind,undefined);
 assert.equal(turns[0].content,'锁定时长改为 20 分钟，请更新相关用例。');
 assert.ok(screen.getByRole('table',{name:'AI 评审意见'}));
});

test('an empty review is stated explicitly while real questions and errors remain visible',()=>{
 const view=render(<ConversationPrompt prompt={{...gate,review:{summary:'本次评审已完成。',issues:[]}}}/>);
 assert.ok(screen.getByText('本次 AI 评审未列出问题。'));
 view.rerender(<ConversationPrompt waiting prompt={{id:'questions',kind:'clarification',title:'请补充解锁规则',message:'答案会更新需求理解。',questions:[{id:'Q1',question:'多久解锁？',suggestion:'20 分钟'}]}}/>);
 assert.ok(screen.getByText('多久解锁？'));assert.ok(screen.getByText('建议假设：20 分钟'));
 assert.equal(screen.queryByText(/正在处理你的回复/),null);
 view.rerender(<ConversationPrompt prompt={{id:'failed',kind:'failed',title:'操作未完成',message:'模型连接失败，请重试当前步骤。'}}/>);
 assert.ok(screen.getByText('模型连接失败，请重试当前步骤。'));
 view.rerender(<ConversationPrompt prompt={{id:'choice',kind:'selection',title:'选择资料用途',message:'请说明这份资料的用途。',choices:[{id:'supplement',title:'作为补充需求'}]}}/>);
 assert.ok(screen.getByText('作为补充需求'));
});
