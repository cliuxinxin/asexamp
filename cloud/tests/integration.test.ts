import {test} from 'node:test';
import assert from 'node:assert/strict';
import * as XLSX from 'xlsx';
import {zipSync,strToU8} from 'fflate';
import {createHandler} from '../server';
import {Store} from '../store';
import {Engine} from '../graph';
import {DomainError,type Json} from '../domain';
import {type ModelInvoke} from '../model';
import {parseDocument} from '../documents';
import {TestD1,TestR2} from './d1';

async function setup(invoke:ModelInvoke){
 const env={DB:new TestD1(),BUCKET:new TestR2(),TCG_SECRET_KEY:btoa('x'.repeat(32))},jobs:Promise<void>[]=[],handler=createHandler(invoke,5),store=new Store(env.DB,'alice',env.BUCKET);
 const response=(path:string,b?:any,method=b===undefined?'GET':'POST',owner='alice',signal?:AbortSignal)=>handler(new Request('https://test.example/api'+path,{method,headers:{...(owner?{'oai-authenticated-user-id':owner}:{}),...(b instanceof FormData?{}:{'Content-Type':'application/json'})},body:b===undefined?undefined:b instanceof FormData?b:JSON.stringify(b),signal}),env,{waitUntil:(p:Promise<void>)=>jobs.push(p)});
 const api=async(path:string,b?:any,method?:string)=>{const r=await response(path,b,method);const v=await r.json() as Json;assert.equal(r.status,200,JSON.stringify(v));return v;};
 const project=(await api('/projects'))[0],chat=await api(`/projects/${project.id}/chats`,{title:'Test'});
 const source=await api(`/chats/${chat.id}/sources/text`,{name:'需求',role:'primary',text:'用户输入正确密码后登录成功。错误密码显示错误提示。'});
 return {env,jobs,store,api,response,chat,source};
}
function model(respond:(task:string,c:Json)=>Json|Promise<Json>,calls:Json[]=[]):ModelInvoke{return async(task,context,onText,signal,record)=>{
 signal.throwIfAborted();calls.push({task,context});await record({task,provider:'fixture',model:'fixture',base_url:'https://fixture.example/v1',timeout_seconds:3600,messages:[{role:'system',content:'fixture contract'},{role:'user',content:JSON.stringify(context)}],parameters:{stream:true}});
 const value=await respond(task,context),text=JSON.stringify(value);for(let i=0;i<text.length;i+=20){signal.throwIfAborted();await onText(text.slice(i,i+20));}return value;
};}
function answer(task:string,c:Json):Json{
 const refs=c.evidence?.length?[c.evidence[0].id]:[];
 if(task==='route')return {intent:'generate_case'};
 if(task==='analyze_requirement')return {items:[{id:'r1',title:'登录',description:'验证密码',refs}],report:{questions:[],assumptions:[]}};
 if(task==='generate_scenarios')return {items:[{id:'s1',title:'正确密码',description:'成功登录',priority:'P0',refs}],has_more:false,next_cursor:null};
 if(['generate_cases','import_cases'].includes(task))return {items:[{id:'c1',title:'登录成功',type:'Business',priority:'P1',preconditions:'注册账户',scenario_id:c.scenarios?.[0]?.id??'',steps:[{action:'输入正确密码',expected:'登录成功'}],refs}],has_more:false,next_cursor:null};
 if(task==='review_cases')return {operations:[{op:'update',id:c.cases[0].id,item:{priority:'P0'}}],report:{summary:'通过'}};
 if(task==='query')return {answer:'正确密码可以登录',refs};
 if(task==='modify')return {operations:[{op:'update',id:c.artifact.items[0].id,item:{title:'修改后的标题'}}]};
 if(task==='learn_template')return {summary:'建议步骤行',config:{excel_layout:'step'}};
 if(task==='connection_test')return {ok:true};
 throw new Error('Unhandled '+task);
}
async function waitFor(fn:()=>Promise<boolean>,ms=5000){const end=Date.now()+ms;while(!await fn()){if(Date.now()>end)throw new Error('Condition timed out');await new Promise(r=>setTimeout(r,5));}}
async function startFeed(f:Awaited<ReturnType<typeof setup>>,id:string){const response=await f.response(`/runs/${id}/events`);assert.equal(response.status,200);const reader=response.body!.getReader();let text='';const reading=(async()=>{while(true){const row=await reader.read();if(row.done)break;text+=new TextDecoder().decode(row.value);}})();return {reader,reading,text:()=>text,close:async()=>{await reader.cancel();await reading;await Promise.all(f.jobs);}};}

