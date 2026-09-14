import {JSDOM} from 'jsdom';
import {afterEach,test} from 'node:test';
import assert from 'node:assert/strict';
const dom=new JSDOM('<!doctype html><html><body></body></html>',{url:'http://localhost/'});
for(const key of ['window','document','HTMLElement','MutationObserver','Node','Event','MouseEvent','KeyboardEvent','CustomEvent'])Object.defineProperty(globalThis,key,{value:(dom.window as any)[key],configurable:true,writable:true});
Object.defineProperty(globalThis,'navigator',{value:dom.window.navigator,configurable:true});
(globalThis as any).IS_REACT_ACT_ENVIRONMENT=true;
const React=await import('react');
const {render,fireEvent,screen,waitFor,cleanup,act,within}=await import('@testing-library/react');
const {FullScreenReviewer}=await import('../src/FullScreenReviewer');
const {reviewChanges,applyDecisions,cellChanges,globalIssues}=await import('../src/table-review-model');
const originalFetch=globalThis.fetch;
afterEach(async()=>{await act(async()=>{});cleanup();globalThis.fetch=originalFetch;});
const json=(value:unknown,status=200)=>new Response(JSON.stringify(value),{status,headers:{'Content-Type':'application/json'}});
const original=[{id:'TC1',title:'登录',preconditions:'未登录',steps:[{action:'打开首页',expected:'展示首页'},{action:'点击登录',expected:'进入系统'}],refs:['src#P1'],scenario_id:'S1',type:'Business',priority:'P1'}];
const proposed=[{...original[0],title:'有效登录',preconditions:'已登录',steps:[{action:'打开登录页面',expected:'展示首页'},{action:'输入有效账号后登录',expected:'进入工作台'}]}];
const columns=[{field:'id',header:'编号',value_source:'ai'},{field:'title',header:'名称',value_source:'ai'},{field:'preconditions',header:'准备条件',value_source:'ai'},{field:'steps',header:'操作',value_source:'derived'},{field:'expected',header:'期望',value_source:'derived'}];
function project(items:any[],layout='case',cols=columns){return {columns:cols,rows:items.flatMap(item=>{
 const steps=item.steps??[];const indexes=layout==='step'?steps.map((_:any,index:number)=>index):[null];
 return indexes.map((index:number|null)=>({item_id:item.id,step_index:index,cells:cols.map(column=>column.field==='steps'||column.field==='expected'?(index===null?steps:[steps[index]]).map((step:any,i:number)=>`${index===null?i+1:index+1}. ${step[column.field==='steps'?'action':'expected']}`).join('\n'):String(item[column.field]??''))}));
 })};}
function fixture(options:any={}){
 const writes:any[]=[];let view:any={artifact_id:'cases',title:'登录用例',artifact_revision:1,profile_id:'p',profile_revision:2,layout:'case',columns,original_items:structuredClone(original),proposed_items:structuredClone(proposed),issues:[{title:'操作要可执行',case_ids:['TC1'],field:'steps'},{title:'整体考虑边界'}],run_id:'run',proposal_id:'proposal1',prompt_id:'gate1',read_only:false,...options};
 function response(){return {...view,original_rows:project(view.original_items,view.layout,view.columns).rows,proposed_rows:project(view.proposed_items,view.layout,view.columns).rows};}
 globalThis.fetch=(async(input:any,init:any={})=>{
  const url=new URL(String(input),'http://localhost');
  if(url.pathname.endsWith('/table-review'))return json(response());
  const body=init.body?JSON.parse(init.body):{};
  if(url.pathname.endsWith('/project'))return json(project(body.items,body.layout,view.columns));
  if(url.pathname.endsWith('/save')){writes.push(body);if(options.failSave&&writes.length===1)return json({detail:'连接暂时中断'},503);return json({artifact:{id:'cases',revision:2},message:'审阅已保存'});}
  throw new Error('Unexpected '+url.pathname);
 }) as typeof fetch;
 return {writes,setView:(patch:any)=>{view={...view,...patch};}};
}
function View(props:any={}){return <FullScreenReviewer request={{artifactId:'cases',runId:'run',proposalId:'proposal1'}} chatId="chat" messages={[]} onClose={()=>{}} onSaved={()=>{}} onSend={async()=>{}} {...props}/>;}
async function loaded(){await screen.findByRole('table',{name:'测试用例评审表格'});await waitFor(()=>assert.ok(!screen.queryByText('正在加载表格与评审建议…')));}
async function projected(){await waitFor(()=>assert.ok(!(screen.getByRole('button',{name:'导出 Excel'}) as HTMLButtonElement).disabled));}
function cell(name:string){return screen.getByRole('button',{name});}

