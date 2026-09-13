import {JSDOM} from 'jsdom';
import {test} from 'node:test';
import assert from 'node:assert/strict';
const dom=new JSDOM('<!doctype html><html><body></body></html>',{url:'http://localhost:8000/'});
for(const key of ['window','document','HTMLElement','HTMLDialogElement','MutationObserver','Node','Event','MouseEvent'])Object.defineProperty(globalThis,key,{value:(dom.window as any)[key],configurable:true,writable:true});
Object.defineProperty(globalThis,'navigator',{value:dom.window.navigator,configurable:true});
(globalThis as any).IS_REACT_ACT_ENVIRONMENT=true;
dom.window.HTMLDialogElement.prototype.showModal=function(){this.open=true;};dom.window.HTMLDialogElement.prototype.close=function(){this.open=false;};
const React=await import('react');const {render,fireEvent,screen,waitFor,cleanup}=await import('@testing-library/react');
const {ArtifactActions}=await import('../src/ArtifactActions');const {WorkspaceCoverage}=await import('../src/WorkspaceCoverage');
const cases={id:'case-art',type:'cases',title:'登录测试用例',revision:2,items:[{id:'C1',title:'成功登录',scenario_id:'S1',type:'Business',priority:'P1',description:'验证有效账号登录',preconditions:'账号处于启用状态',steps:[{action:'输入密码',expected:'显示首页'}],refs:['src#P1'],executor:'Alice'}]};
const scenario={id:'scenario-art',type:'scenarios',title:'登录场景',revision:3,items:[{id:'S1',title:'成功登录',description:'登录功能',refs:['src#P1']}]};
function json(value:unknown){return new Response(JSON.stringify(value),{status:200,headers:{'Content-Type':'application/json'}});}

test('microtuning previews actual before/after steps and only applies on confirmation',async()=>{
 const original=globalThis.fetch;const requests:any[]=[];let changed=0;
 globalThis.fetch=(async(url:any,init:any)=>{const path=String(url),body=init?.body?JSON.parse(init.body):undefined;requests.push({path,body});if(path.endsWith('/preview'))return json({id:'p1',summary:'将操作细化为有效账号和密码。',changes:[{artifact_id:cases.id,title:cases.title,expected_revision:2,items:[{...cases.items[0],steps:[{action:'输入有效账号和密码并点击登录',expected:'跳转首页且显示账号名称'}]}]}]});if(path.endsWith('/apply'))return json({artifacts:[{...cases,revision:3}],summary:'已保存登录用例新版本'});throw new Error(path);}) as typeof fetch;
 try{render(<ArtifactActions artifact={cases} selected={['C1']} onChanged={()=>changed++}/>);fireEvent.click(screen.getByRole('button',{name:'AI 微调选中 1 条'}));fireEvent.click(screen.getByRole('button',{name:'步骤更清楚'}));fireEvent.click(screen.getByRole('button',{name:'预览修改'}));await screen.findByText('输入有效账号和密码并点击登录');assert.ok(screen.getByText('输入密码'));assert.ok(screen.getByText('显示首页'));assert.ok(screen.getByText('跳转首页且显示账号名称'));assert.equal(requests.length,1);assert.deepEqual(requests[0].body.selected_ids,['C1']);assert.equal(changed,0);fireEvent.click(screen.getByRole('button',{name:'确认应用修改'}));await screen.findByText('已保存登录用例新版本');assert.equal(changed,1);assert.deepEqual(requests[1].body,{proposal_id:'p1'});assert.equal(screen.queryByRole('button',{name:'确认应用修改'}),null);}finally{cleanup();globalThis.fetch=original;}
});

test('scenario estimation is read only and omits empty selected scope',async()=>{
 const original=globalThis.fetch;let request:any;
 globalThis.fetch=(async(url:any,init:any)=>{request={path:String(url),body:JSON.parse(init.body)};return json({id:'est1',action:'estimate',summary:'按正常与异常分支估算',changes:[],estimate:{min_count:2,max_count:4,scenarios:[{scenario_id:'S1',min_count:2,max_count:4,rationale:'正常登录与凭证错误',assumptions:['账号锁定规则尚待确认']}]}});}) as typeof fetch;
 try{render(<ArtifactActions artifact={scenario} selected={[]} onChanged={()=>assert.fail('read-only action changed data')}/>);fireEvent.click(screen.getByRole('button',{name:'估算用例数量（不生成）'}));fireEvent.click(screen.getByRole('button',{name:'开始估算'}));await screen.findByText('预计 2–4 条用例');assert.equal(request.body.action,'estimate');assert.equal('selected_ids' in request.body,false);assert.ok(screen.getByText('账号锁定规则尚待确认'));assert.equal(screen.queryByRole('button',{name:'确认应用修改'}),null);}finally{cleanup();globalThis.fetch=original;}
});

