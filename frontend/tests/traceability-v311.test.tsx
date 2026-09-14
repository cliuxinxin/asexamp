import {JSDOM} from 'jsdom';
import {afterEach,test} from 'node:test';
import assert from 'node:assert/strict';
const dom=new JSDOM('<!doctype html><html><body></body></html>',{url:'http://localhost/'});
for(const key of ['window','document','HTMLElement','HTMLDialogElement','MutationObserver','Node','Event','MouseEvent','CustomEvent'])Object.defineProperty(globalThis,key,{value:(dom.window as any)[key],configurable:true,writable:true});
Object.defineProperty(globalThis,'navigator',{value:dom.window.navigator,configurable:true});
(globalThis as any).IS_REACT_ACT_ENVIRONMENT=true;
dom.window.HTMLDialogElement.prototype.showModal=function(){this.open=true;};
dom.window.HTMLDialogElement.prototype.close=function(){this.open=false;};
const React=await import('react');
const {render,fireEvent,screen,cleanup,waitFor,within,act}=await import('@testing-library/react');
const {TraceabilityPanel,traceRows}=await import('../src/TraceabilityPanel');
const originalFetch=globalThis.fetch;
afterEach(async()=>{await act(async()=>{});cleanup();globalThis.fetch=originalFetch;});
const node=(key:string,kind:string,item_id:string,parent_keys:string[]=[],other:any={})=>({key,kind,item_id,title:'标题 '+item_id,artifact_id:'artifact-'+kind,artifact_title:'成果 '+kind,revision:2,chat_id:'chat',parent_keys,statuses:['linked'],missing:false,stale:false,independent:false,direct:false,basis:[],...other});
const nodes=[node('r','analysis','R1'),node('s1','scenarios','S1',['r']),node('s2','scenarios','S2',['r']),node('c','cases','C1',['s1'],{stale:true,statuses:['stale'],basis:[{artifact_id:'artifact-scenarios',item_id:'S1',revision:1,current_revision:2,stale:true,missing:false}]}),node('c-direct','cases','C-direct',['r'],{direct:true,statuses:['skipped_scenarios']}),node('ind','cases','I1',[],{independent:true,statuses:['independent']})];
const data={project_id:'project',chat_id:'chat',summary:{requirements:1,scenarios:2,cases:3,missing:0,stale:1,independent:1,direct_cases:1},chats:[{id:'chat',title:'登录测试',nodes}],notes:['结构覆盖不代表已执行通过。']};
const json=(body:unknown,status=200)=>new Response(JSON.stringify(body),{status,headers:{'Content-Type':'application/json'}});
function fixture(){const reads:string[]=[];globalThis.fetch=(async(input:any)=>{reads.push(String(input));return json(data);}) as typeof fetch;return reads;}

test('tree table opens exact rows, collapses branches and filters stale while keeping ancestors',async()=>{
 fixture();const opened:any[]=[];render(<TraceabilityPanel projectId="project" chatId="chat" onClose={()=>{}} onOpen={(...args)=>opened.push(args)}/>);
 const table=await screen.findByRole('table',{name:'需求场景用例层级表'});
 assert.ok(within(table).getByText('场景已跳过'));assert.ok(within(table).getByText('独立 N/A'));
 fireEvent.click(within(table).getByRole('button',{name:'收起 S1'}));
 assert.equal(within(table).queryByRole('button',{name:'C1 标题 C1'}),null);
 fireEvent.click(within(table).getByRole('button',{name:'展开 S1'}));
 fireEvent.change(screen.getByLabelText('追溯矩阵筛选'),{target:{value:'stale'}});
 assert.ok(within(table).getByRole('button',{name:'R1 标题 R1'}));
 assert.ok(within(table).getByRole('button',{name:'S1 标题 S1'}));
 assert.equal(within(table).queryByRole('button',{name:'S2 标题 S2'}),null);
 fireEvent.click(within(table).getByRole('button',{name:'C1 标题 C1'}));
 assert.deepEqual(opened,[['artifact-cases','C1','chat']]);
 assert.ok(within(table).getByText('S1 · v1 → 当前 v2'));
});

test('scope toggles from active chat to entire project and refreshes through a single compact endpoint',async()=>{
 const reads=fixture();render(<TraceabilityPanel projectId="project" chatId="chat" onClose={()=>{}} onOpen={()=>{}}/>);
 await screen.findByRole('table');assert.deepEqual(reads,['/api/projects/project/traceability?chat_id=chat']);
 fireEvent.change(screen.getByLabelText('追溯矩阵范围'),{target:{value:'project'}});
 await waitFor(()=>assert.equal(reads.length,2));
 assert.equal(reads[1],'/api/projects/project/traceability');
 await screen.findByText('登录测试');
 fireEvent.click(screen.getByRole('button',{name:'刷新'}));
 await waitFor(()=>assert.equal(reads.length,3));
});

test('multi-parent tree repeats display paths while rendering is capped without losing rows',()=>{
 const repeated={...data,chats:[{...data.chats[0],nodes:[node('r1','analysis','R1'),node('r2','analysis','R2'),node('s','scenarios','S1',['r1','r2'])]}]} as any;
 const shared=traceRows(repeated,'all',new Set(),200);
 assert.equal(shared.rows.length,4);assert.equal(new Set(shared.rows.map(row=>row.path)).size,4);
 const large={...data,chats:[{...data.chats[0],nodes:Array.from({length:250},(_,i)=>node('r'+i,'analysis','R'+i))}]} as any;
 const page=traceRows(large,'all',new Set(),200);assert.equal(page.rows.length,200);assert.ok(page.hasMore);
 assert.equal(traceRows(large,'all',new Set(),400).rows.length,250);
});

test('empty results and request errors give clear recoverable messages',async()=>{
 let fail=true;globalThis.fetch=(async()=>fail?json({detail:'读取失败'},500):json({...data,chats:[],summary:{requirements:0,scenarios:0,cases:0,missing:0,stale:0,independent:0,direct_cases:0}})) as typeof fetch;
 render(<TraceabilityPanel projectId="project" onClose={()=>{}} onOpen={()=>{}}/>);
 await screen.findByRole('alert');fail=false;fireEvent.click(screen.getByRole('button',{name:'刷新'}));
 await screen.findByText('还没有已保存的需求、场景或用例。生成后即可在这里查看关联。');
 assert.equal(screen.queryByRole('alert'),null);
});
