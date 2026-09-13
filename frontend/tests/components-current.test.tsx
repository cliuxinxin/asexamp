// Retained current contracts extracted from interactions.test.tsx; archived legacy controls remain in legacy-tests/frontend.
import {JSDOM} from 'jsdom';
import {test,afterEach} from 'node:test';
import assert from 'node:assert/strict';

const dom=new JSDOM('<!doctype html><html><body></body></html>',{url:'http://localhost:8000/'});
for(const key of ['window','document','HTMLElement','HTMLDialogElement','MutationObserver','Node','Event','MouseEvent','File','FormData'])Object.defineProperty(globalThis,key,{value:(dom.window as any)[key],configurable:true,writable:true});
Object.defineProperty(globalThis,'navigator',{value:dom.window.navigator,configurable:true});
(globalThis as any).IS_REACT_ACT_ENVIRONMENT=true;
(globalThis as any).EventSource=class{addEventListener(){}close(){}};
dom.window.HTMLElement.prototype.scrollIntoView=function(){};
dom.window.HTMLDialogElement.prototype.showModal=function(){this.open=true;};
dom.window.HTMLDialogElement.prototype.close=function(){this.open=false;};
const React=await import('react');
const {render,fireEvent,screen,waitFor,cleanup,act}=await import('@testing-library/react');
const {ArtifactCard}=await import('../src/ArtifactCard');
const {SettingsDialog}=await import('../src/SettingsDialog');
afterEach(()=>cleanup());

test('input inspector ignores a late response after switching calls',async()=>{
 const {RequestInspector}=await import('../src/RequestInspector');const slow=deferred();
 globalThis.fetch=async(input:any)=>String(input).includes('old-call')?slow.promise:json({run_id:'new-run',call_id:'new-call',messages:[{role:'system',content:'NEW INPUT'}],parameters:{}});
 const view=render(<RequestInspector runId="old-run" callId="old-call" onClose={()=>{}}/>);
 view.rerender(<RequestInspector runId="new-run" callId="new-call" onClose={()=>{}}/>);
 await screen.findByText('NEW INPUT');
 await act(async()=>{slow.resolve(json({run_id:'old-run',call_id:'old-call',messages:[{role:'system',content:'STALE INPUT'}]}));});
 assert.equal(screen.queryByText('STALE INPUT'),null);
});

test('settings display the unified hour timeout as read only',async()=>{
 globalThis.fetch=async()=>json({provider:'ollama',base_url:'http://localhost:11434',model:'local',has_api_key:false,timeout_seconds:3600,timeout_policy:'fixed_60_minutes'});
 render(<SettingsDialog projectId="p1" profiles={profiles} activeProfileId="pf1" onClose={()=>{}} onSaved={()=>{}}/>);
 const timeout=await screen.findByLabelText('单次请求超时（秒）');assert.equal((timeout as HTMLInputElement).value,'3600');assert.equal((timeout as HTMLInputElement).disabled,true);
 assert.ok(screen.getByText(/统一为 60 分钟/));
});
const clone=(x:any)=>structuredClone(x);
const json=(x:any,status=200)=>new Response(JSON.stringify(x),{status,headers:{'Content-Type':'application/json'}});
const deferred=()=>{let resolve!:(value:any)=>void;const promise=new Promise<any>(r=>resolve=r);return{promise,resolve};};
const profiles=[{id:'pf1',project_id:'p1',name:'Default Profile',version:1,config:{language:'中文',case_types:['Business']}},{id:'pf2',project_id:'p1',name:'Detailed',version:2,config:{language:'English',case_types:['Negative']}}];

test('environment settings stay read-only and connection test does not overwrite them',async()=>{
 const calls:{path:string;method:string}[]=[];
 globalThis.fetch=async(input:any,init:RequestInit={})=>{const path=String(input);calls.push({path,method:init.method??'GET'});return json(path.endsWith('/settings/test')?{ok:true,message:'Connection verified'}:{provider:'openai',base_url:'https://api.example.test/v1',model:'env-model',timeout_seconds:300,has_api_key:true,environment_managed:true,env_file:'/data/.env'});};
 render(<SettingsDialog projectId="p1" profiles={profiles} activeProfileId="pf1" onClose={()=>{}} onSaved={()=>{}}/>);
 const input=await screen.findByLabelText('模型名称');
 assert.equal((input as HTMLInputElement).disabled,true);
 assert.match(screen.getByText(/配置由.*管理/).textContent??'',/\.env/);
 fireEvent.click(screen.getByRole('button',{name:'测试连接'}));
 await screen.findByText('Connection verified');
 assert.equal(calls.filter(c=>c.method==='PUT').length,0);
 assert.equal(calls.filter(c=>c.path.endsWith('/settings/test')).length,1);
});