test('HTTP → actual nine-node graph → HITP edits/resume → SSE/request inspection/XLSX and later turns',{timeout:15000},async()=>{
 const calls:Json[]=[],f=await setup(model(answer,calls)),created=await f.api(`/chats/${f.chat.id}/messages`,{content:'生成用例',intent:'auto',mode:'hitp'}),id=created.run.id,feed=await startFeed(f,id);
 await waitFor(async()=>(await f.store.get('run',id)).status==='waiting');
 const waiting=await f.api(`/runs/${id}`),a=await f.api(`/artifacts/${waiting.interrupt.artifact_id}`);
 const edited=a.items.map((i:Json)=>({...i,description:'人工确认的描述'}));await f.api(`/artifacts/${a.id}`,{expected_revision:a.revision,items:edited},'PUT');
 await f.api(`/runs/${id}/edit`,{content:'修改场景标题',selected_ids:[a.items[0].id]});
 await waitFor(async()=>!(await f.store.get('run',id))._pending_edit);
 assert.equal((await f.api(`/artifacts/${a.id}`)).items[0].title,'修改后的标题');
 await f.api(`/runs/${id}/resume`,{approved:true});await feed.reading;await Promise.all(f.jobs);
 const finished=await f.api(`/runs/${id}`);assert.equal(finished.status,'completed',finished.error);assert.match(feed.text(),/event: model_delta/);assert.match(feed.text(),/event: done/);
 const caseContext=calls.find(c=>c.task==='generate_cases')!.context;assert.equal(caseContext.scenarios[0].description,'人工确认的描述');assert.equal(caseContext.scenarios[0].title,'修改后的标题');
 const art=await f.api(`/artifacts/${finished.artifact_ids.at(-1)}`);assert.equal(art.items[0].priority,'P0');assert.equal(art.revision,2);
 const exportResponse=await f.response(`/artifacts/${art.id}/export?layout=step`),book=XLSX.read(await exportResponse.arrayBuffer());assert.ok(book.Sheets[book.SheetNames[0]]['A2']);
 const reqs=await f.store.list('model_request');assert.ok(reqs.length>=5);const req=await f.api(`/runs/${id}/model-calls/${reqs[0].call_id}/request`);assert.ok(req.messages[1].content);assert.equal(req.timeout_seconds,3600);
 const diagnostic=await f.api(`/runs/${id}/diagnostics`);assert.ok(!JSON.stringify(diagnostic).includes('正确密码'));
 const snapshot=await f.api(`/chats/${f.chat.id}`);assert.equal(snapshot.messages.filter((m:Json)=>m.role==='assistant').length,2);
 for(const intent of ['query','modify','learn_template','review_case']){
  const next=await f.api(`/chats/${f.chat.id}/messages`,{content:'继续 '+intent,intent,artifact_id:art.id});const stream=await startFeed(f,next.run.id);await stream.reading;await Promise.all(f.jobs);const r=await f.api(`/runs/${next.run.id}`);assert.equal(r.status,'completed',r.error);
  assert.ok((await f.store.get('run',r.id))._conversation.length>=3);
 }
 assert.equal((await f.store.list('profile')).length,1,'template suggestion must not overwrite profiles');
});

test('disconnect aborts unfinished call; a fresh SSE resumes without rerunning completed analysis',{timeout:12000},async()=>{
 let first=true,entered=false;const counts:Record<string,number>={};
 const f=await setup(async(task,c,onText,signal,record)=>{
  counts[task]=(counts[task]??0)+1;await record({messages:[],model:'fixture',task});
  if(task==='generate_scenarios'&&first){first=false;entered=true;await new Promise<void>((_,reject)=>{signal.addEventListener('abort',()=>reject(signal.reason),{once:true});});}
  const value=answer(task,c);await onText(JSON.stringify(value));return value;
 });
 const {run}=await f.api(`/chats/${f.chat.id}/messages`,{content:'生成场景',intent:'generate_scenario'}),feed=await startFeed(f,run.id);
 await waitFor(async()=>entered);await feed.close();assert.equal((await f.store.get('run',run.id)).status,'queued');
 const second=await startFeed(f,run.id);await second.reading;await Promise.all(f.jobs);assert.equal((await f.store.get('run',run.id)).status,'completed');assert.equal(counts.analyze_requirement,1);assert.equal(counts.generate_scenarios,2);assert.equal((await f.store.list('message')).filter(m=>m.role==='assistant').length,1);
});

