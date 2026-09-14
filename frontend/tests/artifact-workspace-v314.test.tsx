import {JSDOM} from 'jsdom';
import {afterEach,test} from 'node:test';
import assert from 'node:assert/strict';

const dom=new JSDOM('<!doctype html><html><body></body></html>',{url:'http://localhost/'});
for(const key of ['window','document','HTMLElement','MutationObserver','Node','Event','MouseEvent','KeyboardEvent'])Object.defineProperty(globalThis,key,{value:(dom.window as any)[key],configurable:true,writable:true});
Object.defineProperty(globalThis,'navigator',{value:dom.window.navigator,configurable:true});
(globalThis as any).IS_REACT_ACT_ENVIRONMENT=true;
const React=await import('react');
const {render,fireEvent,screen,waitFor,cleanup,within}=await import('@testing-library/react');
const {ArtifactWorkspace}=await import('../src/ArtifactWorkspace');

afterEach(cleanup);
const json=(value:unknown,status=200)=>new Response(JSON.stringify(value),{status,headers:{'Content-Type':'application/json'}});
const columns=[
 {field:'id',header:'场景编号',editable:false},
 {field:'title',header:'标题'},
 {field:'description',header:'说明'},
 {field:'requirement_ids',header:'对应需求'},
 {field:'risk',header:'风险说明'},
];
const item={id:'SC-1',title:'普通登录',description:'有效账号登录',requirement_ids:['REQ-1'],risk:{level:'medium'},refs:['source#1']};
const row=(value:any)=>({item_id:value.id,step_index:null,cells:columns.map(column=>typeof value[column.field]==='object'?JSON.stringify(value[column.field]):String(value[column.field]??''))});
const base={artifact_id:'scenarios',artifact_type:'scenarios',title:'登录场景',artifact_revision:2,mode:'manual',layout:'case',columns,original_items:[item],proposed_items:[item],original_rows:[row(item)],proposed_rows:[row(item)],issues:[],read_only:false};

function fixture(view:any=base){
 const writes:any[]=[];const paths:string[]=[];
 globalThis.fetch=(async(input:any,init:any={})=>{
  const url=new URL(String(input),'http://localhost');const path=url.pathname.replace(/^\/api/,'');paths.push(path+url.search);
  if(path.endsWith('/workspace-grid/project')){const body=JSON.parse(init.body);return json({columns,rows:body.items.map(row)});}
  if(path.endsWith('/workspace-grid/save')){writes.push(JSON.parse(init.body));return json({message:'已保存'});}
  if(path.endsWith('/workspace-grid/export'))return new Response(new Blob(['xlsx']),{headers:{'Content-Disposition':"attachment; filename*=UTF-8''scenarios.xlsx"}});
  if(path.endsWith('/workspace-grid'))return json(view);
  throw new Error('Unexpected '+path);
 }) as typeof fetch;
 return {writes,paths};
}

const props={request:{artifactId:'scenarios'},chatId:'chat',messages:[],onClose:()=>{},onSaved:()=>{},onSend:async()=>{}};

test('scenario workspace edits structured cells and can add and delete rows before one save',async()=>{
 const second={...item,id:'SC-2',title:'验证码登录'};const view={...base,original_items:[item,second],proposed_items:[item,second],original_rows:[row(item),row(second)],proposed_rows:[row(item),row(second)]};
 const request=fixture(view);render(<ArtifactWorkspace {...props}/>);
 const grid=await screen.findByRole('table',{name:'测试场景工作表'});
 fireEvent.doubleClick(within(grid).getByRole('button',{name:'SC-1 标题'}));
 const title=screen.getByLabelText('编辑 标题');fireEvent.change(title,{target:{value:'并发登录'}});fireEvent.click(screen.getByRole('button',{name:'保存单元格'}));
 fireEvent.keyDown(within(grid).getByRole('button',{name:'SC-1 风险说明'}),{key:'F2'});
 const risk=screen.getByLabelText('编辑 风险说明');fireEvent.change(risk,{target:{value:'{"level":"high","reason":"burst"}'}});fireEvent.click(screen.getByRole('button',{name:'保存单元格'}));
 fireEvent.click(screen.getByRole('button',{name:'添加场景'}));
 await waitFor(()=>assert.ok(screen.getByText('SC-NEW-1')));
 fireEvent.click(screen.getByRole('button',{name:'删除条目 SC-NEW-1'}));
 fireEvent.click(screen.getByRole('button',{name:'删除条目 SC-2'}));
 const save=screen.getByRole('button',{name:'保存更改'}) as HTMLButtonElement;
 await waitFor(()=>assert.equal(save.disabled,false));fireEvent.click(save);
 await waitFor(()=>assert.equal(request.writes.length,1));
 assert.equal(request.writes[0].expected_revision,2);
 assert.equal(request.writes[0].items.length,1);
 assert.equal(request.writes[0].items[0].title,'并发登录');
 assert.deepEqual(request.writes[0].items[0].risk,{level:'high',reason:'burst'});
 assert.deepEqual(request.writes[0].items[0].refs,['source#1']);
});