test('saving UI settings preserves configured request timeout',async()=>{
 let saved:any;
 globalThis.fetch=async(input:any,init:RequestInit={})=>{if(init.method==='PUT')saved=JSON.parse(String(init.body));return json({provider:'ollama',base_url:'http://localhost:11434',model:'local',has_api_key:false,timeout_seconds:300});};
 render(<SettingsDialog projectId="p1" profiles={profiles} activeProfileId="pf1" onClose={()=>{}} onSaved={()=>{}}/>);
 await screen.findByLabelText('模型名称');
 fireEvent.click(screen.getByRole('button',{name:'保存',exact:true}));
 await waitFor(()=>assert.equal(saved?.timeout_seconds,300));
});
function fixture(override?:(path:string,init:RequestInit)=>Promise<Response>|undefined){
 const chats:any={p1:[{id:'c1',project_id:'p1',title:'对话一',updated_at:'2026-09-07T01:00:00Z'},{id:'c2',project_id:'p1',title:'对话二',updated_at:'2026-09-07T00:00:00Z'}],p2:[{id:'c3',project_id:'p2',title:'项目二对话',updated_at:'2026-09-07T00:00:00Z'}]};
 const calls:{path:string;init:RequestInit}[]=[];
 globalThis.fetch=async(input:any,init:RequestInit={})=>{const path=String(input).replace('/api','');calls.push({path,init});const response=override?.(path,init);if(response)return response;
  if(path==='/projects')return json([{id:'p1',name:'项目一'},{id:'p2',name:'项目二'}]);
  if(path==='/settings')return json({provider:'ollama',base_url:'http://localhost:11434',model:'installed-model',has_api_key:false,timeout_seconds:120});
  if(path.endsWith('/profiles'))return json(path.includes('/p1/')?profiles:[{...profiles[0],id:'pf3',project_id:'p2'}]);
  if(path.endsWith('/chats'))return json(chats[path.split('/')[2]]);
  if(path.startsWith('/chats/')){const id=path.split('/')[2];return json({chat:Object.values(chats).flat().find((c:any)=>c.id===id),messages:[],sources:[],runs:[]});}
  throw new Error('Unexpected API request: '+path);
 };return{calls,chats};
}

test('artifact draft keeps base revision and preserves unknown fields',async()=>{
 let revision=1;let submitted:any;const item={id:'TC-1',title:'原用例',type:'Business',priority:'P1',preconditions:'已登录',scenario_id:'SC-1',steps:[{action:'申请退款',expected:'成功'}],refs:['s1#P1'],custom_tag:'preserve-me'};
 globalThis.fetch=async(_path:any,init:RequestInit={})=>{if(init.method==='PUT'){submitted=JSON.parse(String(init.body));return json({detail:'版本冲突'},409);}return json({id:'a1',type:'cases',title:'测试用例',revision,items:[{...item,title:revision===1?'原用例':'其他页面的新内容'}]});};
 const view=render(<ArtifactCard id="a1" refreshKey="one" onTarget={()=>{}} onChanged={()=>{}}/>);await screen.findByText('原用例');fireEvent.click(screen.getByRole('button',{name:'编辑',exact:true}));
 fireEvent.change(screen.getByLabelText('条目 TC-1 标题'),{target:{value:'我的修改'}});revision=2;view.rerender(<ArtifactCard id="a1" refreshKey="two" onTarget={()=>{}} onChanged={()=>{}}/>);
 await screen.findByText('其他页面的新内容');fireEvent.click(screen.getByRole('button',{name:'保存新版本'}));await screen.findAllByText('版本冲突');
 assert.equal(submitted.expected_revision,1);assert.equal(submitted.items[0].title,'我的修改');assert.equal(submitted.items[0].custom_tag,'preserve-me');assert.deepEqual(submitted.items[0].refs,['s1#P1']);
 assert.equal((screen.getByLabelText('条目 TC-1 标题') as HTMLInputElement).value,'我的修改');
});