test('case explanations retain selected scope and offer no apply action',async()=>{
 const original=globalThis.fetch;let request:any;
 globalThis.fetch=(async(_url:any,init:any)=>{request=JSON.parse(init.body);return json({id:'ex1',answer:'C1 验证启用账号登录后的首页结果。',changes:[]});}) as typeof fetch;
 try{render(<ArtifactActions artifact={cases} selected={['C1']} onChanged={()=>assert.fail('read-only action changed data')}/>);fireEvent.click(screen.getByRole('button',{name:'解释选中用例'}));fireEvent.click(screen.getByRole('button',{name:'生成说明'}));await screen.findByText('C1 验证启用账号登录后的首页结果。');assert.deepEqual(request.selected_ids,['C1']);assert.equal(request.action,'explain');assert.equal(screen.queryByRole('button',{name:'确认应用修改'}),null);}finally{cleanup();globalThis.fetch=original;}
});

test('sync selects linked cases by default and requires explicit choice for legacy artifacts',async()=>{
 const original=globalThis.fetch;let requested:any;
 globalThis.fetch=(async(url:any,init:any)=>{if(String(url).endsWith('/workspace'))return json({related_artifacts:[{id:'case-art',type:'cases',title:'登录测试用例',revision:2,linked:true},{id:'old-art',type:'cases',title:'旧版导入用例',revision:1,linked:false}]});requested=JSON.parse(init.body);return json({id:'sync1',changes:[],summary:'检查完成'});}) as typeof fetch;
 try{render(<ArtifactActions artifact={scenario} selected={['S1']} onChanged={()=>{}}/>);fireEvent.click(screen.getByRole('button',{name:'同步关联用例'}));const linked=await screen.findByLabelText('登录测试用例 · v2 · 已关联');const old=screen.getByLabelText('旧版导入用例 · v1 · 待选择关联');assert.equal((linked as HTMLInputElement).checked,true);assert.equal((old as HTMLInputElement).checked,false);fireEvent.click(old);fireEvent.click(screen.getByRole('button',{name:'预览修改'}));await screen.findByText('检查完成');assert.deepEqual(requested.related_artifact_ids,['case-art','old-art']);assert.deepEqual(requested.selected_ids,['S1']);}finally{cleanup();globalThis.fetch=original;}
});

test('unselected microtuning omits selection and changing instruction discards stale preview',async()=>{
 const original=globalThis.fetch;let request:any;globalThis.fetch=(async(_url:any,init:any)=>{request=JSON.parse(init.body);return json({id:'preview',summary:'变更',changes:[{artifact_id:cases.id,expected_revision:2,items:[{...cases.items[0],title:'清楚的标题'}]}]});}) as typeof fetch;
 try{render(<ArtifactActions artifact={cases} selected={[]} onChanged={()=>{}}/>);fireEvent.click(screen.getByRole('button',{name:'AI 微调'}));fireEvent.click(screen.getByRole('button',{name:'统一术语'}));fireEvent.click(screen.getByRole('button',{name:'预览修改'}));await screen.findByRole('button',{name:'确认应用修改'});assert.equal('selected_ids' in request,false);fireEvent.change(screen.getByLabelText('本次微调或分析要求'),{target:{value:'只修改前置条件'}});assert.equal(screen.queryByRole('button',{name:'确认应用修改'}),null);}finally{cleanup();globalThis.fetch=original;}
});

test('coverage shows linked IDs and gaps without describing execution success',async()=>{
 const original=globalThis.fetch;globalThis.fetch=(async()=>json({coverage:{totals:{requirements:2,requirements_with_scenarios:2,requirements_with_cases:1,scenarios:2,scenarios_with_cases:1,cases:1},scenarios:[{id:'S1',title:'成功登录',case_ids:['C1'],case_count:1},{id:'S2',title:'失败登录',case_ids:[],case_count:0}],requirements:[{id:'R1',title:'登录',scenario_ids:['S1'],case_ids:['C1']}],orphan_cases:['C-OLD'],notes:[]}})) as typeof fetch;
 try{render(<WorkspaceCoverage artifactId="scenario-art" revision={3}/>);fireEvent.click(screen.getByRole('button',{name:'查看需求 → 场景 → 用例覆盖'}));await screen.findByText('S2 · 失败登录');assert.ok(screen.getByText('待补用例'));assert.ok(screen.getByText('未找到关联场景的用例：C-OLD'));assert.ok(screen.getByText(/已有用例不代表已执行通过/));}finally{cleanup();globalThis.fetch=original;}
});

test('review artifacts open full steps and expected results and can collapse all',async()=>{
 const {ArtifactCard}=await import('../src/ArtifactCard');const original=globalThis.fetch;globalThis.fetch=(async()=>json({...cases,report:{review_reports:[{summary:'评审完成'}]}})) as typeof fetch;
 try{render(<ArtifactCard id={cases.id} onTarget={()=>{}} onChanged={()=>{}}/>);await screen.findByText('账号处于启用状态');assert.ok(screen.getByText('输入密码'));assert.ok(screen.getByText('显示首页'));assert.ok(screen.getByRole('table',{name:'C1 步骤与预期结果'}));fireEvent.click(screen.getByRole('button',{name:'收起全部步骤'}));assert.equal(screen.queryByText('输入密码'),null);fireEvent.click(screen.getByRole('button',{name:'展开全部步骤'}));assert.ok(screen.getByText('输入密码'));}finally{cleanup();globalThis.fetch=original;}
});

