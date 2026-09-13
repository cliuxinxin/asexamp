import {JSDOM} from 'jsdom';
import {test,afterEach} from 'node:test';
import assert from 'node:assert/strict';
const dom=new JSDOM('<!doctype html><html><body></body></html>',{url:'http://localhost:8000/'});
for(const key of ['window','document','HTMLElement','HTMLDialogElement','MutationObserver','Node','Event','MouseEvent'])Object.defineProperty(globalThis,key,{value:(dom.window as any)[key],configurable:true,writable:true});
Object.defineProperty(globalThis,'navigator',{value:dom.window.navigator,configurable:true});
(globalThis as any).IS_REACT_ACT_ENVIRONMENT=true;
dom.window.HTMLDialogElement.prototype.showModal=function(){this.open=true;};dom.window.HTMLDialogElement.prototype.close=function(){this.open=false;};
const React=await import('react');
const {render,fireEvent,screen,within,waitFor,cleanup}=await import('@testing-library/react');
const {ArtifactCard}=await import('../src/ArtifactCard');
const originalFetch=globalThis.fetch;
afterEach(()=>{cleanup();globalThis.fetch=originalFetch;});
const requirement={id:'R-1',title:'登录与锁定',description:'连续五次失败锁定十分钟',refs:['source#P1']};
const scenario={id:'S-1',title:'失败后锁定账号',description:'第五次失败触发锁定',requirement_ids:['R-1'],priority:'P1',refs:['source#P1']};
const requirementParent={artifact_id:'analysis',revision:2,item:requirement,evidence:[{id:'source#P1',text:'连续五次失败后锁定账号十分钟。'}]};
const scenarioParent={artifact_id:'scenarios',revision:3,item:scenario,evidence:[]};
const caseItem={id:'C-1',title:'锁定边界',scenario_id:'S-1',priority:'P1',type:'Negative',preconditions:'账号已连续登录失败四次',steps:[{action:'第五次输入错误密码',expected:'账号锁定十分钟'},{action:'再次尝试登录',expected:'拒绝登录并提示锁定'}],refs:['source#P1'],actual_result:'人工记录：尚未执行'};
const cases={id:'cases',title:'登录用例',type:'cases',revision:4,items:[caseItem,{...caseItem,id:'C-2',title:'其他用例',scenario_id:'S-2'}],report:{lineage:{scenario_artifact_id:'scenarios',scenario_revision:3},review_reports:[{summary:'检查锁定边界',issues:[{case_id:'C-1',description:'补充锁定期间的拒绝验证',reason:'原预期未说明锁定期间行为',fields:['steps']}]}],template_usage:{field_contract:[{field:'actual_result',header:'实际结果',value_source:'manual'}]}}};
function fixture(artifact:any,workspace:any){const paths:string[]=[];globalThis.fetch=(async(input:any)=>{const path=String(input);paths.push(path);return new Response(JSON.stringify(path.includes('/workspace')?{artifact_id:artifact.id,revision:artifact.revision,...workspace}:artifact),{status:200,headers:{'Content-Type':'application/json'}});}) as typeof fetch;return paths;}
function linked(){return {lineage_rows:[{item_id:'C-1',scenario:scenarioParent,requirements:[requirementParent],status:'linked'},{item_id:'C-2',scenario:null,requirements:[],status:'missing_parent'}],coverage:{totals:{requirements:1,requirements_with_scenarios:1,scenarios:1,scenarios_with_cases:1,cases:2},scenarios:[{...scenario,case_ids:['C-1']}],requirements:[{...requirement,scenario_ids:['S-1'],case_ids:['C-1']}]}};}

