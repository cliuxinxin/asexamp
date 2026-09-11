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
const {App}=await import('../src/App');
const {ArtifactCard}=await import('../src/ArtifactCard');
const {SettingsDialog}=await import('../src/SettingsDialog');
afterEach(()=>cleanup());

function streamingFixture(){
 const previous=(globalThis as any).EventSource;
 const instances:FakeSource[]=[];
 class FakeSource{
  listeners=new Map<string,Function[]>();closed=false;onerror?:Function;onopen?:Function;readyState=1;
  constructor(public url:string){instances.push(this);}
  addEventListener(name:string,fn:Function){this.listeners.set(name,[...(this.listeners.get(name)??[]),fn]);}
  emit(name:string,id:number,data:any){for(const fn of this.listeners.get(name)??[])fn({lastEventId:String(id),data:JSON.stringify(data)});}
  close(){this.closed=true;}
 }
 (globalThis as any).EventSource=FakeSource;
 return{instances,restore:()=>{(globalThis as any).EventSource=previous;}};
}

test('run card renders SSE text incrementally and reconnects without duplicating output',async()=>{
 const {RunCard}=await import('../src/RunCard');const stream=streamingFixture();
 const run:any={id:'run-stream',status:'running',stage:'case_generation',updated_at:'2026-09-07T01:00:00Z',artifact_ids:[]};
 try{
  const view=render(<RunCard run={run} onChanged={()=>{}} onTarget={()=>{}}/>);
  await waitFor(()=>assert.equal(stream.instances.length,1));const source=stream.instances[0];
  act(()=>{source.emit('progress',1,{event:'model.start',run_id:run.id,node:'cases',stage:'case_generation',call_id:'call-one',attempt:1,at:run.updated_at});source.emit('model_delta',2,{run_id:run.id,call_id:'call-one',text:'第一段'});});
  await screen.findByText('第一段');
  act(()=>{source.onerror?.();source.emit('model_delta',2,{run_id:run.id,call_id:'call-one',text:'重复内容'});source.emit('model_delta',3,{run_id:run.id,call_id:'call-one',text:'，第二段'});});
  await screen.findByText('第一段，第二段');assert.equal(source.closed,false);assert.equal(screen.queryByText(/重复内容/),null);
  act(()=>source.emit('progress',4,{event:'model.error',run_id:run.id,node:'cases',call_id:'call-one',elapsed_ms:300,at:run.updated_at}));
  await screen.findByText(/本次请求或阶段失败/);
  view.rerender(<RunCard run={{...run,status:'failed'}} onChanged={()=>{}} onTarget={()=>{}}/>);
  await waitFor(()=>assert.equal(stream.instances.length,2));
  assert.match(stream.instances[1].url,/after=4/);
  act(()=>{stream.instances[1].emit('progress',5,{event:'model.start',run_id:run.id,node:'cases',stage:'case_generation',call_id:'call-two',attempt:2,at:run.updated_at});stream.instances[1].emit('model_delta',6,{run_id:run.id,call_id:'call-two',text:'新请求的输出'});});
  await screen.findByText('新请求的输出');assert.ok(screen.getByText('第一段，第二段'));
 }finally{stream.restore();}
});