test('mixed accept, reject and inline manual edits preserve choices when accepting the rest and save canonical data',async()=>{
 const {writes}=fixture();render(<View/>);await loaded();
 assert.deepEqual(screen.getAllByRole('columnheader').slice(1).map(node=>node.textContent),columns.map(column=>column.header));
 assert.ok(document.querySelector('del'));assert.ok(document.querySelector('ins'));
 fireEvent.click(cell('TC1 名称'));fireEvent.click(screen.getByRole('button',{name:'拒绝修改'}));
 fireEvent.doubleClick(cell('TC1 准备条件'));
 const input=screen.getByRole('textbox',{name:'编辑 准备条件'});assert.ok(input.closest('td'));assert.equal(screen.getAllByRole('dialog').length,1);
 fireEvent.change(input,{target:{value:'已认证并初始化测试数据'}});fireEvent.click(screen.getByRole('button',{name:'保存单元格'}));
 fireEvent.click(screen.getByRole('button',{name:'全部接受'}));await projected();
 fireEvent.click(screen.getByRole('button',{name:'保存并完成评审'}));await waitFor(()=>assert.equal(writes.length,1));
 assert.equal(writes[0].items[0].title,'登录');assert.equal(writes[0].items[0].preconditions,'已认证并初始化测试数据');
 assert.deepEqual(writes[0].items[0].steps,proposed[0].steps);assert.deepEqual(writes[0].items[0].refs,['src#P1']);assert.equal(writes[0].prompt_id,'gate1');
});

test('per-step rejection remains visible in the combined case layout while another action is pending',async()=>{
 fixture();render(<View/>);await loaded();
 fireEvent.change(screen.getByLabelText('表格与 Excel 布局'),{target:{value:'step'}});await screen.findByRole('button',{name:'TC1 操作 第 1 步'});await projected();
 fireEvent.click(cell('TC1 操作 第 1 步'));fireEvent.click(screen.getByRole('button',{name:'拒绝修改'}));await projected();
 fireEvent.change(screen.getByLabelText('表格与 Excel 布局'),{target:{value:'case'}});await screen.findByRole('button',{name:'TC1 操作'});await projected();
 const rendered=cell('TC1 操作');const added=[...rendered.querySelectorAll('ins')].map(node=>node.textContent).join('');
 assert.ok(!added.includes('登录页面'));assert.ok(rendered.textContent!.includes('打开首页'));assert.ok(rendered.textContent!.includes('输入有效账号后登录'));
});

test('paired step editor never serializes numbered text back into an array and rejects incomplete expected results',async()=>{
 const {writes}=fixture();render(<View/>);await loaded();
 fireEvent.doubleClick(cell('TC1 期望'));
 fireEvent.change(screen.getByRole('textbox',{name:'第 1 步操作'}),{target:{value:'第一行\n1. 这里是操作文本'}});
 fireEvent.change(screen.getByRole('textbox',{name:'第 1 步预期结果'}),{target:{value:''}});fireEvent.click(screen.getByRole('button',{name:'保存单元格'}));
 await screen.findByRole('alert');assert.ok(screen.getByRole('textbox',{name:'第 1 步操作'}));
 fireEvent.change(screen.getByRole('textbox',{name:'第 1 步预期结果'}),{target:{value:'对应结果'}});fireEvent.click(screen.getByRole('button',{name:'保存单元格'}));
 fireEvent.click(screen.getByRole('button',{name:'全部接受'}));await projected();fireEvent.click(screen.getByRole('button',{name:'保存并完成评审'}));await waitFor(()=>assert.equal(writes.length,1));
 assert.equal(writes[0].items[0].steps[0].action,'第一行\n1. 这里是操作文本');assert.equal(writes[0].items[0].steps[0].expected,'对应结果');assert.equal(writes[0].items[0].steps.length,2);
});