test('scenario rows expose exact requirement content and evidence without a separate coverage table',async()=>{
 const artifact={id:'scenarios',title:'登录场景',type:'scenarios',revision:3,items:[scenario],report:{lineage:{analysis_artifact_id:'analysis',analysis_revision:2}}};
 const paths=fixture(artifact,{lineage_rows:[{item_id:'S-1',scenario:null,requirements:[requirementParent],status:'linked'}],coverage:{totals:{requirements:1,requirements_with_scenarios:1,scenarios:1,cases:0},scenarios:[{...scenario,case_ids:[]}],requirements:[{...requirement,scenario_ids:['S-1'],case_ids:[]}]}});
 render(<ArtifactCard id={artifact.id} snapshot={artifact} simplified onTarget={()=>{}} onChanged={()=>{}}/>);
 const row=screen.getByRole('checkbox',{name:'选择 S-1',exact:true}).closest('tr')!;
 await within(row).findByText('R-1');assert.ok(within(row).getByText('登录与锁定'));
 fireEvent.click(within(row).getByText('R-1'));assert.ok(within(row).getByText('连续五次失败锁定十分钟'));
 assert.ok(within(row).getByText('连续五次失败后锁定账号十分钟。'));assert.ok(within(row).getByText(/v2/));
 assert.equal(screen.queryByRole('button',{name:'查看需求 → 场景 → 用例覆盖'}),null);
 assert.equal(paths.filter(path=>path.includes('/workspace')).length,1);
});

test('collapsed case review keeps first step paired with expected result and expands complete steps',async()=>{
 fixture(cases,linked());render(<ArtifactCard id={cases.id} snapshot={cases} simplified reviewMode onTarget={()=>{}} onChanged={()=>{}}/>);
 const row=screen.getByRole('checkbox',{name:'选择 C-1',exact:true}).closest('tr')!;
 await within(row).findByText('S-1');assert.ok(within(row).getByText('R-1'));assert.ok(within(row).getByText('账号已连续登录失败四次'));
 assert.ok(within(row).getByText('第五次输入错误密码'));assert.ok(within(row).getByText('账号锁定十分钟'));assert.ok(within(row).getByText('共 2 步'));
 assert.ok(within(row).getByText('补充锁定期间的拒绝验证'));assert.ok(within(row).getByText('原预期未说明锁定期间行为'));
 fireEvent.click(within(row).getByRole('button',{name:'展开 C-1',exact:true}));
 const steps=screen.getByRole('table',{name:'C-1 步骤与预期结果'});
 const second=within(steps).getByText('再次尝试登录').closest('tr')!;assert.ok(within(second).getByText('拒绝登录并提示锁定'));
 fireEvent.click(screen.getByText('显示模板列'));fireEvent.click(screen.getByLabelText('显示 实际结果'));
 assert.ok(within(row).getByText('人工记录：尚未执行'));
});

test('inline filters preserve selected IDs and render missing lineage and pending upstream differences',async()=>{
 const workspace={...linked(),upstream_discrepancies:[{item_ids:['C-1'],source_ids:['extra'],refs:['extra#P1'],status:'pending',reason:'已有会话规则尚未写入上游',parents:[]}]};
 fixture(cases,workspace);let targeted:any;render(<ArtifactCard id={cases.id} snapshot={cases} simplified onTarget={(artifact,selected)=>{targeted={artifact,selected};}} onChanged={()=>{}}/>);
 await screen.findByText('上游待同步');fireEvent.click(screen.getByRole('checkbox',{name:'选择 C-1',exact:true}));
 fireEvent.click(screen.getByRole('button',{name:'未关联 1'}));assert.equal(screen.queryByRole('checkbox',{name:'选择 C-1',exact:true}),null);assert.ok(screen.getByRole('checkbox',{name:'选择 C-2',exact:true}));assert.ok(screen.getByText('尚未关联'));
 fireEvent.click(screen.getByRole('button',{name:'让 AI 修改选中 1 条'}));assert.deepEqual(targeted.selected,['C-1']);assert.equal(targeted.artifact.revision,4);
 assert.equal(screen.queryByText('已验证覆盖'),null);
});

test('historical rows fetch their immutable workspace revision and offer no write or selection entry',async()=>{
 const historical={...cases,revision:2,items:[caseItem]};const paths=fixture(historical,{...linked(),revision:2});
 const view=render(<ArtifactCard id={historical.id} snapshot={historical} readOnly simplified onTarget={()=>assert.fail('historical selection')} onChanged={()=>assert.fail('historical mutation')}/>);
 await screen.findByText('S-1');assert.ok(paths.some(path=>path.endsWith('/workspace?revision=2')));
 assert.equal(screen.queryByRole('checkbox',{name:'选择 C-1',exact:true}),null);assert.equal(screen.queryByRole('button',{name:/让 AI 修改/}),null);
 view.rerender(<ArtifactCard id={historical.id} snapshot={{...historical}} readOnly simplified refreshKey="refresh" onTarget={()=>{}} onChanged={()=>{}}/>);
 await waitFor(()=>assert.equal(paths.filter(path=>path.includes('/workspace')).length,1));
});

