import {JSDOM} from 'jsdom';
import {test} from 'node:test';
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
const {render,fireEvent,screen,waitFor,cleanup,within}=await import('@testing-library/react');
const {App}=await import('../src/App');
test('failed task exposes the small log without opening run details',async()=>{
 const {RunCard}=await import('../src/RunCard');
 try{
  render(<RunCard run={{id:'failed-run',status:'failed',intent:'generate_case',mode:'auto',stage:'failed',updated_at:'2026-09-09',artifact_ids:[],experience:'reliable',graph_version:7,error:'JSON 语法错误'}} onChanged={()=>{}} onTarget={()=>{}}/>);
  const link=screen.getByRole('link',{name:/下载失败步骤日志/});
  assert.equal(link.getAttribute('href'),'/api/runs/failed-run/failed-step');
  assert.equal(link.hasAttribute('download'),true);
  assert.equal(link.closest('details'),null);
 }finally{cleanup();}
});

test('confirmation is disabled while AI is editing the waiting artifact',async()=>{
 const {RunCard}=await import('../src/RunCard');
 try{
  render(<RunCard interactionBusy={true} run={{id:'editing-run',status:'waiting',intent:'generate_case',mode:'hitp',stage:'scenario_review',updated_at:'2026-09-10',artifact_ids:[],experience:'reliable',graph_version:7,edit_in_progress:true,interrupt:{type:'scenario_review'}}} onChanged={()=>{}} onTarget={()=>{}}/>);
  assert.equal(screen.getByRole('button',{name:'确认场景，继续生成用例'}).hasAttribute('disabled'),true);
  assert.ok(screen.getByText('正在处理当前请求，完成后可继续确认。'));
 }finally{cleanup();}
});
// This narrow component smoke uses deterministic API state, without browser/network dependencies.
test('wide conversation shows inline results and explicit editing targets them',async()=>{
 let generated=false,modified=false;const requests:any[]=[];
 const artifact={id:'a1',type:'cases',title:'测试用例',revision:1,items:[{id:'TC-1',title:'Login',type:'Business',priority:'P1',scenario_id:'',preconditions:'Account',steps:[{action:'Login',expected:'Home'}],refs:['s1#P1']}]};
 const response=(v:any)=>Promise.resolve(new Response(JSON.stringify(v),{status:200,headers:{'Content-Type':'application/json'}}));
 const originalFetch=globalThis.fetch;
 globalThis.fetch=(async(url:any,init:any)=>{
  const path=String(url);
  if(path==='/api/projects')return response([{id:'p1',name:'Smoke project'}]);
  if(path==='/api/settings')return response({provider:'openai',model:'smoke'});
  if(path.endsWith('/profiles'))return response([{id:'profile',name:'Default',config:{}}]);
  if(path==='/api/projects/p1/chats')return response([{id:'c1',project_id:'p1',title:'Smoke'}]);
  if(path==='/api/chats/c1')return response({chat:{id:'c1',title:'Smoke'},sources:[],runs:generated?[{id:'r1',status:'completed',experience:'reliable'}]:[],messages:generated?[{id:'stage1',role:'assistant',content:'需求理解与业务图已完成',metadata:{run_id:'r1',stage:'understand',stage_artifact_id:'analysis1',stage_revision:1}},{id:'m1',role:'assistant',content:'已完成',metadata:{run_id:'r1',artifact_ids:['a1']}}]:[]});
  if(path==='/api/chats/c1/interpret')return response({handled:false,intent:'generate_case'});
  if(path==='/api/chats/c1/messages'){const body=JSON.parse(init.body);requests.push(body);if(generated){modified=true;artifact.revision++;artifact.items[0].title='Updated login';}generated=true;return response({});}
  if(path==='/api/artifacts/a1/export-options')return response({snapshot:{},snapshot_check:{columns:[],missing:[]},profiles:[]});
  if(path==='/api/artifacts/a1')return response(artifact);
  if(path==='/api/artifacts/analysis1/revisions/1')return response({id:'analysis1',type:'analysis',title:'需求理解',revision:1,items:[{id:'REQ1',title:'历史登录规则'}],report:{summary:'阶段摘要'}});
  throw new Error('Unexpected API '+path);
 }) as typeof fetch;
 try{
  render(<App/>);
  await waitFor(()=>assert.equal(screen.getByRole('button',{name:'发送消息',exact:true}).hasAttribute('disabled'),true));
  await screen.findByText('Smoke project');
  assert.equal(screen.queryByLabelText('生成流程'),null);
  await waitFor(()=>assert.equal(screen.getByRole('button',{name:'新建生成会话'}).hasAttribute('disabled'),false));
  fireEvent.change(screen.getByLabelText('聊天输入'),{target:{value:'用户登录需求'}});
  await waitFor(()=>assert.equal(screen.getByRole('button',{name:'发送消息',exact:true}).hasAttribute('disabled'),false));
  fireEvent.click(screen.getByRole('button',{name:'发送消息',exact:true}));
  await screen.findByText('Login');
  assert.equal(screen.getByRole('button',{name:/执行过程/}).getAttribute('aria-expanded'),'true');
  const stage=screen.getByText('查看需求理解与业务图 · v1').closest('details')!;
  stage.open=true;fireEvent(stage,new Event('toggle'));
  await screen.findByText('历史登录规则');
  assert.equal(screen.queryByRole('region',{name:'用例工作区'}),null);
  assert.equal(requests[0].intent,'generate_case');assert.equal(requests[0].as_requirement,false);
  fireEvent.click(screen.getByRole('button',{name:'让 AI 修改此结果'}));
  fireEvent.change(screen.getByLabelText('聊天输入'),{target:{value:'修改标题'}});
  fireEvent.click(screen.getByRole('button',{name:'发送消息',exact:true}));
  await waitFor(()=>assert.equal(modified,true));
  assert.equal(requests[1].intent,'modify');assert.equal(requests[1].artifact_id,'a1');
  fireEvent.click(screen.getByRole('button',{name:'导出 Excel'}));
  await screen.findByRole('button',{name:'下载 XLSX'});
 }finally{cleanup();globalThis.fetch=originalFetch;}
});