test('historical workspace is immutable and exposes export as its only artifact action',async()=>{
 const request=fixture({...base,mode:'read_only',read_only:true});
 render(<ArtifactWorkspace {...props} request={{artifactId:'scenarios',revision:1,readOnly:true}}/>);
 const dialog=await screen.findByRole('dialog',{name:'成果工作区'});
 await within(dialog).findByRole('table');
 assert.ok(within(dialog).getByRole('button',{name:'导出 Excel'}));
 assert.equal(within(dialog).queryByRole('button',{name:'添加场景'}),null);
 assert.equal(within(dialog).queryByRole('button',{name:'保存更改'}),null);
 assert.equal(within(dialog).queryByRole('button',{name:/助手/}),null);
 assert.equal(within(dialog).queryByRole('checkbox'),null);
 assert.ok(request.paths.some(path=>path==='/artifacts/scenarios/workspace-grid?revision=1'));
 assert.equal(request.paths.some(path=>path.endsWith('/save')||path.endsWith('/project')),false);
});

test('case workspace keeps steps paired and submits custom column additions and removals',async()=>{
 const caseColumns=[{field:'id',header:'编号',editable:false},{field:'title',header:'标题'},{field:'steps',header:'操作步骤'},{field:'expected',header:'预期结果'},{field:'legacy_data',header:'旧数据'}];
 const before={id:'TC-1',title:'登录',description:'验证登录',scenario_id:'SC-1',type:'Business',priority:'P1',preconditions:'已注册',steps:[{action:'点击登录',expected:'进入首页',automation:'keep'}],refs:['source#1'],legacy_data:'旧值'};
 const rows=(items:any[],cols=caseColumns)=>items.map(value=>({item_id:value.id,step_index:null,cells:cols.map(column=>column.field==='steps'?value.steps.map((step:any)=>step.action).join('\n'):column.field==='expected'?value.steps.map((step:any)=>step.expected).join('\n'):String(value[column.field]??''))}));
 const view={artifact_id:'cases',artifact_type:'cases',title:'登录用例',artifact_revision:4,mode:'manual',layout:'case',columns:caseColumns,original_items:[before],proposed_items:[before],original_rows:rows([before]),proposed_rows:rows([before]),issues:[],read_only:false};
 const writes:any[]=[];
 globalThis.fetch=(async(input:any,init:any={})=>{const path=new URL(String(input),'http://localhost').pathname.replace(/^\/api/,'');
  if(path.endsWith('/workspace-grid/project')){const body=JSON.parse(init.body);let next=caseColumns.filter(column=>!body.column_changes?.removed?.includes(column.field));next=[...next,...(body.column_changes?.added??[])];return json({columns:next,rows:rows(body.items,next)});}
  if(path.endsWith('/workspace-grid/save')){writes.push(JSON.parse(init.body));return json({message:'已保存'});}
  if(path.endsWith('/workspace-grid'))return json(view);throw new Error('Unexpected '+path);
 }) as typeof fetch;
 render(<ArtifactWorkspace {...props} request={{artifactId:'cases'}}/>);await screen.findByRole('table',{name:'测试用例工作表'});
 fireEvent.doubleClick(screen.getByRole('button',{name:'TC-1 操作步骤'}));
 fireEvent.change(screen.getByLabelText('第 1 步操作'),{target:{value:'输入账号并点击登录'}});fireEvent.change(screen.getByLabelText('第 1 步预期结果'),{target:{value:'显示用户首页'}});fireEvent.click(screen.getByRole('button',{name:'保存单元格'}));
 fireEvent.click(screen.getByRole('button',{name:'管理自定义列'}));fireEvent.change(screen.getByLabelText('新列字段名'),{target:{value:'test_data'}});fireEvent.change(screen.getByLabelText('新列名称'),{target:{value:'测试数据'}});fireEvent.click(screen.getByRole('button',{name:'添加列'}));
 await screen.findByRole('columnheader',{name:'测试数据'});fireEvent.click(screen.getByRole('button',{name:'管理自定义列'}));fireEvent.click(screen.getByRole('button',{name:'删除列 旧数据'}));
 const save=screen.getByRole('button',{name:'保存更改'}) as HTMLButtonElement;await waitFor(()=>assert.equal(save.disabled,false));fireEvent.click(save);await waitFor(()=>assert.equal(writes.length,1));
 assert.deepEqual(writes[0].items[0].steps,[{action:'输入账号并点击登录',expected:'显示用户首页',automation:'keep'}]);
 assert.deepEqual(writes[0].column_changes,{added:[{field:'test_data',header:'测试数据'}],removed:['legacy_data']});
 assert.equal(Object.hasOwn(writes[0].items[0],'legacy_data'),false);
});