test('completed history can replay its full process and switching runs rejects stale SSE text',async()=>{
 const {RunTimeline}=await import('../src/RunTimeline');const stream=streamingFixture();
 try{
  const view=render(<RunTimeline runId="old-run" initialOpen={false}/>);
  assert.equal(stream.instances.length,0);
  fireEvent.click(screen.getByRole('button',{name:/查看完整执行过程/}));
  const first=stream.instances[0];
  act(()=>{first.emit('progress',1,{event:'model.start',run_id:'old-run',node:'analysis',call_id:'old-call',at:'2026-09-07T01:00:00Z'});first.emit('model_delta',2,{run_id:'old-run',call_id:'old-call',text:'旧任务输出'});});
  await screen.findByText('旧任务输出');
  view.rerender(<RunTimeline runId="new-run"/>);
  await waitFor(()=>assert.equal(stream.instances.length,2));
  act(()=>first.emit('model_delta',3,{run_id:'old-run',call_id:'old-call',text:'不能串到新任务'}));
  assert.equal(screen.queryByText(/旧任务输出|不能串到新任务/),null);
  assert.match(stream.instances[1].url,/new-run.*after=0/);
 }finally{stream.restore();}
});
test('cancel and recovery finalize unfinished output and paused edits have their own stage',async()=>{
 const {emptyFeed,reduceEvent}=await import('../src/runProgress');
 for(const ending of ['run.cancelled','run.suspended','run.recovered','edit.recovered','run.failed']){
  let feed=reduceEvent(emptyFeed(),'progress',1,{event:'model.start',run_id:'r',node:'paused_edit',stage:'scenario_review',call_id:'c',at:'2026-09-07T01:00:00Z'});
  assert.equal(feed.calls.c.label,'修改待确认场景');
  feed=reduceEvent(feed,'progress',2,{event:ending,at:'2026-09-07T01:00:01Z'});
  assert.equal(feed.calls.c.status,ending==='run.failed'?'failed':'cancelled');
  feed=reduceEvent(feed,'progress',3,{event:'model.complete',call_id:'c',node:'paused_edit'});
  assert.equal(feed.calls.c.status,ending==='run.failed'?'failed':'cancelled');
 }
});

test('permanently closed SSE offers reconnect using the saved cursor',async()=>{
 const {RunTimeline}=await import('../src/RunTimeline');const stream=streamingFixture();
 try{
  render(<RunTimeline runId="r"/>);const source=stream.instances[0];
  act(()=>{source.emit('progress',12,{event:'run.retried',run_id:'r',at:'2026-09-07T01:00:00Z'});source.readyState=2;source.onerror?.();});
  const retry=await screen.findByRole('button',{name:'重新连接'});fireEvent.click(retry);
  await waitFor(()=>assert.equal(stream.instances.length,2));assert.match(stream.instances[1].url,/after=12/);
  await screen.findByText('从失败阶段重新执行');
 }finally{stream.restore();}
});
test('actual request input loads only when opened and stays separate from streamed output',async()=>{
 const {RunTimeline}=await import('../src/RunTimeline');const stream=streamingFixture();const reads:string[]=[];
 globalThis.fetch=async(input:any)=>{reads.push(String(input));return json({run_id:'inspect-run',call_id:'inspect-call',model:'test',base_url:'http://localhost:1234/v1',timeout_seconds:3600,messages:[{role:'system',content:[{type:'text',text:'EXACT SYSTEM CONTRACT'}]},{role:'user',content:[{type:'text',text:JSON.stringify({evidence:[{text:'<img src=x onerror=alert(1)> FULL SOURCE'}],validation_repair:{previous_response:'REJECTED JSON'}})}]}],parameters:{stream:true}});};
 try{
  render(<RunTimeline runId="inspect-run"/>);
  act(()=>{stream.instances[0].emit('progress',1,{event:'model.start',run_id:'inspect-run',node:'cases',call_id:'inspect-call',at:'2026-09-07T01:00:00Z'});stream.instances[0].emit('progress',2,{event:'model.request_saved',run_id:'inspect-run',call_id:'inspect-call',request_available:true});});
  const button=await screen.findByRole('button',{name:'查看发送内容'});assert.equal(reads.length,0);
  fireEvent.click(button);await screen.findByText('EXACT SYSTEM CONTRACT');assert.equal(reads.length,1);assert.ok(screen.getByText(/应用交给 LangChain/));
  fireEvent.click(screen.getByRole('button',{name:'任务上下文'}));await screen.findByText(/FULL SOURCE/);assert.equal(document.querySelector('img'),null);
  assert.ok(screen.getByText(/REJECTED JSON/));
  assert.match(screen.getByRole('link',{name:'下载请求记录'}).getAttribute('href')!,/inspect-run.*inspect-call.*download=true/);
  act(()=>stream.instances[0].emit('model_delta',3,{run_id:'inspect-run',call_id:'inspect-call',text:'INCREMENTAL ANSWER'}));
  await screen.findByText('INCREMENTAL ANSWER');assert.ok(screen.getByText(/FULL SOURCE/));
  fireEvent.click(screen.getByRole('button',{name:'关闭',exact:true}));assert.equal(screen.queryByRole('dialog'),null);
 }finally{stream.restore();}
});

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
  if(path.endsWith('/interpret')){const body=JSON.parse(String(init.body));return json({handled:false,intent:body.intent_hint&&body.intent_hint!=='auto'?body.intent_hint:'generate_case'});}
  if(path.startsWith('/chats/')){const id=path.split('/')[2];return json({chat:Object.values(chats).flat().find((c:any)=>c.id===id),messages:[],sources:[],runs:[]});}
  throw new Error('Unexpected API request: '+path);
 };return{calls,chats};
}
async function ready(){await screen.findByRole('button',{name:'对话一'});await waitFor(()=>assert.equal((screen.getByRole('button',{name:'新建生成会话'}) as HTMLButtonElement).disabled,false));}