test('source confirmation explicitly sends selected file IDs',async()=>{
 const {RunCard}=await import('../src/RunCard');
 const originalFetch=globalThis.fetch;let body:any;
 globalThis.fetch=(async(url:any,init:any)=>{assert.equal(String(url),'/api/runs/r1/resume');body=JSON.parse(init.body);return new Response('{}',{status:200,headers:{'Content-Type':'application/json'}});}) as typeof fetch;
 try{
  render(<RunCard run={{id:'r1',status:'waiting',intent:'generate_case',mode:'hitp',stage:'source_review',updated_at:'2026-09-09',artifact_ids:[],experience:'reliable',graph_version:7,interrupt:{type:'source_review',message:'请选择需求文件',sources:[{id:'s1',name:'PHFSD.docx',role:'example'}]}}} onChanged={()=>{}} onTarget={()=>{}}/>);
  assert.equal(screen.getByRole('button',{name:'确认资料，继续当前任务'}).hasAttribute('disabled'),true);
  assert.equal(screen.getByRole('button',{name:/执行过程/}).getAttribute('aria-expanded'),'true');
  fireEvent.click(screen.getByRole('checkbox'));
  fireEvent.click(screen.getByRole('button',{name:'确认资料，继续当前任务'}));
  await waitFor(()=>assert.deepEqual(body.source_ids,['s1']));
 }finally{cleanup();globalThis.fetch=originalFetch;}
});


test('template column definition can be edited without losing its header',async()=>{
 const {ProfileEditor}=await import('../src/ProfileEditor');let saved:any;
 try{
  render(<ProfileEditor value={JSON.stringify({excel_columns:[{field:'test_data',header:'测试数据',definition:'原定义'}]})} onChange={v=>{saved=JSON.parse(v);}}/>);
  fireEvent.change(screen.getByLabelText('第 1 列定义'),{target:{value:'字段=值'}});
  assert.deepEqual(saved.excel_columns,[{field:'test_data',header:'测试数据',definition:'字段=值'}]);
 }finally{cleanup();}
});

test('export checks arbitrary template columns and explains unresolved fields',async()=>{
 const {ArtifactCard}=await import('../src/ArtifactCard');const originalFetch=globalThis.fetch;let request:any,changed=false;
 const result={id:'case-desc',type:'cases',title:'测试用例',revision:3,items:[{id:'C1',title:'登录',type:'Business',priority:'P1',steps:[{action:'登录',expected:'首页'}],refs:[]}]};
 globalThis.fetch=(async(url:any,init:any)=>{
  const path=String(url);let value:any;
  if(path==='/api/artifacts/case-desc')value=result;
  else if(path.endsWith('/export-options'))value={snapshot:{excel_columns:[{field:'validation_goal',header:'验证目的'}]},snapshot_check:{columns:[{field:'validation_goal',header:'验证目的',value_source:'ai',required:true}],missing:[{id:'C1',field:'validation_goal',header:'验证目的',reason:'需要补充字段业务定义'}],manual_columns:['实际结果']},profiles:[]};
  else if(path.endsWith('/complete-fields')){request=JSON.parse(init.body);value={run:{id:'fill1'}};}
  else throw new Error(path);
  return new Response(JSON.stringify(value),{status:200,headers:{'Content-Type':'application/json'}});
 }) as typeof fetch;
 try{
  render(<ArtifactCard id="case-desc" refreshKey="one" onTarget={()=>{}} onChanged={()=>{changed=true;}}/>);
  fireEvent.click(await screen.findByRole('button',{name:'导出 Excel'}));
  const complete=await screen.findByRole('button',{name:'补全模板字段'});
  assert.equal(screen.getByRole('button',{name:'下载 XLSX'}).hasAttribute('disabled'),true);
  assert.ok(screen.getByText(/需要补充字段业务定义/));
  assert.ok(screen.getByText(/没有填写时允许留空/));
  fireEvent.click(screen.getByRole('button',{name:'填写缺失字段'}));
  const input=await screen.findByLabelText('条目 C1 验证目的');
  fireEvent.change(input,{target:{value:'人工补充的验证目的'}});
  assert.equal((input as HTMLTextAreaElement).value,'人工补充的验证目的');
  fireEvent.click(screen.getByRole('button',{name:'关闭'}));
  fireEvent.click(screen.getByRole('button',{name:'导出 Excel'}));
  fireEvent.click(await screen.findByRole('button',{name:'补全模板字段'}));
  await waitFor(()=>assert.equal(changed,true));
  assert.deepEqual(request,{expected_revision:3});
 }finally{cleanup();globalThis.fetch=originalFetch;}
});