test('template proposal starts at active Profile and survives destination changes',async()=>{
 let submitted:any;fixture((path,init)=>{if(path.startsWith('/profiles/')&&init.method==='PUT'){submitted={path,...JSON.parse(String(init.body))};return Promise.resolve(json({...profiles[0],version:2}));}});
 render(<SettingsDialog projectId="p1" profiles={profiles} activeProfileId="pf2" proposal={{additional_rules:'AI 推荐格式'}} onClose={()=>{}} onSaved={()=>{}}/>);
 assert.equal((screen.getByLabelText('选择 Profile') as HTMLSelectElement).value,'pf2');
 fireEvent.change(screen.getByLabelText('选择 Profile'),{target:{value:'pf1'}});assert.equal(JSON.parse((screen.getByLabelText('Profile 配置 JSON') as HTMLTextAreaElement).value).additional_rules,'AI 推荐格式');
 fireEvent.click(screen.getByRole('button',{name:'更新当前 Profile'}));await waitFor(()=>assert.ok(submitted));assert.equal(submitted.path,'/profiles/pf1');assert.equal(submitted.config.additional_rules,'AI 推荐格式');
});

test('invalid JSON cannot crash the readable artifact editor',async()=>{
 fixture(path=>path==='/artifacts/a1'?Promise.resolve(json({id:'a1',type:'cases',title:'用例',revision:1,items:[]})):undefined);
 render(<ArtifactCard id="a1" refreshKey="x" onTarget={()=>{}} onChanged={()=>{}}/>);await screen.findByRole('button',{name:'编辑',exact:true});fireEvent.click(screen.getByRole('button',{name:'编辑',exact:true}));fireEvent.click(screen.getByRole('button',{name:'JSON · 全部字段'}));
 for(const invalid of ['[null]','[{"id":"TC-1","steps":"wrong"}]','[{"id":"TC-1","steps":[],"refs":"x"}]','[{"id":"TC-1","steps":[],"refs":[{}]}]']){fireEvent.change(screen.getByLabelText('产物 JSON'),{target:{value:invalid}});fireEvent.click(screen.getByRole('button',{name:'表单编辑'}));await screen.findAllByText(/每个条目必须是对象/);assert.equal((screen.getByLabelText('产物 JSON') as HTMLTextAreaElement).value,invalid);}
});

test('custom header settings preserve secrets unless explicitly replaced',async()=>{
 let saved:any;
 globalThis.fetch=async(_input:any,init:RequestInit={})=>{if(init.method==='PUT')saved=JSON.parse(String(init.body));return json({provider:'openai',base_url:'https://gateway.example/v1',model:'configured',timeout_seconds:3600,has_api_key:true,header_names:['X-Tenant-ID'],has_headers:true,auth_mode:'bearer'});};
 render(<SettingsDialog projectId="p1" profiles={profiles} onClose={()=>{}} onSaved={()=>{}}/>);
 await screen.findByLabelText('模型名称');fireEvent.click(screen.getByRole('button',{name:'保存',exact:true}));await waitFor(()=>assert.ok(saved));assert.equal(Object.hasOwn(saved,'headers'),false);
 fireEvent.click(screen.getByText('自定义请求头'));
 fireEvent.change(screen.getByLabelText('鉴权方式'),{target:{value:'headers'}});
 fireEvent.change(screen.getByLabelText('自定义 Header JSON'),{target:{value:'{"X-Tenant-ID":"tenant-2"}'}});
 fireEvent.click(screen.getByRole('button',{name:'保存',exact:true}));
 await waitFor(()=>assert.deepEqual(saved.headers,{'X-Tenant-ID':'tenant-2'}));assert.equal(saved.auth_mode,'headers');
});

test('recovery guidance offers model settings and identifies preserved progress',async()=>{
 const {RunCard}=await import('../src/RunCard');let opened=false;
 render(<RunCard run={{id:'r-error',experience:'agent',status:'failed',intent:'generate_case',mode:'auto',stage:'failed',updated_at:'2026-09-08',artifact_ids:[],recovery:{category:'authentication',title:'模型鉴权失败',detail:'检查网关请求头',suggestions:['核对 X-API-Key'],preserved:['需求分析'],retryable:false}} as any} onChanged={()=>{}} onTarget={()=>{}} onSettings={()=>{opened=true;}}/>);
 assert.ok(screen.getByText('核对 X-API-Key'));assert.ok(screen.getByText(/已保留.*需求分析/));
 fireEvent.click(screen.getByRole('button',{name:'打开模型设置'}));assert.equal(opened,true);
});