test('reset after removing a middle custom column restores coherent headers and cell values',async()=>{
 const caseColumns=[{field:'id',header:'编号',editable:false},{field:'title',header:'标题'},{field:'legacy_data',header:'旧数据'},{field:'description',header:'说明'}];
 const value={id:'TC-1',title:'登录',legacy_data:'Legacy value',description:'真实说明',scenario_id:'SC-1',type:'Business',priority:'P1',preconditions:'已注册',steps:[{action:'登录',expected:'成功'}],refs:[]};
 const rows=(items:any[],cols=caseColumns)=>items.map(item=>({item_id:item.id,step_index:null,cells:cols.map(column=>String(item[column.field]??''))}));
 const view={artifact_id:'cases',artifact_type:'cases',title:'登录用例',artifact_revision:4,mode:'manual',layout:'case',columns:caseColumns,original_items:[value],proposed_items:[value],original_rows:rows([value]),proposed_rows:rows([value]),issues:[],read_only:false};
 globalThis.fetch=(async(input:any,init:any={})=>{const path=new URL(String(input),'http://localhost').pathname.replace(/^\/api/,'');if(path.endsWith('/workspace-grid/project')){const body=JSON.parse(init.body);const next=caseColumns.filter(column=>!body.column_changes?.removed?.includes(column.field));return json({columns:next,rows:rows(body.items,next)});}if(path.endsWith('/workspace-grid'))return json(view);throw new Error('Unexpected '+path);}) as typeof fetch;
 render(<ArtifactWorkspace {...props} request={{artifactId:'cases'}}/>);await screen.findByRole('table',{name:'测试用例工作表'});
 fireEvent.click(screen.getByRole('button',{name:'管理自定义列'}));fireEvent.click(screen.getByRole('button',{name:'删除列 旧数据'}));await waitFor(()=>assert.equal(screen.queryByRole('columnheader',{name:'旧数据'}),null));
 fireEvent.click(screen.getByRole('button',{name:'撤销本地更改'}));
 await screen.findByRole('columnheader',{name:'旧数据'});assert.equal(screen.getByRole('button',{name:'TC-1 标题'}).textContent,'登录');assert.equal(screen.getByRole('button',{name:'TC-1 旧数据'}).textContent,'Legacy value');assert.equal(screen.getByRole('button',{name:'TC-1 说明'}).textContent,'真实说明');
});