test('delayed send preserves newer text and drafts survive chat navigation',async()=>{
 const pending=deferred();fixture((path,init)=>path==='/chats/c1/messages'?pending.promise:undefined);render(<App/>);await ready();
 fireEvent.change(screen.getByLabelText('聊天输入'),{target:{value:'第一条需求'}});fireEvent.click(screen.getByLabelText('发送消息'));
 fireEvent.change(screen.getByLabelText('聊天输入'),{target:{value:'尚未发送的新草稿'}});
 await act(async()=>pending.resolve(json({message:{},run:{id:'r1'}})));
 await waitFor(()=>assert.equal((screen.getByLabelText('聊天输入') as HTMLTextAreaElement).value,'尚未发送的新草稿'));
 fireEvent.click(screen.getByRole('button',{name:'对话二'}));await waitFor(()=>assert.equal(screen.getByRole('button',{name:'对话二'}).classList.contains('active'),true));
 fireEvent.change(screen.getByLabelText('聊天输入'),{target:{value:'第二个对话的草稿'}});
 fireEvent.click(screen.getByRole('button',{name:'对话一'}));await waitFor(()=>assert.equal((screen.getByLabelText('聊天输入') as HTMLTextAreaElement).value,'尚未发送的新草稿'));
});

test('Enter cannot submit while requirement upload is unfinished',async()=>{
 const pending=deferred();const state=fixture(path=>path==='/chats/c1/sources'?pending.promise:undefined);render(<App/>);await ready();
 const input=document.querySelector('input[type=file]')!;fireEvent.change(input,{target:{files:[new File(['text'],'requirement.txt',{type:'text/plain'})]}});
 await waitFor(()=>assert.equal((screen.getByLabelText('上传需求文档') as HTMLButtonElement).disabled,true));
 fireEvent.change(screen.getByLabelText('聊天输入'),{target:{value:'生成用例'}});fireEvent.keyDown(screen.getByLabelText('聊天输入'),{key:'Enter',code:'Enter'});
 assert.equal(state.calls.filter(c=>c.path.endsWith('/messages')).length,0);
 await act(async()=>pending.resolve(json({id:'s1'})));
});

test('multi-file upload continues after one failure and reports every result',async()=>{
 const uploaded:string[]=[];fixture((path,init)=>{if(path==='/chats/c1/sources'){const file=(init.body as FormData).get('file') as File;uploaded.push(file.name);return file.name==='bad.txt'?Promise.resolve(json({detail:'文件无法解析'},400)):Promise.resolve(json({id:'source-'+file.name}));}});
 render(<App/>);await ready();const files=[new File(['bad'],'bad.txt'),new File(['good'],'good.txt')];fireEvent.change(document.querySelector('input[type=file]')!,{target:{files}});
 await screen.findByText('bad.txt：文件无法解析');await screen.findByText('good.txt：上传成功');assert.deepEqual(uploaded,['bad.txt','good.txt']);
});

