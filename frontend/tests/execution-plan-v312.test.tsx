import {JSDOM} from 'jsdom';
import {afterEach,test} from 'node:test';
import assert from 'node:assert/strict';
const dom=new JSDOM('<!doctype html><html><body></body></html>',{url:'http://localhost/'});
for(const key of ['window','document','HTMLElement','MutationObserver','Node','Event','MouseEvent'])Object.defineProperty(globalThis,key,{value:(dom.window as any)[key],configurable:true,writable:true});
Object.defineProperty(globalThis,'navigator',{value:dom.window.navigator,configurable:true});
(globalThis as any).IS_REACT_ACT_ENVIRONMENT=true;
const React=await import('react');
const {render,fireEvent,screen,waitFor,cleanup,act}=await import('@testing-library/react');
const {ExecutionPlan}=await import('../src/ExecutionPlan');
const {ConversationParts}=await import('../src/ConversationParts');
const originalFetch=globalThis.fetch;
afterEach(()=>{cleanup();globalThis.fetch=originalFetch;});
const json=(value:unknown,status=200)=>new Response(JSON.stringify(value),{status,headers:{'Content-Type':'application/json'}});
const waiting={id:'plan1',status:'waiting_confirmation',title:'修改前置条件并导出 Excel',waiting_prompt_id:'proposal1',steps:[
 {id:'edit',title:'修改用例前置条件',status:'completed'},
 {id:'confirm',title:'确认修改',status:'waiting_confirmation',message:'请在修改预览中确认这次更改。'},
 {id:'export',title:'导出 Excel',status:'pending'}
]};
const complete={...waiting,status:'completed',waiting_prompt_id:undefined,steps:waiting.steps.map(step=>({...step,status:'completed',message:undefined}))};

test('a plan part follows existing refresh updates from waiting to complete using GET only',async()=>{
 const requests:{path:string;method:string}[]=[];let latest=waiting;
 globalThis.fetch=(async(input:any,init:any={})=>{requests.push({path:String(input),method:init.method});return json(latest);}) as typeof fetch;
 const response={id:'turn1',client_message_id:'message1',status:'needs_confirmation',message:'已安排修改和导出',parts:[{type:'execution_plan',plan_id:'plan1',chat_id:'chat1',plan:waiting}],pending:[],actions:[]};
 const props={response,onTarget:()=>{},onChanged:()=>{},refreshKey:'1'};
 const view=render(<ConversationParts {...props as any}/>);
 assert.ok(screen.getByRole('region',{name:'本次安排'}));assert.ok(screen.getByText('请在修改预览中确认这次更改。'));
 await waitFor(()=>assert.equal(requests.length,1));
 assert.equal(screen.queryByRole('button',{name:/接受|拒绝|确认修改/}),null);
 latest=complete;
 view.rerender(<ConversationParts {...{...props,refreshKey:'2'} as any}/>);
 await waitFor(()=>assert.equal(screen.getAllByText('已完成').length,4));
 assert.equal(screen.queryByText('等待确认'),null);assert.equal(screen.queryByText('待执行'),null);
 assert.deepEqual(requests,[{path:'/api/chats/chat1/plans/plan1',method:'GET'},{path:'/api/chats/chat1/plans/plan1',method:'GET'}]);
});

test('temporary refresh errors preserve the last plan and allow a read-only retry',async()=>{
 let reads=0;const methods:string[]=[];
 globalThis.fetch=(async(_input:any,init:any={})=>{methods.push(init.method);reads++;return reads===2?json({detail:'Temporary failure'},503):json(reads===1?waiting:complete);}) as typeof fetch;
 const view=render(<ExecutionPlan chatId="chat1" planId="plan1" snapshot={waiting} refreshKey="1"/>);
 await waitFor(()=>assert.equal(reads,1));
 view.rerender(<ExecutionPlan chatId="chat1" planId="plan1" snapshot={waiting} refreshKey="2"/>);
 await screen.findByRole('alert');
 assert.ok(screen.getByText('修改用例前置条件'));assert.equal(screen.getAllByText('等待确认').length,2);assert.ok(screen.getByText('导出 Excel'));
 fireEvent.click(screen.getByRole('button',{name:'刷新安排'}));
 await waitFor(()=>assert.equal(screen.getAllByText('已完成').length,4));
 assert.equal(screen.queryByRole('alert'),null);assert.deepEqual(methods,['GET','GET','GET']);
});

test('a completed historical snapshot stays completed when the current request is unavailable',async()=>{
 globalThis.fetch=(async()=>json({detail:'Offline'},503)) as typeof fetch;
 render(<ExecutionPlan chatId="chat1" planId="plan1" snapshot={complete}/>);
 await screen.findByRole('alert');
 assert.equal(screen.getAllByText('已完成').length,4);assert.equal(screen.queryByText('待执行'),null);assert.equal(screen.queryByText('等待确认'),null);
 assert.equal(screen.queryByRole('button',{name:/继续|接受|拒绝/}),null);
});

test('plan and step text are escaped while private tool payloads are never rendered',async()=>{
 const unsafe={...waiting,title:'<img src=x onerror=alert(1)>',internal_reasoning:'PRIVATE REASONING',steps:[{id:'1',title:'<script>alert(2)</script>',status:'waiting_confirmation',message:'<b>请确认</b>',tool_name:'modify_artifact_tool',arguments:{secret:'DO NOT DISPLAY'}}]};
 globalThis.fetch=(async()=>json(unsafe)) as typeof fetch;
 const {container}=render(<ExecutionPlan chatId="chat1" planId="plan1" snapshot={unsafe}/>);
 await screen.findByText('<script>alert(2)</script>');
 assert.ok(screen.getByText('<img src=x onerror=alert(1)>'));assert.ok(screen.getByText('<b>请确认</b>'));
 assert.equal(container.querySelector('img,script,b'),null);
 assert.doesNotMatch(container.textContent??'',/PRIVATE REASONING|modify_artifact_tool|DO NOT DISPLAY/);
});

test('switching plans never displays an earlier in-flight response under the new plan',async()=>{
 let resolveOld!:(value:Response)=>void;
 globalThis.fetch=(async(input:any)=>String(input).endsWith('/plan1')?new Promise<Response>(resolve=>{resolveOld=resolve;}):json({id:'plan2',title:'新的导出安排',status:'cancelled',steps:[{id:'export',title:'导出当前场景',status:'cancelled'}]})) as typeof fetch;
 const view=render(<ExecutionPlan chatId="chat1" planId="plan1" snapshot={waiting}/>);
 view.rerender(<ExecutionPlan chatId="chat1" planId="plan2"/>);
 await screen.findByText('新的导出安排');
 await act(async()=>{resolveOld(json(waiting));});
 assert.equal(screen.queryByText('修改用例前置条件'),null);assert.equal(screen.getAllByText('已取消').length,2);
});
