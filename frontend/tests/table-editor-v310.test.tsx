import {JSDOM} from 'jsdom';
import {test,afterEach} from 'node:test';
import assert from 'node:assert/strict';
const dom=new JSDOM('<!doctype html><html><body></body></html>',{url:'http://localhost:8000/'});
for(const key of ['window','document','HTMLElement','HTMLDialogElement','MutationObserver','Node','Event','MouseEvent'])Object.defineProperty(globalThis,key,{value:(dom.window as any)[key],configurable:true,writable:true});
Object.defineProperty(globalThis,'navigator',{value:dom.window.navigator,configurable:true});
(globalThis as any).IS_REACT_ACT_ENVIRONMENT=true;
dom.window.HTMLDialogElement.prototype.showModal=function(){this.open=true;};
dom.window.HTMLDialogElement.prototype.close=function(){this.open=false;};
const React=await import('react');
const {render,fireEvent,screen,waitFor,cleanup,within}=await import('@testing-library/react');
const {ArtifactCard}=await import('../src/ArtifactCard');
const {rowContext,matchesScope}=await import('../src/ArtifactLineage');
afterEach(cleanup);
const json=(x:any)=>new Response(JSON.stringify(x),{headers:{'Content-Type':'application/json'}});
const original={id:'TC-1',title:'登录校验',type:'Business',priority:'P1',description:'验证可登录',preconditions:'已注册',scenario_id:'SC-1',steps:[{action:'输入有效账号',expected:'登录成功',automation:'keep-step'}],refs:['src1#P1'],custom:{important:true},status:false};
const artifact={id:'a1',chat_id:'c1',type:'cases',title:'测试用例',revision:3,items:[original],report:{}};
function fixture(data:any=artifact){let submitted:any;globalThis.fetch=async(path:any,init:RequestInit={})=>{if(init.method==='PUT'){submitted=JSON.parse(String(init.body));return json({...data,revision:data.revision+1,items:submitted.items});}if(String(path).includes('/workspace'))return json({lineage_rows:[]});return json(data);};return ()=>submitted;}

test('current chat viewer edits case cells and preserves unedited fields, links and step metadata',async()=>{
 const submitted=fixture();let changed=0;let target:any;
 render(<ArtifactCard id="a1" snapshot={artifact} chatOnly simplified onChanged={()=>changed++} onTarget={(_a,ids)=>target=ids}/>);
 fireEvent.click(screen.getByRole('button',{name:'编辑',exact:true}));
 const table=screen.getByRole('table',{name:'用例表格编辑'});
 fireEvent.change(within(table).getByLabelText('条目 TC-1 标题'),{target:{value:'登录后进入首页'}});
 fireEvent.change(within(table).getByLabelText('TC-1 预期结果 1'),{target:{value:'显示首页'}});
 fireEvent.click(screen.getByRole('button',{name:'保存新版本'}));
 await waitFor(()=>assert.equal(changed,1));
 const item=submitted().items[0];assert.equal(item.title,'登录后进入首页');assert.equal(item.steps[0].expected,'显示首页');assert.equal(item.steps[0].automation,'keep-step');assert.deepEqual(item.custom,{important:true});assert.equal(item.status,false);assert.equal(item.scenario_id,'SC-1');assert.deepEqual(item.refs,['src1#P1']);assert.equal(submitted().expected_revision,3);
 fireEvent.click(screen.getByLabelText('选择 TC-1'));fireEvent.click(screen.getByRole('button',{name:'让 AI 修改选中 1 条'}));assert.deepEqual(target,['TC-1']);
});

test('scenario table edits existing custom fields and rows without proposing case column changes',async()=>{
 const data={...artifact,type:'scenarios',title:'场景',items:[{id:'SC-1',title:'原场景',type:'Business',priority:'P1',description:'现有规则',requirement_ids:['REQ-1'],refs:['src1#P1'],custom_label:'原说明'}]};
 const submitted=fixture(data);render(<ArtifactCard id="a1" snapshot={data} chatOnly onChanged={()=>{}} onTarget={()=>{}}/>);
 fireEvent.click(screen.getByRole('button',{name:'编辑',exact:true}));assert.ok(screen.getByRole('table',{name:'场景表格编辑'}));
 assert.equal(screen.queryByRole('button',{name:'添加列',exact:true}),null);assert.equal(screen.queryByRole('button',{name:'删除列 custom_label'}),null);assert.equal(screen.queryByLabelText('新列字段名'),null);
 fireEvent.click(screen.getByRole('button',{name:'添加条目'}));
 const associations=screen.getAllByLabelText(/对应需求$/) as HTMLInputElement[];assert.equal(associations.at(-1)?.value,'');assert.equal(associations.at(-1)?.placeholder,'N/A');
 fireEvent.change(screen.getByLabelText('条目 SC-1 custom_label'),{target:{value:'新的说明'}});
 fireEvent.click(screen.getByRole('button',{name:'保存新版本'}));await waitFor(()=>assert.ok(submitted()));
 assert.deepEqual(submitted().items[1].requirement_ids,[]);assert.deepEqual(submitted().items[1].refs,[]);assert.deepEqual(submitted().items[0].requirement_ids,['REQ-1']);assert.equal(submitted().items[0].custom_label,'新的说明');assert.equal(Object.hasOwn(submitted(),'column_changes'),false);
});