test('delayed send cannot replace another project sidebar or draft',async()=>{
 const pending=deferred();fixture(path=>path==='/chats/c1/messages'?pending.promise:undefined);render(<App/>);await ready();
 fireEvent.change(screen.getByLabelText('聊天输入'),{target:{value:'项目一需求'}});fireEvent.click(screen.getByLabelText('发送消息'));
 fireEvent.change(screen.getByLabelText('当前项目'),{target:{value:'p2'}});await screen.findByRole('button',{name:'项目二对话'});
 fireEvent.change(screen.getByLabelText('聊天输入'),{target:{value:'项目二草稿'}});
 await act(async()=>pending.resolve(json({message:{},run:{id:'r1'}})));
 await waitFor(()=>assert.equal(screen.queryByRole('button',{name:'对话一'}),null));
 assert.equal((screen.getByLabelText('聊天输入') as HTMLTextAreaElement).value,'项目二草稿');
});

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

test('rejected message retains submitted requirements',async()=>{
 fixture(path=>path==='/chats/c1/messages'?Promise.resolve(json({detail:'请先配置模型'},400)):undefined);render(<App/>);await ready();fireEvent.change(screen.getByLabelText('聊天输入'),{target:{value:'需要保留的需求正文'}});fireEvent.click(screen.getByLabelText('发送消息'));await screen.findByText('请先配置模型');assert.equal((screen.getByLabelText('聊天输入') as HTMLTextAreaElement).value,'需要保留的需求正文');
});

test('all-result AI edits omit selection while selected edits send explicit IDs',async()=>{
 const artifact={id:'a1',type:'cases',title:'测试用例',revision:1,items:[{id:'TC-1',title:'退款成功',type:'Business',priority:'P1',preconditions:'',scenario_id:'SC-1',steps:[{action:'退款',expected:'成功'}],refs:['s1#P1']}]};
 let submitted:any;
 fixture((path,init)=>{
  if(path==='/artifacts/a1')return Promise.resolve(json(artifact));
  if(path==='/chats/c1/messages'){submitted=JSON.parse(String(init.body));return Promise.resolve(json({message:{},run:{id:'r1'}}));}
  if(path==='/chats/c1')return Promise.resolve(json({chat:{id:'c1',project_id:'p1',title:'对话一'},messages:[{id:'m1',role:'assistant',content:'已完成',metadata:{artifact_ids:['a1']}}],sources:[],runs:[]}));
 });
 render(<App/>);await ready();fireEvent.click(await screen.findByRole('button',{name:'打开成果 · 测试用例'}));await screen.findByText('退款成功');fireEvent.click(screen.getByRole('button',{name:'让 AI 修改此结果'}));fireEvent.change(screen.getByLabelText('聊天输入'),{target:{value:'优化全部标题'}});fireEvent.click(screen.getByLabelText('发送消息'));
 await waitFor(()=>assert.ok(submitted));assert.equal(submitted.intent,'modify');assert.equal(Object.hasOwn(submitted,'selected_ids'),false);
 await waitFor(()=>assert.equal((screen.getByLabelText('聊天输入') as HTMLTextAreaElement).value,''));
 submitted=undefined;fireEvent.click(screen.getByLabelText('选择 TC-1'));fireEvent.click(screen.getByRole('button',{name:'让 AI 修改选中 1 条'}));fireEvent.change(screen.getByLabelText('聊天输入'),{target:{value:'只改选中的标题'}});fireEvent.click(screen.getByLabelText('发送消息'));
 await waitFor(()=>assert.ok(submitted));assert.deepEqual(submitted.selected_ids,['TC-1']);
});