test('failed model retry preserves prior nodes, repair fixes schema, and clarification persists across new graph instances',{timeout:15000},async()=>{
 let failing=true;const calls:Json[]=[];
 const f=await setup(model((task,c)=>{if(task==='analyze_requirement'){const v=answer(task,c);v.report.questions=['密码错误如何提示？'];return v;}if(task==='generate_scenarios'&&failing)throw new DomainError('fixture failure');if(task==='generate_cases'&&!c.validation_repair){const v=answer(task,c);v.items[0].steps[0].expected=123;return v;}return answer(task,c);},calls));
 const {run}=await f.api(`/chats/${f.chat.id}/messages`,{content:'生成用例',intent:'generate_case',mode:'hitp'});
 const drive=async()=>{const fence=await f.store.claim(run.id);assert.ok(fence);await new Engine(f.store,fence!,new AbortController().signal,model((task,c)=>{if(task==='analyze_requirement'){const v=answer(task,c);v.report.questions=['密码错误如何提示？'];return v;}if(task==='generate_scenarios'&&failing)throw new DomainError('fixture failure');if(task==='generate_cases'&&!c.validation_repair){const v=answer(task,c);v.items[0].steps[0].expected=123;return v;}return answer(task,c);},calls)).execute();await f.store.release(fence!);};
 await drive();assert.equal((await f.store.get('run',run.id)).interrupt.type,'clarification');await f.api(`/runs/${run.id}/resume`,{answer:'显示密码错误'});await drive();assert.equal((await f.store.get('run',run.id)).status,'failed');
 failing=false;await f.api(`/runs/${run.id}/retry`,{});await drive();assert.equal((await f.store.get('run',run.id)).interrupt.type,'scenario_review');await f.api(`/runs/${run.id}/resume`,{approved:true});await drive();const end=await f.store.get('run',run.id);assert.equal(end.status,'completed',end.error);
 assert.equal((await f.store.list('source')).filter(s=>s.role==='clarification').length,1);assert.equal(calls.filter(c=>c.task==='analyze_requirement').length,2);assert.equal(calls.filter(c=>c.task==='generate_cases').length,2);
});

test('two SSE viewers share one execution; cancellation rejects delayed output',{timeout:12000},async()=>{
 let entered=0;const f=await setup(async(task,c,onText,signal)=>{entered++;await new Promise<void>((_,reject)=>signal.addEventListener('abort',()=>reject(signal.reason),{once:true}));return answer(task,c);});
 const {run}=await f.api(`/chats/${f.chat.id}/messages`,{content:'分析',intent:'review_requirement'}),one=await startFeed(f,run.id),two=await startFeed(f,run.id);await waitFor(async()=>entered===1);
 await f.api(`/runs/${run.id}/cancel`,{});await one.close();await two.close();assert.equal(entered,1);assert.equal((await f.store.get('run',run.id)).status,'cancelled');assert.equal((await f.store.list('artifact')).length,0);
});

test('settings encrypt once, retain on same endpoint, clear on endpoint change; every owner is isolated',async()=>{
 const f=await setup(model(answer));const config={provider:'openai',base_url:'https://model.example/v1',model:'test',api_key:'fixture-secret'};
 const saved=await f.api('/settings',config,'PUT');assert.equal(saved.has_api_key,true);assert.ok(!JSON.stringify(saved).includes('fixture-secret'));assert.ok(!JSON.stringify(await f.store.get('settings','settings')).includes('fixture-secret'));
 await f.api('/settings',{...config,api_key:undefined},'PUT');assert.equal((await f.api('/settings')).has_api_key,true);
 await f.api('/settings',{...config,api_key:undefined,base_url:'https://another.example/v1'},'PUT');assert.equal((await f.api('/settings')).has_api_key,false);
 const unauth=await f.response('/projects',undefined,'GET','');assert.equal(unauth.status,401);
 for(const path of [`/chats/${f.chat.id}`,`/sources/${f.source.id}`])assert.equal((await f.response(path,undefined,'GET','bob')).status,404);
 assert.equal((await f.response('/settings',{...config,base_url:'http://localhost:1234'},'PUT')).status,400);
});