test('new proposal while a cell textarea is open preserves the unsaved editor and requires explicit reload',async()=>{
 const state=fixture();const view=render(<View refreshKey="1"/>);await loaded();
 fireEvent.doubleClick(cell('TC1 准备条件'));fireEvent.change(screen.getByRole('textbox',{name:'编辑 准备条件'}),{target:{value:'尚未保存的人工文字'}});
 state.setView({proposal_id:'proposal2',prompt_id:'gate2',proposed_items:[{...proposed[0],title:'另一份建议'}]});view.rerender(<View refreshKey="2"/>);
 await screen.findByText('有新的成果或评审建议。本地审阅内容已保留，请核对后重新载入。');
 assert.equal((screen.getByRole('textbox',{name:'编辑 准备条件'}) as HTMLTextAreaElement).value,'尚未保存的人工文字');assert.ok((screen.getByRole('button',{name:'保存并完成评审'}) as HTMLButtonElement).disabled);
 fireEvent.click(screen.getByRole('button',{name:'放弃本地审阅并载入最新版本'}));await waitFor(()=>assert.ok(!screen.queryByRole('textbox',{name:'编辑 准备条件'})));
});

test('retry after an uncertain save reuses the same client request identifier',async()=>{
 const {writes}=fixture({failSave:true});render(<View/>);await loaded();fireEvent.click(screen.getByRole('button',{name:'全部接受'}));await projected();
 fireEvent.click(screen.getByRole('button',{name:'保存并完成评审'}));await screen.findByRole('alert');
 fireEvent.click(screen.getByRole('button',{name:'保存并完成评审'}));await waitFor(()=>assert.equal(writes.length,2));assert.equal(writes[0].client_request_id,writes[1].client_request_id);
});

test('AI added rows edited by a human remain preserved when accepting unresolved changes',async()=>{
 const added={...original[0],id:'TC2',title:'新用例'};const {writes}=fixture({proposed_items:[...proposed,added]});render(<View/>);await loaded();
 fireEvent.doubleClick(cell('TC2 名称'));fireEvent.change(screen.getByRole('textbox',{name:'编辑 名称'}),{target:{value:'人工修正新增标题'}});fireEvent.click(screen.getByRole('button',{name:'保存单元格'}));
 fireEvent.click(screen.getByRole('button',{name:'全部接受'}));await projected();fireEvent.click(screen.getByRole('button',{name:'保存并完成评审'}));await waitFor(()=>assert.equal(writes.length,1));
 assert.equal(writes[0].items.find((item:any)=>item.id==='TC2').title,'人工修正新增标题');
});

test('comments without a mapped case or field remain in global review notes and default columns cannot be edited',async()=>{
 const allColumns=[...columns,{field:'status',header:'状态',value_source:'default'}];fixture({columns:allColumns,issues:[{title:'未知字段意见',case_ids:['TC1'],field:'unmapped'},'总体边界风险']});render(<View/>);await loaded();
 assert.ok(screen.getByText('整体评审意见（2）'));fireEvent.doubleClick(cell('TC1 状态'));assert.equal(screen.queryByRole('textbox',{name:'编辑 状态'}),null);
 fireEvent.click(screen.getByRole('button',{name:'仅看有批注'}));assert.ok(screen.getByText('此筛选下没有用例。'));
});

test('model helpers keep separate step expected changes and reject added or deleted rows without losing other fields',()=>{
 const changes=reviewChanges(original,proposed);const expected=cellChanges(changes,'TC1',columns[4],1);
 assert.equal(expected.length,1);const rejected=applyDecisions(proposed,original,proposed,expected,'reject');
 assert.equal(rejected[0].steps[1].expected,'进入系统');assert.equal(rejected[0].steps[1].action,'输入有效账号后登录');
 assert.equal(globalIssues([{title:'x',case_id:'unknown'}],original,columns).length,1);
});