test('a delayed new-chat response cannot switch the newly selected project',async()=>{
 const pending=deferred();fixture((path,init)=>path==='/projects/p1/chats'&&init.method==='POST'?pending.promise:undefined);render(<App/>);await ready();
 fireEvent.click(screen.getByRole('button',{name:'新对话',exact:true}));fireEvent.change(screen.getByLabelText('当前项目'),{target:{value:'p2'}});await screen.findByRole('button',{name:'项目二对话'});
 await act(async()=>pending.resolve(json({id:'late-chat',project_id:'p1',title:'迟到的对话'})));
 assert.equal(screen.queryByRole('button',{name:'迟到的对话'}),null);assert.equal((screen.getByLabelText('当前项目') as HTMLSelectElement).value,'p2');
});

test('project loading blocks new chat and keyboard submission until its initial list arrives',async()=>{
 const pending=deferred();const state=fixture((path,init)=>path==='/projects/p2/chats'&&!init.method?pending.promise:undefined);render(<App/>);await ready();
 fireEvent.change(screen.getByLabelText('当前项目'),{target:{value:'p2'}});
 assert.equal((screen.getByRole('button',{name:'新对话',exact:true}) as HTMLButtonElement).disabled,true);
 fireEvent.change(screen.getByLabelText('聊天输入'),{target:{value:'尚未加载完的项目需求'}});fireEvent.keyDown(screen.getByLabelText('聊天输入'),{key:'Enter'});
 assert.equal(state.calls.filter(c=>c.init.method==='POST').length,0);
 await act(async()=>pending.resolve(json(state.chats.p2)));await screen.findByRole('button',{name:'项目二对话'});
 assert.equal((screen.getByRole('button',{name:'新对话',exact:true}) as HTMLButtonElement).disabled,false);
});

test('invalid JSON cannot crash the readable artifact editor',async()=>{
 fixture(path=>path==='/artifacts/a1'?Promise.resolve(json({id:'a1',type:'cases',title:'用例',revision:1,items:[]})):undefined);
 render(<ArtifactCard id="a1" refreshKey="x" onTarget={()=>{}} onChanged={()=>{}}/>);await screen.findByRole('button',{name:'编辑',exact:true});fireEvent.click(screen.getByRole('button',{name:'编辑',exact:true}));fireEvent.click(screen.getByRole('button',{name:'JSON · 全部字段'}));
 for(const invalid of ['[null]','[{"id":"TC-1","steps":"wrong"}]','[{"id":"TC-1","steps":[],"refs":"x"}]','[{"id":"TC-1","steps":[],"refs":[{}]}]']){fireEvent.change(screen.getByLabelText('产物 JSON'),{target:{value:invalid}});fireEvent.click(screen.getByRole('button',{name:'表单编辑'}));await screen.findAllByText(/每个条目必须是对象/);assert.equal((screen.getByLabelText('产物 JSON') as HTMLTextAreaElement).value,invalid);}
});

test('only explicit requirement checkbox promotes a chat message to source evidence',async()=>{
 let submitted:any;fixture((path,init)=>path==='/chats/c1/messages'?(submitted=JSON.parse(String(init.body)),Promise.resolve(json({message:{},run:{id:'r1'}}))):undefined);render(<App/>);await ready();
 fireEvent.change(screen.getByLabelText('聊天输入'),{target:{value:'订单支付成功后，用户可以申请部分退款。'}});fireEvent.click(screen.getByRole('button',{name:/本次方案/}));fireEvent.click(screen.getByLabelText('这条消息是需求正文'));fireEvent.click(screen.getByLabelText('发送消息'));await waitFor(()=>assert.ok(submitted));assert.equal(submitted.as_requirement,true);
 await waitFor(()=>assert.equal((screen.getByLabelText('聊天输入') as HTMLTextAreaElement).value,''));assert.equal((screen.getByLabelText('这条消息是需求正文') as HTMLInputElement).checked,false);
});