test('case table proposes explicit custom column additions and removals',async()=>{
 const data={...artifact,items:[{...original,custom_label:'remove'}]};const submitted=fixture(data);
 render(<ArtifactCard id="a1" snapshot={data} chatOnly onChanged={()=>{}} onTarget={()=>{}}/>);fireEvent.click(screen.getByRole('button',{name:'编辑',exact:true}));
 fireEvent.change(screen.getByLabelText('新列字段名'),{target:{value:'test_data'}});fireEvent.change(screen.getByLabelText('新列名称'),{target:{value:'测试数据'}});fireEvent.click(screen.getByRole('button',{name:'添加列',exact:true}));
 fireEvent.change(screen.getByLabelText('条目 TC-1 测试数据'),{target:{value:'有效账号'}});fireEvent.click(screen.getByRole('button',{name:'删除列 custom_label'}));
 fireEvent.click(screen.getByRole('button',{name:'保存新版本'}));await waitFor(()=>assert.ok(submitted()));
 assert.equal(submitted().items[0].test_data,'有效账号');assert.equal(Object.hasOwn(submitted().items[0],'custom_label'),false);assert.deepEqual(submitted().column_changes,{added:[{field:'test_data',header:'测试数据'}],removed:['custom_label']});
});

test('independent rows show N/A and are not classified as broken parent links',async()=>{
 const independent={...original,scenario_id:'',_independent_origin:{reason:'用户指定独立用例'}};
 const data={...artifact,items:[independent]};const workspace={lineage_rows:[{item_id:'TC-1',status:'independent',reason:'用户指定独立用例',requirements:[]}]};
 const context=rowContext(data,independent,workspace);assert.equal(context.missing,false);assert.equal(matchesScope(context,'unlinked','cases'),false);
 globalThis.fetch=async()=>json(workspace);render(<ArtifactCard id="a1" snapshot={data} chatOnly onChanged={()=>{}} onTarget={()=>{}}/>);
 await screen.findByText('场景 / 需求：N/A');assert.equal(screen.queryByText('尚未关联'),null);
});

test('historical table remains read-only and does not expose mutation actions',async()=>{
 fixture();render(<ArtifactCard id="a1" snapshot={artifact} readOnly chatOnly simplified onChanged={()=>assert.fail('history mutation')} onTarget={()=>assert.fail('history selection')}/>);
 assert.ok(screen.getByRole('table',{name:'用例与评审'}));assert.equal(screen.queryByRole('button',{name:'编辑',exact:true}),null);assert.equal(screen.queryByRole('button',{name:/让 AI 修改/}),null);assert.equal(screen.queryByLabelText('选择 TC-1'),null);
});

test('editing a partial viewer loads its complete immutable revision before saving',async()=>{
 const omitted={...original,id:'TC-2',title:'未显示用例'};const partial={...artifact,view_item_ids:['TC-1']};let submitted:any;const paths:string[]=[];
 globalThis.fetch=async(input:any,init:RequestInit={})=>{const path=String(input);paths.push(path);if(init.method==='PUT'){submitted=JSON.parse(String(init.body));return json({...artifact,revision:4,items:submitted.items});}if(path.includes('/revisions/3'))return json({...artifact,items:[original,omitted]});return json({lineage_rows:[]});};
 render(<ArtifactCard id="a1" snapshot={partial} chatOnly onChanged={()=>{}} onTarget={()=>{}}/>);
 fireEvent.click(screen.getByRole('button',{name:'编辑',exact:true}));await screen.findByLabelText('条目 TC-2 标题');
 fireEvent.change(screen.getByLabelText('条目 TC-1 标题'),{target:{value:'局部修改'}});fireEvent.click(screen.getByRole('button',{name:'保存新版本'}));
 await waitFor(()=>assert.ok(submitted));assert.equal(submitted.items.length,2);assert.equal(submitted.items[1].title,'未显示用例');assert.equal(submitted.expected_revision,3);assert.ok(paths.some(path=>path.endsWith('/revisions/3')));
});