test('business diagram rejects executable directives and leaves a recoverable source view',async()=>{
 const {BusinessDiagram}=await import('../src/BusinessDiagram');
 render(<BusinessDiagram title="退款业务图" source={'flowchart TD\nA-->B\nclick A "https://evil.example"'}/>);
 await screen.findByText(/图表包含不支持的交互或配置/);
 assert.equal(document.querySelectorAll('a[href="https://evil.example"]').length,0);
 assert.ok(screen.getByText('Mermaid 源码'));
});

test('analysis reports expose business questions and distinguish design coverage from executed tests',async()=>{
 const {AnalysisReport}=await import('../src/AnalysisReport');
 render(<AnalysisReport report={{summary:'已经识别订单的关键状态',questions:['已发货后是否允许退款？'],strategy:{depth:'deep',rationale:'存在跨系统依赖',techniques:['状态迁移'],scope:['订单','退款']},coverage:{requirements_total:4,requirements_covered:3,branches_total:5,branches_covered:4,gaps:[{id:'g1',title:'缺少退款失败场景'}]}}}/>);
 assert.ok(screen.getByText('已发货后是否允许退款？'));assert.ok(screen.getByText('缺少退款失败场景'));assert.ok(screen.getByText(/尚未执行这些测试/));
});

test('business branches display case links and explicit association gaps',async()=>{
 const {AnalysisReport}=await import('../src/AnalysisReport');
 render(<AnalysisReport report={{business_model:{edges:[{id:'B1',from:'N1',to:'N2',label:'退款成功',refs:[]},{id:'B2',from:'N1',to:'N2',label:'退款失败',refs:[]}]},traceability:[{id:'C1',case_id:'C1',title:'成功退款到账',branch_ids:['B1'],requirement_ids:['R1'],scenario_id:'S1',refs:[]}]}}/>);
 assert.ok(screen.getByText('关联用例：C1'));assert.ok(screen.getByText('尚无关联用例'));
});

test('switching workspace artifact discards old editors before new data arrives',async()=>{
 const {ArtifactWorkspace}=await import('../src/ArtifactWorkspace');const pending=deferred();
 const a={id:'a1',type:'cases',title:'用例甲',revision:1,items:[{id:'C1',title:'原用例',steps:[],refs:[]}]} as any;
 fixture(path=>path==='/artifacts/a1'?Promise.resolve(json(a)):path==='/artifacts/a2'?pending.promise:undefined);
 const props={refreshKey:'x',onClose:()=>{},onTarget:()=>{},onChanged:()=>{}};
 const view=render(<ArtifactWorkspace artifact={a} {...props}/>);await screen.findByText('原用例');fireEvent.click(screen.getByRole('button',{name:'编辑',exact:true}));
 await screen.findByLabelText('条目 C1 标题');view.rerender(<ArtifactWorkspace artifact={{...a,id:'a2',title:'用例乙'}} {...props}/>);
 assert.ok(screen.queryByLabelText('条目 C1 标题')===null);assert.ok(screen.queryByRole('button',{name:'保存新版本'})===null);
 await act(async()=>pending.resolve(json({...a,id:'a2',title:'用例乙',items:[]})));
});

test('switching to private gateway seeds documented endpoint and displays actual timeout',async()=>{
 globalThis.fetch=async()=>json({provider:'ollama',base_url:'http://localhost:11434',model:'local',has_api_key:false,timeout_seconds:300});
 render(<SettingsDialog projectId="p1" profiles={profiles} activeProfileId="pf1" onClose={()=>{}} onSaved={()=>{}}/>);
 fireEvent.change(await screen.findByLabelText('模型服务'),{target:{value:'openai'}});
 assert.equal((screen.getByLabelText('服务地址') as HTMLInputElement).value,'http://10.206.3.151:8000/api/v1');
 assert.ok(screen.getByText('请求等待时间 · 300 秒'));
 assert.ok(screen.getByRole('option',{name:'API Key · X-API-Key'}));
});