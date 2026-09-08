import {Store,Conflict} from './store';
import {DomainError,INTENTS,ROLES,publicValue,now,uid,profileConfig,type Json} from './domain';
import {Settings,Gateway,type ModelInvoke} from './model';
import {parseDocument,parseText,exportCases,MAX_UPLOAD} from './documents';
import {eventsResponse} from './sse';

const json=(value:any,status=200,headers:Record<string,string>={})=>Response.json(value,{status,headers:{'Cache-Control':'no-store',...headers}});
function required(value:any,label:string,max=200){if(typeof value!=='string'||!value.trim()||value.length>max)throw new DomainError(`${label}不能为空，且不能超过 ${max} 字符`);return value.trim();}
function role(value:any){if(!ROLES.includes(value))throw new DomainError('来源 role 无效');return value;}
async function body(request:Request):Promise<Json>{let v:any;try{const text=await request.text();if(text.length>2_100_000)throw new DomainError('请求过大，请拆分内容',413);v=JSON.parse(text);}catch(e){if(e instanceof DomainError)throw e;throw new DomainError('请求必须为 JSON');}if(!v||Array.isArray(v)||typeof v!=='object')throw new DomainError('请求必须为 JSON 对象');return v;}
function message(value:Json){const intent=value.intent??'auto',mode=value.mode??'auto';if(!['auto',...INTENTS].includes(intent)||!['auto','hitp'].includes(mode))throw new DomainError('任务类型或运行模式无效');return {content:required(value.content,'消息',100000),intent,mode,as_requirement:value.as_requirement===true,profile_id:value.profile_id??null,source_ids:value.source_ids??null,artifact_id:value.artifact_id??null,selected_ids:value.selected_ids??null};}
async function visible(store:Store,id:string){const a=await store.get('artifact',id);if(!a._visible)throw new DomainError('未找到已发布的产物',404);return a;}
export function createHandler(override?:ModelInvoke,sseInterval=500){return async function handle(request:Request,env:Json,ctx:any){
 const url=new URL(request.url),parts=url.pathname.split('/').filter(Boolean),method=request.method;
 if(parts[0]!=='api')return env.ASSETS?.fetch(request)??new Response('Not found',{status:404});
 try{
  // Sites supplies this verified identity after its access control layer.
  const owner=request.headers.get('oai-authenticated-user-id');if(!owner)throw new DomainError('请先登录此站点',401);
  if(!['GET','HEAD'].includes(method)){
   const origin=request.headers.get('origin');if(origin&&new URL(origin).host!==url.host)throw new DomainError('拒绝跨站修改请求',403);
   if(request.headers.get('sec-fetch-site')==='cross-site')throw new DomainError('拒绝跨站修改请求',403);
   const size=Number(request.headers.get('content-length'));if(size>MAX_UPLOAD+65536)throw new DomainError('请求超过上传限制',413);
  }
  if(!env.DB||!env.BUCKET)throw new DomainError('云端存储尚未绑定',503);
  const store=new Store(env.DB,owner,env.BUCKET);await store.initialize();
  const settings=new Settings(store,env),gateway=new Gateway(settings),invoke=override??gateway.invoke.bind(gateway);
  const path=parts.slice(1),id=path[1];
  if(path[0]==='health')return json({status:'ok',version:'2.1.0',storage:'cloud',model_configured:!!override||!!(await settings.value()).model});
  if(path[0]==='settings'){
   if(path.length===1&&method==='GET')return json(await settings.public());
   if(path.length===1&&method==='PUT')return json(await settings.save(await body(request)));
   if(path[1]==='test'&&method==='POST'){
    const timeout=AbortSignal.timeout(3600*1000),signal=AbortSignal.any([request.signal,timeout]);
    try{const result=await invoke('connection_test',{},async()=>{},signal,async()=>{});return json({ok:result.ok===true,message:result.ok===true?'模型服务连接成功。':'模型没有返回预期的连接测试结果'});}catch(e){return json({ok:false,message:e instanceof DomainError?e.message:'模型服务连接失败，请检查地址和模型名称'});}
   }
  }
  if(path[0]==='projects'){
   if(path.length===1){if(method==='GET'){await store.ensureDefault();return json(await store.list('project'));}if(method==='POST'){const b=await body(request);return json(await store.transaction(tx=>tx.createProject(required(b.name,'项目名称'))));}}
   await store.get('project',id);
   if(path[2]==='profiles'){
    if(method==='GET')return json(await store.list('profile',{project_id:id}));
    if(method==='POST'){const b=await body(request),name=required(b.name,'Profile 名称'),config=profileConfig(b.config);return json(await store.transaction(async tx=>tx.put('profile',{id:uid('prof_'),project_id:id,name,config,version:1})));}
   }
   if(path[2]==='chats'){
    if(method==='GET')return json((await store.list('chat',{project_id:id})).sort((a,b)=>b.updated_at.localeCompare(a.updated_at)));
    if(method==='POST'){const b=await body(request);return json(await store.transaction(async tx=>tx.put('chat',{id:uid('chat_'),project_id:id,title:required(b.title??'新对话','标题'),created_at:now(),updated_at:now()})));}
   }
  }
  if(path[0]==='profiles'&&path.length===2&&method==='PUT'){
   const b=await body(request),name=required(b.name,'Profile 名称'),config=profileConfig(b.config);
   return json(await store.transaction(async tx=>{const old=await tx.get('profile',id);if(old.version!==b.expected_version)throw new Conflict();return tx.put('profile',{...old,name,config,version:old.version+1});}));
  }
  if(path[0]==='chats'){
   const chat=await store.get('chat',id);
   if(path.length===2&&method==='GET')return json({chat,messages:(await store.list('message',{chat_id:id})).sort((a,b)=>a.created_at.localeCompare(b.created_at)).map(publicValue),sources:(await store.list('source',{chat_id:id})).filter(s=>s._active).map(publicValue),runs:await Promise.all((await store.list('run',{chat_id:id})).sort((a,b)=>a.created_at.localeCompare(b.created_at)).map(r=>store.runView(r)))});
   if(path[2]==='sources'&&method==='POST'){
    let parsed:{text:string;chunks:Json[]},name:string,sourceRole:string,key:string|undefined,bytes:Uint8Array|undefined;const sourceId=uid('src_');
    if(path[3]==='text'){const b=await body(request);parsed=parseText(b.text);name=required(b.name??'粘贴的需求','来源名称');sourceRole=role(b.role??'primary');}
    else{const form=await request.formData(),file=form.get('file');if(!file||typeof file==='string')throw new DomainError('请选择上传文件');if(file.size>MAX_UPLOAD)throw new DomainError('文件超过 15 MB 限制',413);name=file.name.replace(/\\/g,'/').split('/').at(-1)!.slice(0,200)||'document.txt';sourceRole=role(form.get('role')??'primary');bytes=new Uint8Array(await file.arrayBuffer());parsed=await parseDocument(name,bytes);key=`uploads/${encodeURIComponent(owner)}/${sourceId}`;}
    if(key)await env.BUCKET.put(key,bytes,{httpMetadata:{contentType:'application/octet-stream'}});
    try{return json(publicValue(await store.transaction(tx=>tx.addSource(id,name,sourceRole,parsed.text,parsed.chunks,key,sourceId))));}catch(e){if(key)await env.BUCKET.delete(key);throw e;}
   }
   if(path[2]==='messages'&&method==='POST'){
    if(!override&&!(await settings.value()).model)throw new DomainError('请先在模型与设置中保存模型配置');const req=message(await body(request));
    const result=await store.transaction(async tx=>{const value={...req};if(req.as_requirement){const p=parseText(req.content),source=await tx.addSource(id,'消息中的需求正文','primary',p.text,p.chunks);if(value.source_ids)value.source_ids=[...value.source_ids,source.id];}return tx.createRun(id,value);});
    return json({message:publicValue(result.message),run:await store.runView(result.run)});
   }
  }
  if(path[0]==='sources'){
   const source=await store.get('source',id);
   if(method==='GET'){const chunks=(await store.evidence([id])).sort((a,b)=>Number(a.id.split('#P')[1])-Number(b.id.split('#P')[1]));return json({...publicValue(source),text:chunks.map(c=>c.text).join('\n\n'),chunks:chunks.map(({id,text,location})=>({id,text,location}))});}
   if(method==='DELETE'){await store.transaction(async tx=>{const latest=await tx.get('source',id);tx.put('source',{...latest,_active:false});});return json({ok:true});}
  }
  if(path[0]==='runs'){
   const run=await store.get('run',id);
   if(path.length===2&&method==='GET')return json(await store.runView(run));
   if(path[2]==='events'&&method==='GET')return eventsResponse(request,store,id,invoke,ctx,sseInterval);
   if(path[2]==='diagnostics'&&method==='GET'){
    const rows=await store.db.prepare("SELECT payload FROM tcg_events WHERE owner=? AND run_id=? AND kind='progress' ORDER BY id DESC LIMIT 200").bind(owner,id).all();
    return json({version:'2.1.0',run_id:id,chat_id:run.chat_id,status:run.status,stage:run.stage,created_at:run.created_at,updated_at:run.updated_at,runtime:{storage:'cloud',graph_thread_id:id,resume_policy:'open_page_checkpoint_resume'},context:{conversation_messages:run._conversation.length,history_limit:12,history_omitted:Math.max(0,run._history_total-run._conversation.length),source_count:run._source_ids.length,artifact_id:run._artifact_snapshot?.id??null},events:rows.results.reverse().map((r:Json)=>JSON.parse(r.payload)),retention:'Last 200 diagnostic events; request/response content is not included.'},200,url.searchParams.has('download')?{'Content-Disposition':`attachment; filename="${id}-diagnostics.json"`}:{});
   }
   if(path[2]==='model-calls'&&path[4]==='request'&&method==='GET')return json(await store.get('model_request',`request:${id}:${path[3]}`),200,url.searchParams.has('download')?{'Content-Disposition':`attachment; filename="${path[3]}-request.json"`}:{});
   if(method==='POST'&&['resume','retry','cancel','edit'].includes(path[2])){
    const action=path[2],b=await body(request);
    const result=await store.transaction(async tx=>{
     const latest=await tx.get('run',id);
     if(action==='cancel'){
      if(['completed','cancelled'].includes(latest.status))return latest;
      tx.leaseDeletes.add(id);tx.event(id,'progress',{at:now(),event:'run.cancelled',run_id:id});return tx.saveRun({...latest,status:'cancelled',stage:'cancelled',_pending_edit:null,_resume:null,interrupt:null});
     }
     if(action==='retry'){
      if(latest.status!=='failed')throw new DomainError('只有失败的任务可以重试',409);
      if((await tx.list('run',{chat_id:latest.chat_id})).some(r=>r.id!==id&&['queued','running','waiting'].includes(r.status)))throw new DomainError('此对话已有运行中的任务，请先完成或取消它',409);
      if(latest._failed_cache_key){const key=latest._failed_cache_key;const applied=await tx.maybe('cache',`cache:${id}:review_applied`);if(!(applied&&key.startsWith('case_review')))tx.remove(`cache:${id}:${key}`);}
      tx.event(id,'progress',{at:now(),event:'run.retried',run_id:id});return tx.saveRun({...latest,status:'queued',stage:'retrying',error:null});
     }
     if(latest.status!=='waiting'||!latest.interrupt||latest._pending_edit)throw new DomainError('任务当前不能接受确认或编辑，请等待当前操作完成',409);
     if(action==='resume'){
      if(latest.interrupt.type==='clarification')required(b.answer,'澄清答案',100000);else if(b.approved!==true)throw new DomainError('请确认场景后继续');
      tx.event(id,'progress',{at:now(),event:'run.resumed',run_id:id});return tx.saveRun({...latest,status:'queued',stage:'resuming',error:null,_resume:{interrupt_id:latest._interrupt_id,value:latest.interrupt.type==='clarification'?{answer:b.answer}:{approved:true}}});
     }
     if(latest.interrupt.type!=='scenario_review')throw new DomainError('只有场景确认阶段支持 AI 修改');
     const content=required(b.content,'编辑指令',2000000),artifact=await tx.get('artifact',latest.interrupt.artifact_id);
     if(b.selected_ids&&(!Array.isArray(b.selected_ids)||b.selected_ids.some((x:string)=>!artifact.items.some((i:Json)=>i.id===x))))throw new DomainError('所选条目无效');
     return tx.saveRun({...latest,stage:'paused_edit',error:null,_pending_edit:{id:uid('edit_'),artifact,content,selected_ids:b.selected_ids??null}});
    });return json(await store.runView(result));
   }
  }
  if(path[0]==='artifacts'){
   const art=await visible(store,id);
   if(path.length===2&&method==='GET')return json(publicValue(art));
   if(path.length===2&&method==='PUT'){const b=await body(request);return json(publicValue(await store.transaction(tx=>tx.revise(id,b.expected_revision,b.items))));}
   if(path[2]==='revisions'&&method==='GET'){
    if(path[3])return json(publicValue((await store.get('revision',`rev:${id}:${Number(path[3])}`)).value));
    return json((await store.list('revision')).filter(r=>r.artifact_id===id).sort((a,b)=>b.revision-a.revision).map(({revision,created_at,reason,diff})=>({revision,created_at,reason,diff})));
   }
   if(path[2]==='restore'&&method==='POST'){const b=await body(request),old=await store.get('revision',`rev:${id}:${b.revision}`);return json(publicValue(await store.transaction(tx=>tx.revise(id,b.expected_revision,old.value.items,`restore:${b.revision}`))));}
   if(path[2]==='export'&&method==='GET')return new Response(exportCases(art,url.searchParams.get('layout')??art._profile?.excel_layout??'case',url.searchParams.has('ids')?url.searchParams.get('ids')!.split(','):undefined),{headers:{'Content-Type':'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet','Content-Disposition':'attachment; filename="test-cases.xlsx"','Cache-Control':'no-store'}});
  }
  throw new DomainError('API 路由不存在',404);
 }catch(e){return json({detail:e instanceof DomainError?e.message:'云端请求失败，请重试或查看任务诊断'},e instanceof DomainError?e.status:500);}
};}
export default {fetch:createHandler()};