test('backend stage names and slow model progress are visible with a diagnostic download',async()=>{
 const {RunCard}=await import('../src/RunCard');
 const run={id:'r-slow',status:'running',intent:'review_requirement',mode:'auto',stage:'requirement_analysis',created_at:new Date().toISOString(),updated_at:new Date().toISOString(),artifact_ids:[],diagnostic:{event:'model.waiting',at:new Date().toISOString(),task:'analyze_requirement',batch_index:2,batch_count:4,attempt:1,max_attempts:2,elapsed_ms:15000,timeout_seconds:120}};
 render(<RunCard run={run} onChanged={()=>{}} onTarget={()=>{}}/>);
 await screen.findByText('分析需求');await screen.findByText(/第 2\/4 批/);await screen.findByText(/等待模型响应/);
 const link=screen.getByRole('link',{name:'下载诊断日志'});assert.equal(link.getAttribute('href'),'/api/runs/r-slow/diagnostics?download=true');
 assert.equal(screen.queryByText('正在处理'),null);
});

test('reliable composer hides task selectors and sends the chosen design depth',async()=>{
 let submitted:any;fixture((path,init)=>path==='/chats/c1/messages'?(submitted=JSON.parse(String(init.body)),Promise.resolve(json({run:{id:'r'}}))):undefined);
 render(<App/>);await ready();
 assert.ok(screen.queryByLabelText('任务目标')===null, 'Task selectors must be collapsed by default');
 fireEvent.click(screen.getByRole('button',{name:/本次方案/}));
 fireEvent.change(screen.getByLabelText('测试设计深度'),{target:{value:'deep'}});
 fireEvent.change(screen.getByLabelText('聊天输入'),{target:{value:'关注退款与支付状态'}});
 fireEvent.click(screen.getByLabelText('发送消息'));
 await waitFor(()=>assert.equal(submitted?.experience,'reliable'));
 assert.equal(submitted.depth,'deep');assert.equal(submitted.mode,'auto');
});

test('reliable submissions expose depth case types and explicit Auto or HITP mode',async()=>{
 let submitted:any;fixture((path,init)=>path==='/chats/c1/messages'?(submitted=JSON.parse(String(init.body)),Promise.resolve(json({run:{id:'r'}}))):undefined);
 render(<App/>);await ready();fireEvent.click(screen.getByRole('button',{name:/本次方案/}));
 assert.equal((screen.getByLabelText('测试设计深度') as HTMLSelectElement).value,'standard');
 assert.equal((screen.getByLabelText('Business') as HTMLInputElement).checked,true);
 assert.equal((screen.getByLabelText('Negative') as HTMLInputElement).checked,true);
 assert.equal((screen.getByLabelText('Boundary') as HTMLInputElement).checked,true);
 assert.equal((screen.getByLabelText('Security') as HTMLInputElement).checked,false);
 fireEvent.click(screen.getByLabelText('Security'));fireEvent.click(screen.getByLabelText('人工确认关键节点'));
 fireEvent.change(screen.getByLabelText('聊天输入'),{target:{value:'生成可靠用例'}});fireEvent.click(screen.getByLabelText('发送消息'));
 await waitFor(()=>assert.ok(submitted));assert.equal(submitted.experience,'reliable');assert.equal(submitted.mode,'hitp');assert.equal(submitted.depth,'standard');assert.deepEqual(submitted.case_types,['Business','Negative','Boundary','Security']);
 assert.equal(Object.hasOwn(submitted,'confirm_strategy'),false);
});

test('run progress shows durable counts including failures and retry scope',async()=>{
 const {RunCard}=await import('../src/RunCard');
 render(<RunCard run={{id:'r-count',experience:'reliable',status:'failed',intent:'generate_case',mode:'auto',stage:'case_generation',updated_at:'2026-09-09',artifact_ids:[],progress:{phase:'case_generation',completed:17,total:24,label:'生成用例'},error:'第 3 批失败'} as any} onChanged={()=>{}} onTarget={()=>{}}/>);
 assert.ok(screen.getByText(/已完成 17 \/ 24/));assert.ok(screen.getByText(/失败.*第 3 批/));assert.ok(screen.getByRole('button',{name:/重试当前批次/}));
});