test('layout switching after a custom-column removal keeps immutable baseline projections coherent on reset',async()=>{
 const caseColumns=[{field:'id',header:'编号',editable:false},{field:'title',header:'标题'},{field:'legacy_data',header:'旧数据'},{field:'description',header:'说明'}];
 const value={id:'TC-1',title:'登录',legacy_data:'Legacy value',description:'真实说明',scenario_id:'SC-1',type:'Business',priority:'P1',preconditions:'已注册',steps:[{action:'登录',expected:'成功'}],refs:[]};
 const rows=(items:any[],cols=caseColumns)=>items.map(item=>({item_id:item.id,step_index:null,cells:cols.map(column=>String(item[column.field]??''))}));
 const view={artifact_id:'cases',artifact_type:'cases',title:'登录用例',artifact_revision:4,mode:'manual',layout:'case',columns:caseColumns,original_items:[value],proposed_items:[value],original_rows:rows([value]),proposed_rows:rows([value]),issues:[],read_only:false};
 const projections:any[]=[];globalThis.fetch=(async(input:any,init:any={})=>{const path=new URL(String(input),'http://localhost').pathname.replace(/^\/api/,'');if(path.endsWith('/workspace-grid/project')){const body=JSON.parse(init.body);projections.push(body);const next=caseColumns.filter(column=>!body.column_changes?.removed?.includes(column.field));return json({columns:next,rows:rows(body.items,next)});}if(path.endsWith('/workspace-grid'))return json(view);throw new Error('Unexpected '+path);}) as typeof fetch;
 render(<ArtifactWorkspace {...props} request={{artifactId:'cases'}}/>);await screen.findByRole('table',{name:'测试用例工作表'});
 fireEvent.click(screen.getByRole('button',{name:'管理自定义列'}));fireEvent.click(screen.getByRole('button',{name:'删除列 旧数据'}));await waitFor(()=>assert.equal(screen.queryByRole('columnheader',{name:'旧数据'}),null));
 fireEvent.change(screen.getByLabelText('表格与 Excel 布局'),{target:{value:'step'}});await waitFor(()=>assert.equal(projections.filter(body=>body.layout==='step').length,3));const baselineProjections=projections.filter(body=>body.layout==='step'&&body.items[0].legacy_data==='Legacy value');assert.equal(baselineProjections.length,2);assert.ok(baselineProjections.every(body=>!body.column_changes));
 fireEvent.click(screen.getByRole('button',{name:'撤销本地更改'}));
 await screen.findByRole('columnheader',{name:'旧数据'});assert.equal(screen.getByRole('button',{name:/TC-1 标题/}).textContent,'登录');assert.equal(screen.getByRole('button',{name:/TC-1 旧数据/}).textContent,'Legacy value');assert.equal(screen.getByRole('button',{name:/TC-1 说明/}).textContent,'真实说明');
});

test('analysis workspace saves report summary and diagram source with item edits',async()=>{
 const analysisColumns=[{field:'id',header:'需求编号',editable:false},{field:'title',header:'标题'},{field:'description',header:'说明'}];
 const requirement={id:'REQ-1',title:'登录',description:'用户可以登录',refs:['source#1']};
 const analysis={artifact_id:'analysis',artifact_type:'analysis',title:'需求理解',artifact_revision:3,mode:'manual',layout:'case',columns:analysisColumns,original_items:[requirement],proposed_items:[requirement],original_rows:[{item_id:'REQ-1',step_index:null,cells:['REQ-1','登录','用户可以登录']}],proposed_rows:[{item_id:'REQ-1',step_index:null,cells:['REQ-1','登录','用户可以登录']}],report:{summary:'原摘要',diagrams:[{title:'登录流程',mermaid:'flowchart LR\nA-->B'}]},issues:[],read_only:false};
 const writes:any[]=[];globalThis.fetch=(async(input:any,init:any={})=>{const path=new URL(String(input),'http://localhost').pathname.replace(/^\/api/,'');if(path.endsWith('/workspace-grid/save')){writes.push(JSON.parse(init.body));return json({message:'已保存'});}if(path.endsWith('/workspace-grid'))return json(analysis);throw new Error('Unexpected '+path);}) as typeof fetch;
 render(<ArtifactWorkspace {...props} request={{artifactId:'analysis'}}/>);await screen.findByRole('table',{name:'需求理解工作表'});
 fireEvent.change(screen.getByLabelText('需求理解摘要'),{target:{value:'人工核对后的摘要'}});fireEvent.change(screen.getByLabelText('图形源码 1'),{target:{value:'flowchart LR\nA-->C'}});
 fireEvent.click(screen.getByRole('button',{name:'保存更改'}));await waitFor(()=>assert.equal(writes.length,1));
 assert.equal(writes[0].report.summary,'人工核对后的摘要');assert.equal(writes[0].report.diagrams[0].mermaid,'flowchart LR\nA-->C');assert.deepEqual(writes[0].items,[requirement]);
});

test('historical analysis exposes its supported workspace export without mutation controls',async()=>{
 const view={...base,artifact_type:'analysis',title:'需求理解',mode:'read_only',read_only:true};fixture(view);
 render(<ArtifactWorkspace {...props} request={{artifactId:'analysis',revision:1,readOnly:true}}/>);const dialog=await screen.findByRole('dialog',{name:'成果工作区'});await within(dialog).findByRole('table',{name:'需求理解工作表'});
 assert.ok(within(dialog).getByRole('button',{name:'导出 Excel'}));assert.equal(within(dialog).queryByRole('button',{name:'保存更改'}),null);assert.equal(within(dialog).queryByRole('button',{name:/助手/}),null);
});