test('coverage can select an explicit related case branch and warns when cases are stale',async()=>{
 const original=globalThis.fetch;const paths:string[]=[];globalThis.fetch=(async(url:any)=>{paths.push(String(url));return json({related_artifacts:[{id:'a1',type:'cases',title:'登录用例第一版',revision:1,linked:true},{id:'a2',type:'cases',title:'登录用例第二版',revision:2,linked:true}],stale:{scenarios:true,changed_scenario_ids:['S1']},coverage:{totals:{scenarios:1,cases:1},scenarios:[{id:'S1',title:'成功登录',case_ids:['C1'],case_count:1}],notes:[]}});}) as typeof fetch;
 try{render(<WorkspaceCoverage artifactId="scenario-art" revision={3} artifactType="scenarios"/>);fireEvent.click(screen.getByRole('button',{name:'查看需求 → 场景 → 用例覆盖'}));await screen.findByLabelText('覆盖统计使用的用例成果');assert.ok(screen.getByText(/场景已更新，关联用例仍基于较早版本/));fireEvent.change(screen.getByLabelText('覆盖统计使用的用例成果'),{target:{value:'a1'}});await waitFor(()=>assert.ok(paths.some(path=>path.endsWith('?case_artifact_id=a1'))));await screen.findByLabelText('覆盖统计使用的用例成果');}finally{cleanup();globalThis.fetch=original;}
});

test('multiple linked branches require an explicit sync target before preview',async()=>{
 const original=globalThis.fetch;globalThis.fetch=(async()=>json({related_artifacts:[{id:'a1',type:'cases',title:'用例分支一',revision:1,linked:true},{id:'a2',type:'cases',title:'用例分支二',revision:1,linked:true}]})) as typeof fetch;
 try{render(<ArtifactActions artifact={scenario} selected={[]} onChanged={()=>{}}/>);fireEvent.click(screen.getByRole('button',{name:'同步关联用例'}));const first=await screen.findByLabelText('用例分支一 · v1 · 已关联');assert.equal((first as HTMLInputElement).checked,false);assert.equal((screen.getByLabelText('用例分支二 · v1 · 已关联') as HTMLInputElement).checked,false);assert.equal(screen.getByRole('button',{name:'预览修改'}).hasAttribute('disabled'),true);fireEvent.click(first);assert.equal(screen.getByRole('button',{name:'预览修改'}).hasAttribute('disabled'),false);}finally{cleanup();globalThis.fetch=original;}
});

test('new supplemental sources are opt-in and sent only to the requested modification',async()=>{
 const original=globalThis.fetch;let requested:any;globalThis.fetch=(async(url:any,init:any)=>{if(String(url).endsWith('/workspace'))return json({sources:[{id:'new',name:'新增锁定规则',role:'change'},{id:'sample',name:'样例不可做需求',role:'example'}]});requested=JSON.parse(init.body);return json({id:'with-source',summary:'根据补充资料检查完成',changes:[]});}) as typeof fetch;
 try{render(<ArtifactActions artifact={cases} selected={['C1']} onChanged={()=>{}}/>);fireEvent.click(screen.getByRole('button',{name:'AI 微调选中 1 条'}));fireEvent.click(screen.getByRole('button',{name:'步骤更清楚'}));fireEvent.click(screen.getByRole('button',{name:'添加本次补充资料'}));const extra=await screen.findByLabelText('新增锁定规则');assert.equal((extra as HTMLInputElement).checked,false);assert.equal(screen.queryByLabelText('样例不可做需求'),null);fireEvent.click(extra);fireEvent.click(screen.getByRole('button',{name:'预览修改'}));await screen.findByText('根据补充资料检查完成');assert.deepEqual(requested.source_ids,['new']);assert.deepEqual(requested.selected_ids,['C1']);}finally{cleanup();globalThis.fetch=original;}
});

test('in-flight modification keeps its dialog visible with close disabled',async()=>{
 const original=globalThis.fetch;let finish:((value:Response)=>void)|undefined;globalThis.fetch=(()=>new Promise<Response>(resolve=>{finish=resolve;})) as typeof fetch;
 try{render(<ArtifactActions artifact={cases} selected={[]} onChanged={()=>{}}/>);fireEvent.click(screen.getByRole('button',{name:'AI 微调'}));fireEvent.click(screen.getByRole('button',{name:'步骤更清楚'}));fireEvent.click(screen.getByRole('button',{name:'预览修改'}));assert.equal(screen.getAllByRole('button',{name:'关闭'}).every(button=>button.hasAttribute('disabled')),true);const dialog=screen.getByRole('dialog');const cancel=new dom.window.Event('cancel',{bubbles:false,cancelable:true});fireEvent(dialog,cancel);assert.equal(cancel.defaultPrevented,true);assert.ok(screen.getByLabelText('本次微调或分析要求'));finish?.(json({id:'slow',summary:'完成',changes:[]}));await screen.findByText('完成');}finally{cleanup();globalThis.fetch=original;}
});