test('requirements without downstream rows remain visible and filter in the existing result',async()=>{
 const uncovered={id:'R-2',title:'会话撤销规则',description:'移除白名单后拒绝下一次请求',refs:[]};const artifact={id:'analysis',title:'需求理解',type:'analysis',revision:2,items:[requirement,uncovered],report:{}};
 fixture(artifact,{coverage:{totals:{requirements:2,requirements_with_scenarios:1},requirements:[{...requirement,scenario_ids:['S-1'],case_ids:['C-1']},{...uncovered,scenario_ids:[],case_ids:[]}],scenarios:[]},lineage_rows:[]});
 render(<ArtifactCard id={artifact.id} snapshot={artifact} simplified onTarget={()=>{}} onChanged={()=>{}}/>);
 fireEvent.click(await screen.findByRole('button',{name:'待补场景 1'}));assert.ok(screen.getByText('会话撤销规则'));assert.equal(screen.queryByText('登录与锁定'),null);
});

test('two cases sharing a scenario retain their separate synchronization status',async()=>{
 const artifact={...cases,items:[caseItem,{...caseItem,id:'C-2',title:'同场景另一用例'}]};
 fixture(artifact,{...linked(),stale:{changed_scenario_ids:['S-1']},lineage_rows:[{item_id:'C-1',scenario:scenarioParent,requirements:[requirementParent],status:'linked',stale:false},{item_id:'C-2',scenario:{...scenarioParent,revision:2},requirements:[requirementParent],status:'linked',stale:true}]});
 render(<ArtifactCard id={artifact.id} snapshot={artifact} simplified onTarget={()=>{}} onChanged={()=>{}}/>);
 const first=screen.getByRole('checkbox',{name:'选择 C-1',exact:true}).closest('tr')!;await within(first).findByText('S-1');
 assert.equal(within(first).queryByText('父级已更新 · 待同步')===null,true,'the already synchronized row must not inherit its sibling’s drift');
 const second=screen.getByRole('checkbox',{name:'选择 C-2',exact:true}).closest('tr')!;assert.ok(within(second).getByText('父级已更新 · 待同步'));
});

test('missing exact parents never inherit titles from a coincident business ID in coverage',async()=>{
 fixture(cases,{...linked(),lineage_rows:[{item_id:'C-1',scenario:null,requirements:[],status:'missing_parent'}],coverage:{totals:{},scenarios:[{id:'S-1',title:'另一分支的场景'}],requirements:[{id:'R-1',title:'另一分支的需求'}]}});
 render(<ArtifactCard id={cases.id} snapshot={cases} simplified onTarget={()=>{}} onChanged={()=>{}}/>);
 await screen.findByRole('button',{name:'未关联 2'});assert.equal(screen.queryByText('另一分支的场景'),null);assert.equal(screen.queryByText('另一分支的需求'),null);
});

test('review changes use server revision evidence and form edits retain protected execution fields',async()=>{
 const paths=fixture(cases,{...linked(),revision_diff:{added:[],updated:['C-2'],deleted:[]},review:{status:'stale',revision:3,changed_item_ids:['C-2']}});const editing:boolean[]=[];
 render(<ArtifactCard id={cases.id} snapshot={cases} simplified onEditingChange={value=>editing.push(value)} onTarget={()=>{}} onChanged={()=>{}}/>);
 fireEvent.click(await screen.findByRole('button',{name:'有变化 1'}));assert.ok(screen.getByText('修改后待复查'));assert.equal(screen.queryByRole('checkbox',{name:'选择 C-1',exact:true}),null);
 fireEvent.click(screen.getByRole('button',{name:'编辑',exact:true}));assert.equal(editing.at(-1),true);fireEvent.change(screen.getByLabelText('条目 C-2 标题'),{target:{value:'明确锁定边界'}});
 let saved:any;globalThis.fetch=(async(_input:any,options:any)=>{saved=JSON.parse(options.body);return new Response(JSON.stringify({...cases,revision:5,items:saved.items}),{headers:{'Content-Type':'application/json'}});}) as typeof fetch;
 fireEvent.click(screen.getByRole('button',{name:'保存新版本'}));await waitFor(()=>assert.equal(editing.at(-1),false));assert.equal(saved.expected_revision,4);assert.equal(saved.items[1].actual_result,'人工记录：尚未执行');assert.equal(saved.items[0].title,'锁定边界');assert.equal(paths.filter(path=>path.includes('/workspace')).length,1);
});