test('DOCX/XLSX uploads preserve evidence locations and formula-safe export',async()=>{
 const f=await setup(model(answer));const docx=zipSync({'word/document.xml':strToU8('<w:document xmlns:w="x"><w:body><w:p><w:r><w:t>登录需求</w:t></w:r></w:p><w:tbl><w:tr><w:tc><w:p><w:r><w:t>表格内容</w:t></w:r></w:p></w:tc></w:tr></w:tbl></w:body></w:document>')});
 const form=new FormData();form.set('file',new File([new Uint8Array(docx).buffer],'需求.docx'));form.set('role','primary');const src=await f.api(`/chats/${f.chat.id}/sources`,form);assert.equal(f.env.BUCKET.values.size,1);const content=await f.api(`/sources/${src.id}`);assert.match(content.text,/表格内容/);assert.match(content.chunks[0].location,/DOCX/);
 const book=XLSX.utils.book_new();XLSX.utils.book_append_sheet(book,XLSX.utils.aoa_to_sheet([['Requirement'],['登录成功']]),'需求');const parsed=await parseDocument('需求.xlsx',new Uint8Array(XLSX.write(book,{type:'array',bookType:'xlsx'})));assert.match(parsed.text,/登录成功/);assert.match(parsed.chunks[1].location,/行 2/);
});

test('analysis batches and paginated scenario/case generation keep all completed pages',{timeout:12000},async()=>{
 const calls:Json[]=[],f=await setup(model((task,c)=>{
  const value=answer(task,c);
  if(['generate_scenarios','generate_cases'].includes(task)){
   const page=c.cursor?2:1;value.items[0].id=(task==='generate_cases'?'c':'s')+page;value.items[0].title+=' '+page;
   if(task==='generate_cases')value.items[0].scenario_id=c.scenarios[page-1].id;
   value.has_more=page===1;value.next_cursor=page===1?'page2':null;
  }
  return value;
 },calls));
 const large=await f.api(`/chats/${f.chat.id}/sources/text`,{name:'分批需求',text:Array.from({length:12},(_,i)=>`第 ${i+1} 段 `+'需求'.repeat(2000)).join('\n\n')});
 const evidence=await f.store.evidence([large.id]);assert.match(evidence[10].location,/段落 6/);
 const {run}=await f.api(`/chats/${f.chat.id}/messages`,{content:'完整生成',intent:'generate_case'}),feed=await startFeed(f,run.id);await feed.reading;
 const result=await f.api(`/runs/${run.id}`);assert.equal(result.status,'completed',result.error);const cases=await f.api(`/artifacts/${result.artifact_ids.at(-1)}`);assert.equal(cases.items.length,2);assert.ok(calls.filter(c=>c.task==='analyze_requirement').length>1);assert.equal(calls.filter(c=>c.task==='generate_scenarios').length,2);assert.equal(calls.filter(c=>c.task==='generate_cases').length,2);
});

test('retry cannot start a second active run in the same chat',async()=>{
 const f=await setup(model(answer));const {run}=await f.api(`/chats/${f.chat.id}/messages`,{content:'first',intent:'query'});await f.store.updateRun(run.id,{status:'failed'});
 await f.api(`/chats/${f.chat.id}/messages`,{content:'second',intent:'query'});assert.equal((await f.response(`/runs/${run.id}/retry`,{})).status,409);
});

test('text PDF parsing returns text with page citations',async()=>{
 const content='BT /F1 12 Tf 50 750 Td (Login requirement) Tj ET';
 const objects=['<< /Type /Catalog /Pages 2 0 R >>','<< /Type /Pages /Kids [3 0 R] /Count 1 >>','<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>','<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>',`<< /Length ${content.length} >>\nstream\n${content}\nendstream`];
 let pdf='%PDF-1.4\n';const offsets=[0];objects.forEach((o,i)=>{offsets.push(pdf.length);pdf+=`${i+1} 0 obj\n${o}\nendobj\n`;});const xref=pdf.length;pdf+=`xref\n0 ${objects.length+1}\n0000000000 65535 f \n`+offsets.slice(1).map(n=>String(n).padStart(10,'0')+' 00000 n \n').join('')+`trailer\n<< /Size ${objects.length+1} /Root 1 0 R >>\nstartxref\n${xref}\n%%EOF`;
 const parsed=await parseDocument('requirement.pdf',new TextEncoder().encode(pdf));assert.match(parsed.text,/Login requirement/);assert.match(parsed.chunks[0].location,/PDF 第 1 页/);
});
