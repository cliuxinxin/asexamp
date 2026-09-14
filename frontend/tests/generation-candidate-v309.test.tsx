import {JSDOM} from 'jsdom';
import {afterEach,test} from 'node:test';
import assert from 'node:assert/strict';

const dom=new JSDOM('<!doctype html><html><body></body></html>',{url:'http://localhost/'});
for(const key of ['window','document','HTMLElement','HTMLDialogElement','MutationObserver','Node','Event','MouseEvent'])Object.defineProperty(globalThis,key,{value:(dom.window as any)[key],configurable:true,writable:true});
Object.defineProperty(globalThis,'navigator',{value:dom.window.navigator,configurable:true});
(globalThis as any).IS_REACT_ACT_ENVIRONMENT=true;
dom.window.HTMLDialogElement.prototype.showModal=function(){this.open=true;};
dom.window.HTMLDialogElement.prototype.close=function(){this.open=false;};
const React=await import('react');
const {render,fireEvent,screen,waitFor,cleanup,within}=await import('@testing-library/react');
const {ConversationParts}=await import('../src/ConversationParts');
const {WorkflowSummary}=await import('../src/WorkflowSummary');
const {ConversationPrompt}=await import('../src/ConversationPrompt');
const originalFetch=globalThis.fetch;
afterEach(()=>{cleanup();globalThis.fetch=originalFetch;});
const json=(value:unknown,status=200)=>new Response(JSON.stringify(value),{status,headers:{'Content-Type':'application/json'}});
const part={type:'generation_candidate' as const,candidate_id:'candidate-1',run_id:'run-1',stage:'cases',attempts:3,max_retries:3,request_count:4,title:'未通过校验的用例',item_count:3,issues:[{message:'缺少 S2 的用例',category:'incomplete_coverage',missing_input_ids:['S2']}]};
const candidate={id:'candidate-1',run_id:'run-1',stage:'cases',kind:'cases',attempts:3,max_retries:3,request_count:4,created_at:'2026-09-14T06:00:00Z',items:[
 {id:'C1',title:'正常登录',scenario_id:'S1',steps:[{action:'提交账号',expected:'显示首页'},{action:{invalid:'这是对象'},expected:['异常数据']}],refs:['doc#P1'],custom_field:{label:'自定义内容'}},
 {id:'C2',title:{invalid_title:'无效标题'},steps:'模型返回的无效步骤'},
 '无法转换为条目的模型内容',
],report:{summary:'已经生成部分用例'},issues:part.issues,history:[{attempt:1,call_id:'call-first',result:{items:[]},validation_error:'首次缺少场景覆盖'}]};
const response={id:'turn',client_message_id:'client',status:'failed',message:'场景覆盖仍未完整。',parts:[part],pending:[],actions:[]};

test('exhausted repair attachment opens saved content safely and offers raw download without approval or normal export',async()=>{
 const calls:string[]=[];
 globalThis.fetch=(async(input:any)=>{calls.push(String(input));return json(candidate);}) as typeof fetch;
 render(<ConversationParts chatOnly compact response={response} onChanged={()=>assert.fail('read-only')} onTarget={()=>assert.fail('read-only')}/>);
 assert.deepEqual(calls,[]);
 assert.ok(screen.getByText('未通过校验 · 已自动修复 3 次'));
 fireEvent.click(screen.getByRole('button',{name:/查看已生成内容/}));
 const dialog=await screen.findByRole('dialog',{name:'未通过校验的用例'});
 await within(dialog).findByText('正常登录');
 const table=within(dialog).getByRole('table',{name:'未通过校验的生成内容'});
 assert.ok(within(table).getByText('提交账号'));assert.ok(within(table).getByText('显示首页'));
 assert.ok(within(table).getByText(/这是对象/));assert.ok(within(table).getByText(/无效标题/));
 assert.ok(within(table).getByText('模型返回的无效步骤'));assert.ok(within(table).getByText('无法转换为条目的模型内容'));
 assert.ok(within(dialog).getByText('缺少 S2 的用例'));assert.ok(within(dialog).getByText('缺失输入：S2'));
 assert.ok(within(dialog).getByText('首次请求 + 3 次自动修复，共 4 次模型请求。'));
 assert.equal(within(dialog).getByRole('link',{name:'下载生成记录 JSON'}).getAttribute('href'),'/api/runs/run-1/candidates/candidate-1/download');
 assert.equal(within(dialog).queryByRole('button',{name:/确认|应用|导出 Excel/}),null);
 assert.deepEqual(calls,['/api/runs/run-1/candidates/candidate-1']);
});

test('candidate dialog reports read failure and can retry loading without regenerating',async()=>{
 let count=0;globalThis.fetch=(async()=>++count===1?json({detail:'记录读取暂时失败'},503):json(candidate)) as typeof fetch;
 render(<ConversationParts response={response} onChanged={()=>{}} onTarget={()=>{}}/>);
 fireEvent.click(screen.getByRole('button',{name:/查看已生成内容/}));
 await screen.findByText('记录读取暂时失败');
 fireEvent.click(screen.getByRole('button',{name:'重新读取'}));
 await screen.findByText('正常登录');assert.equal(count,2);
});