test('AI proposal keeps per-cell accept and reject decisions in one saved draft',async()=>{
 const proposed={...item,title:'并发登录',description:'两个设备同时登录'};
 const view={...base,mode:'ai_proposal',proposal_id:'proposal-1',prompt_id:'prompt-1',original_items:[item],proposed_items:[proposed],original_rows:[row(item)],proposed_rows:[row(proposed)]};
 const writes:any[]=[],assistantCalls:any[]=[];globalThis.fetch=(async(input:any,init:any={})=>{const path=new URL(String(input),'http://localhost').pathname.replace(/^\/api/,'');if(path.endsWith('/workspace-grid/project')){const body=JSON.parse(init.body);return json({columns,rows:body.items.map(row)});}if(path.endsWith('/workspace-grid/save')){writes.push(JSON.parse(init.body));return json({message:'已保存'});}if(path.endsWith('/workspace-grid'))return json(view);throw new Error('Unexpected '+path);}) as typeof fetch;
 render(<ArtifactWorkspace {...props} onSend={async(...args)=>{assistantCalls.push(args);}} request={{artifactId:'scenarios',proposalId:'proposal-1',promptId:'prompt-1'}}/>);const grid=await screen.findByRole('table',{name:'测试场景工作表'});
 fireEvent.click(within(grid).getByRole('checkbox',{name:'选择 SC-1'}));fireEvent.change(screen.getByLabelText('成果对话输入'),{target:{value:'解释选中的场景'}});fireEvent.click(screen.getByRole('button',{name:'发送成果消息'}));await waitFor(()=>assert.equal(assistantCalls.length,1));
 assert.deepEqual(assistantCalls[0],['解释选中的场景',['SC-1'],{artifactRevision:2,proposalId:'proposal-1',promptId:'prompt-1'}]);
 fireEvent.click(within(grid).getByRole('button',{name:'SC-1 标题'}));fireEvent.click(screen.getByRole('button',{name:'接受修改'}));
 fireEvent.click(within(grid).getByRole('button',{name:'SC-1 说明'}));fireEvent.click(screen.getByRole('button',{name:'拒绝修改'}));
 const save=screen.getByRole('button',{name:'保存并完成确认'}) as HTMLButtonElement;await waitFor(()=>assert.equal(save.disabled,false));fireEvent.click(save);await waitFor(()=>assert.equal(writes.length,1));
 assert.equal(writes[0].items[0].title,'并发登录');assert.equal(writes[0].items[0].description,'有效账号登录');assert.equal(writes[0].proposal_id,'proposal-1');assert.equal(writes[0].prompt_id,'prompt-1');
});

test('rejecting every AI cell omits an untouched proposal report so the gateway rejects without a revision',async()=>{
 const proposed={...item,title:'并发登录'};const view={...base,mode:'ai_proposal',proposal_id:'proposal-2',prompt_id:'prompt-2',report:{summary:'AI 建议摘要'},original_items:[item],proposed_items:[proposed],original_rows:[row(item)],proposed_rows:[row(proposed)]};
 const writes:any[]=[];globalThis.fetch=(async(input:any,init:any={})=>{const path=new URL(String(input),'http://localhost').pathname.replace(/^\/api/,'');if(path.endsWith('/workspace-grid/project')){const body=JSON.parse(init.body);return json({columns,rows:body.items.map(row)});}if(path.endsWith('/workspace-grid/save')){writes.push(JSON.parse(init.body));return json({message:'已拒绝',rejected:true});}if(path.endsWith('/workspace-grid'))return json(view);throw new Error('Unexpected '+path);}) as typeof fetch;
 render(<ArtifactWorkspace {...props} request={{artifactId:'scenarios',proposalId:'proposal-2'}}/>);const grid=await screen.findByRole('table',{name:'测试场景工作表'});
 fireEvent.click(within(grid).getByRole('button',{name:'SC-1 标题'}));fireEvent.click(screen.getByRole('button',{name:'拒绝修改'}));
 const save=screen.getByRole('button',{name:'保存并完成确认'}) as HTMLButtonElement;await waitFor(()=>assert.equal(save.disabled,false));fireEvent.click(save);await waitFor(()=>assert.equal(writes.length,1));
 assert.deepEqual(writes[0].items,[item]);assert.equal(Object.hasOwn(writes[0],'report'),false);assert.equal(Object.hasOwn(writes[0],'column_changes'),false);
});