test('project memory can be added and deleted by the user',async()=>{
 let created:any;let deleted='';fixture((path,init)=>{
  if(path==='/projects/p1/memory'&&(!init.method||init.method==='GET'))return Promise.resolve(json([{id:'mem-1',content:'退款需原路返回',kind:'business',active:true}]));
  if(path==='/projects/p1/memory'&&init.method==='POST'){created=JSON.parse(String(init.body));return Promise.resolve(json({id:'mem-2',...created,active:true}));}
  if(path==='/projects/p1/memory/mem-1'&&init.method==='DELETE'){deleted=path;return Promise.resolve(json({ok:true}));}
 });
 render(<App/>);await ready();fireEvent.click(screen.getByRole('button',{name:'项目记忆'}));await screen.findByText('退款需原路返回');
 fireEvent.change(screen.getByLabelText('记忆内容'),{target:{value:'优先覆盖资金风险'}});fireEvent.change(screen.getByLabelText('记忆类型'),{target:{value:'preference'}});fireEvent.click(screen.getByRole('button',{name:'保存记忆'}));
 await waitFor(()=>assert.deepEqual(created,{content:'优先覆盖资金风险',kind:'preference'}));fireEvent.click(screen.getByRole('button',{name:'删除 退款需原路返回'}));await waitFor(()=>assert.equal(deleted,'/projects/p1/memory/mem-1'));
});

test('running agent accepts a supplemental instruction instead of starting a second run',async()=>{
 let submitted:any;
 fixture((path,init)=>{
  if(path==='/chats/c1')return Promise.resolve(json({chat:{id:'c1',title:'对话一'},messages:[],sources:[],runs:[{id:'r-live',experience:'agent',status:'running',stage:'planning',updated_at:'2026-09-08',artifact_ids:[]}]}));
  if(path==='/runs/r-live/instructions'){submitted=JSON.parse(String(init.body));return Promise.resolve(json({run:{id:'r-live'}}));}
 });
 render(<App/>);await ready();fireEvent.change(screen.getByLabelText('聊天输入'),{target:{value:'请增加重复退款的场景'}});
 assert.equal((screen.getByLabelText('发送消息') as HTMLButtonElement).disabled,false);
 fireEvent.click(screen.getByLabelText('发送消息'));await waitFor(()=>assert.equal(submitted?.content,'请增加重复退款的场景'));
});