test('failed and cancelled status override stale confirmation or busy prompts using the actual failed stage',()=>{
 const run={id:'run',status:'failed',stage:'scenarios',error:'未覆盖全部输入',intent:'generate_case',mode:'hitp',artifact_ids:[],updated_at:'now'};
 const prompt={id:'old',kind:'strategy_review',title:'确认需求理解',message:'请确认',busy:true};
 const view=render(<WorkflowSummary run={run} prompt={prompt} hasResult onOpen={()=>{}}/>);
 const summary=screen.getByRole('region',{name:'当前工作流'});
 assert.ok(within(summary).getByText('生成场景'));
 assert.ok(summary.classList.contains('failed'));
 assert.ok(!summary.textContent?.includes('等待你的确认'));assert.ok(!summary.textContent?.includes('AI 正在处理'));
 view.rerender(<WorkflowSummary run={{...run,status:'cancelled'}} prompt={prompt} hasResult onOpen={()=>{}}/>);
 assert.ok(summary.classList.contains('cancelled'));assert.ok(!summary.textContent?.includes('等待你的确认'));
});

test('current failure prompt avoids duplicating the assistant error while preserving legacy standalone display',()=>{
 const prompt={id:'failed',kind:'failed',title:'当前步骤未完成',message:'模型未覆盖本批全部输入'};
 const view=render(<ConversationPrompt prompt={prompt} errorInConversation/>);
 assert.ok(screen.queryByRole('region',{name:'当前对话提示'})===null);
 view.rerender(<ConversationPrompt prompt={prompt}/>);
 assert.ok(screen.getByText(prompt.message));
});

test('automatic repair progress replaces generic busy text and disappears once failed',()=>{
 const run={id:'run',status:'running',stage:'scenarios',intent:'generate_case',mode:'hitp',artifact_ids:[],updated_at:'now',repair_progress:{task:'generate_scenarios',retry_count:2,max_retries:3,message:'正在补充 R2 对应的场景'}};
 const view=render(<WorkflowSummary run={run} hasResult={false} onOpen={()=>{}}/>);
 assert.ok(screen.getByText('正在修复本步骤 · 第 2 / 3 次。正在补充 R2 对应的场景'));
 view.rerender(<WorkflowSummary run={{...run,status:'failed'}} hasResult={false} onOpen={()=>{}}/>);
 assert.ok(screen.queryByText(/正在修复本步骤/)===null);
});


test('candidate labels use one-based request attempts and render completed batches as read-only tables',async()=>{
 const saved={...candidate,selected_attempt:4,history:[1,2,3,4].map(attempt=>({attempt,call_id:'call-'+attempt,result:{items:[]}})),completed_batches:[
  {items:[{id:'C-previous-1',title:'前批成功用例',scenario_id:'S-previous',steps:[{action:'打开前一流程',expected:'前一流程成功'}],refs:['doc#previous']}],report:{summary:'第一批全部覆盖'}},
  {items:[{id:'C-previous-2',title:'第二批成功用例',steps:[{action:'打开第二流程',expected:'第二流程成功'}]}]},
 ]};
 globalThis.fetch=(async()=>json(saved)) as typeof fetch;
 render(<ConversationParts response={response} onChanged={()=>assert.fail('read-only')} onTarget={()=>assert.fail('read-only')}/>);
 fireEvent.click(screen.getByRole('button',{name:/查看已生成内容/}));
 const dialog=await screen.findByRole('dialog',{name:'未通过校验的用例'});
 await within(dialog).findByText('当前展示第 3 次自动修复保留的内容；其余轮次可在生成记录中查看。');
 assert.ok(within(dialog).getByText('首次生成 · call-1'));
 assert.ok(within(dialog).getByText('自动修复 1 · call-2'));
 assert.ok(within(dialog).getByText('自动修复 2 · call-3'));
 assert.ok(within(dialog).getByText('自动修复 3 · call-4'));
 const completed=within(dialog).getByRole('region',{name:'已通过校验的分批结果'});
 const first=within(completed).getByRole('table',{name:'已通过校验的第 1 批内容'});
 assert.ok(within(first).getByText('前批成功用例'));assert.ok(within(first).getByText('打开前一流程'));assert.ok(within(first).getByText('前一流程成功'));
 assert.ok(within(completed).getByRole('table',{name:'已通过校验的第 2 批内容'}));
 assert.ok(within(completed).getByText('第二批成功用例'));
 assert.ok(within(dialog).getByRole('table',{name:'未通过校验的生成内容'}));
 assert.ok(within(dialog).queryByRole('button',{name:/确认|应用|导出 Excel/})===null);
});