test('manual rows survive later cell decisions inside an AI proposal draft',async()=>{
 const second={...item,id:'SC-2',title:'验证码登录'},proposed={...item,title:'并发登录'};const view={...base,mode:'ai_proposal',proposal_id:'proposal-3',prompt_id:'prompt-3',original_items:[item,second],proposed_items:[proposed,second],original_rows:[row(item),row(second)],proposed_rows:[row(proposed),row(second)]};
 const writes:any[]=[];globalThis.fetch=(async(input:any,init:any={})=>{const path=new URL(String(input),'http://localhost').pathname.replace(/^\/api/,'');if(path.endsWith('/workspace-grid/project')){const body=JSON.parse(init.body);return json({columns,rows:body.items.map(row)});}if(path.endsWith('/workspace-grid/save')){writes.push(JSON.parse(init.body));return json({message:'已保存'});}if(path.endsWith('/workspace-grid'))return json(view);throw new Error('Unexpected '+path);}) as typeof fetch;
 render(<ArtifactWorkspace {...props} request={{artifactId:'scenarios',proposalId:'proposal-3'}}/>);const grid=await screen.findByRole('table',{name:'测试场景工作表'});
 fireEvent.click(screen.getByRole('button',{name:'添加场景'}));await screen.findByText('SC-NEW-1');
 fireEvent.click(screen.getByRole('button',{name:'删除条目 SC-2'}));
 fireEvent.click(within(grid).getByRole('button',{name:'SC-1 标题'}));fireEvent.click(screen.getByRole('button',{name:'拒绝修改'}));
 const save=screen.getByRole('button',{name:'保存并完成确认'}) as HTMLButtonElement;await waitFor(()=>assert.equal(save.disabled,false));fireEvent.click(save);await waitFor(()=>assert.equal(writes.length,1));
 assert.deepEqual(writes[0].items.map((value:any)=>value.id),['SC-1','SC-NEW-1']);assert.equal(writes[0].items[0].title,'普通登录');
});

test('metadata-only AI proposal can be explicitly rejected through the save gateway',async()=>{
 const view={...base,mode:'ai_proposal',proposal_id:'proposal-metadata',prompt_id:'prompt-metadata',report:{summary:'AI 只修改了报告'},original_items:[item],proposed_items:[item],original_rows:[row(item)],proposed_rows:[row(item)]};
 const writes:any[]=[];globalThis.fetch=(async(input:any,init:any={})=>{const path=new URL(String(input),'http://localhost').pathname.replace(/^\/api/,'');if(path.endsWith('/workspace-grid/project')){const body=JSON.parse(init.body);return json({columns,rows:body.items.map(row)});}if(path.endsWith('/workspace-grid/save')){writes.push(JSON.parse(init.body));return json({message:'已拒绝',rejected:true});}if(path.endsWith('/workspace-grid'))return json(view);throw new Error('Unexpected '+path);}) as typeof fetch;
 render(<ArtifactWorkspace {...props} request={{artifactId:'scenarios',proposalId:'proposal-metadata'}}/>);const grid=await screen.findByRole('table',{name:'测试场景工作表'});
 fireEvent.doubleClick(within(grid).getByRole('button',{name:'SC-1 标题'}));let reject=screen.getByRole('button',{name:'拒绝此建议'}) as HTMLButtonElement;assert.equal(reject.disabled,true);assert.ok(screen.getByText('请先保存或撤销本地更改，再拒绝整份建议。'));fireEvent.click(screen.getByRole('button',{name:'取消'}));
 fireEvent.click(screen.getByRole('button',{name:'添加场景'}));await screen.findByText('SC-NEW-1');reject=screen.getByRole('button',{name:'拒绝此建议'}) as HTMLButtonElement;assert.equal(reject.disabled,true);fireEvent.click(screen.getByRole('button',{name:'撤销本地更改'}));await waitFor(()=>assert.equal(screen.queryByText('SC-NEW-1'),null));reject=screen.getByRole('button',{name:'拒绝此建议'}) as HTMLButtonElement;assert.equal(reject.disabled,false);
 fireEvent.click(reject);await waitFor(()=>assert.equal(writes.length,1));
 assert.equal(writes[0].proposal_decision,'reject');assert.deepEqual(writes[0].items,[item]);assert.equal(Object.hasOwn(writes[0],'report'),false);assert.equal(Object.hasOwn(writes[0],'column_changes'),false);assert.equal(writes[0].proposal_id,'proposal-metadata');
});