test('agent summaries stream while raw invocation details stay collapsed',async()=>{
 const {RunCard}=await import('../src/RunCard');const stream=streamingFixture();
 try{
  render(<RunCard run={{id:'r-agent',experience:'agent',status:'running',intent:'generate_case',mode:'auto',stage:'planning',updated_at:'2026-09-08',artifact_ids:[]} as any} onChanged={()=>{}} onTarget={()=>{}}/>);
  await waitFor(()=>assert.equal(stream.instances.length,1));const source=stream.instances[0];
  act(()=>source.emit('agent',1,{run_id:'r-agent',agent:{depth:'deep',rationale:'涉及多种订单状态',plan:[{id:'p1',title:'核查订单规则',status:'running'}],insights:[{id:'i1',summary:'退款条件尚未明确',refs:[],kind:'question'}]}}));
  await screen.findByText('退款条件尚未明确');assert.ok(screen.getByText('核查订单规则'));
  act(()=>{source.emit('progress',2,{event:'model.start',run_id:'r-agent',call_id:'call-a'});source.emit('model_delta',3,{run_id:'r-agent',call_id:'call-a',text:'RAW-MODEL-JSON'});});
  assert.equal(screen.queryByText('RAW-MODEL-JSON'),null);
  fireEvent.click(screen.getByRole('button',{name:/查看调用详情/}));await screen.findByText('RAW-MODEL-JSON');
  fireEvent.click(screen.getByRole('button',{name:/收起调用详情/}));assert.equal(source.closed,false);
 }finally{stream.restore();}
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

test('completed artifacts open in a workspace without flooding the chat',async()=>{
 fixture(path=>{
  if(path==='/chats/c1')return Promise.resolve(json({chat:{id:'c1',title:'对话一'},sources:[],runs:[],messages:[{id:'m',role:'assistant',content:'完成了退款测试设计。',metadata:{artifact_ids:['result-a']}}]}));
  if(path==='/artifacts/result-a')return Promise.resolve(json({id:'result-a',type:'cases',title:'退款测试用例',revision:1,items:[{id:'TC-7',title:'失败退款不会重复扣款',type:'Negative',priority:'P1',steps:[],refs:[]}]}));
 });
 render(<App/>);await ready();
 const open=await screen.findByRole('button',{name:'打开成果 · 退款测试用例'});
 assert.ok(screen.queryByText('失败退款不会重复扣款')===null);
 fireEvent.click(open);await screen.findByText('失败退款不会重复扣款');
 fireEvent.click(screen.getByRole('button',{name:'关闭成果工作区'}));
 assert.ok(screen.queryByText('失败退款不会重复扣款')===null);
});

test('selected project preference controls subsequent agent requests',async()=>{
 let submitted:any;fixture((path,init)=>path==='/chats/c1/messages'?(submitted=JSON.parse(String(init.body)),Promise.resolve(json({run:{id:'r'}}))):undefined);
 render(<App/>);await ready();fireEvent.click(screen.getByRole('button',{name:'模型与设置'}));
 fireEvent.click(screen.getByRole('button',{name:'项目偏好',exact:true}));
 fireEvent.change(screen.getByLabelText('选择 Profile'),{target:{value:'pf2'}});
 fireEvent.click(screen.getByRole('button',{name:'使用此项目偏好'}));fireEvent.click(screen.getByRole('button',{name:'关闭',exact:true}));
 fireEvent.change(screen.getByLabelText('聊天输入'),{target:{value:'帮我梳理需求'}});fireEvent.click(screen.getByLabelText('发送消息'));
 await waitFor(()=>assert.equal(submitted?.profile_id,'pf2'));
});

test('business branches display case links and explicit association gaps',async()=>{
 const {AnalysisReport}=await import('../src/AnalysisReport');
 render(<AnalysisReport report={{business_model:{edges:[{id:'B1',from:'N1',to:'N2',label:'退款成功',refs:[]},{id:'B2',from:'N1',to:'N2',label:'退款失败',refs:[]}]},traceability:[{id:'C1',case_id:'C1',title:'成功退款到账',branch_ids:['B1'],requirement_ids:['R1'],scenario_id:'S1',refs:[]}]}}/>);
 assert.ok(screen.getByText('关联用例：C1'));assert.ok(screen.getByText('尚无关联用例'));
});

test('active agent never silently converts selected artifact edits into instructions',async()=>{
 const calls:string[]=[];
 fixture((path,init)=>{
  if(init.method==='POST')calls.push(path);
  if(path==='/chats/c1')return Promise.resolve(json({chat:{id:'c1',title:'对话一'},sources:[],messages:[{id:'m',role:'assistant',content:'之前的用例',metadata:{artifact_ids:['a-old']}}],runs:[{id:'r-live',experience:'agent',status:'running',stage:'planning',updated_at:'2026-09-08',artifact_ids:[]}]}));
  if(path==='/artifacts/a-old')return Promise.resolve(json({id:'a-old',type:'cases',title:'旧用例',revision:1,items:[{id:'C1',title:'旧条目',steps:[],refs:[]}]}));
 });
 render(<App/>);await ready();fireEvent.click(await screen.findByRole('button',{name:'打开成果 · 旧用例'}));
 await screen.findByText('旧条目');fireEvent.click(screen.getByRole('button',{name:'让 AI 修改此结果'}));
 await screen.findByText(/请在当前任务完成后修改已有结果/);
 assert.equal(calls.length,0);assert.ok(screen.queryByLabelText('取消修改目标')===null);
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