test('partially missing requirements are explicit and never described as an upstream edit',async()=>{
 const item={...scenario,requirement_ids:['R-1','R-MISSING']};const artifact={id:'scenarios',title:'登录场景',type:'scenarios',revision:3,items:[item]};
 fixture(artifact,{lineage_rows:[{item_id:'S-1',scenario:null,requirements:[requirementParent],status:'missing_parent',stale:true}],coverage:{totals:{},scenarios:[{...item,case_ids:[]}]}});
 render(<ArtifactCard id={artifact.id} snapshot={artifact} simplified onTarget={()=>{}} onChanged={()=>{}}/>);
 await screen.findByText('R-1');assert.equal(screen.queryByText('关联不完整')!==null,true);assert.equal(screen.queryByText('父级已更新 · 待同步')===null,true);assert.ok(screen.getByRole('button',{name:'未关联需求 1'}));
});

test('selection and filtered visible order notify the conversation without requiring the AI button',async()=>{
 fixture(cases,linked());let selectedTarget:any;let focused:any;
 render(<ArtifactCard id={cases.id} snapshot={cases} simplified onSelectionChange={(artifact,selected,viewOrder)=>{selectedTarget={artifact,selected,viewOrder};}} onTarget={(artifact,selected,viewOrder)=>{focused={artifact,selected,viewOrder};}} onChanged={()=>{}}/>);
 await screen.findByRole('button',{name:'未关联 1'});fireEvent.click(screen.getByRole('checkbox',{name:'选择 C-2',exact:true}));
 assert.deepEqual(selectedTarget?.selected,['C-2']);assert.equal(selectedTarget.artifact.revision,4);assert.equal(focused,undefined);
 fireEvent.click(screen.getByRole('button',{name:'未关联 1'}));assert.deepEqual(selectedTarget.viewOrder,['C-2']);
 fireEvent.click(screen.getByRole('button',{name:'让 AI 修改选中 1 条'}));assert.deepEqual(focused.viewOrder,['C-2']);
});

test('entering focus shows every step after a prior explicit collapse and keeps selection',async()=>{
 fixture(cases,linked());const props={id:cases.id,snapshot:cases,simplified:true,onTarget:()=>{},onChanged:()=>{}};
 const view=render(<ArtifactCard {...props}/>);await screen.findByText('S-1');fireEvent.click(screen.getByRole('checkbox',{name:'选择 C-1',exact:true}));
 fireEvent.click(screen.getByRole('button',{name:'展开全部步骤'}));fireEvent.click(screen.getByRole('button',{name:'收起全部步骤'}));
 view.rerender(<ArtifactCard {...props} initialDetailsOpen/>);
 assert.equal(screen.queryByRole('table',{name:'C-1 步骤与预期结果'})!==null,true);assert.equal((screen.getByRole('checkbox',{name:'选择 C-1',exact:true}) as HTMLInputElement).checked,true);
});

test('saved review text accepts a textual modification scope as well as field arrays',async()=>{
 const artifact={...cases,report:{review_reports:[{issues:[{case_id:'C-1',description:'补充时间边界',fields:'第二步的预期结果',refs:'source#P1'}]}]}};
 fixture(artifact,linked());render(<ArtifactCard id={artifact.id} snapshot={artifact} simplified onTarget={()=>{}} onChanged={()=>{}}/>);
 const row=screen.getByRole('checkbox',{name:'选择 C-1',exact:true}).closest('tr')!;assert.ok(within(row).getByText('第二步的预期结果'));
});
