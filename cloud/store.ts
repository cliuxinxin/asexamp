import {ACTIVE,DomainError,LeaseLost,copy,now,uid,publicValue,profileConfig,validateItems,type Json} from './domain';
export type Fence = {runId:string;token:string};
const LIMIT=1_500_000;
export function encode(value:any){const text=JSON.stringify(value);if(new TextEncoder().encode(text).byteLength>LIMIT)throw new DomainError('单个记录超过云端存储容量，请拆分需求；内容未截断或保存',413);return text;}
export class Conflict extends DomainError {constructor(){super('数据已更新，请刷新重试',409);}}
export class Store {
  constructor(public db:any,public owner:string,public bucket?:any){}
  async initialize(){await this.db.prepare('INSERT OR IGNORE INTO tcg_owners(owner,revision) VALUES(?,0)').bind(this.owner).run();}
  async get(kind:string,id:string):Promise<Json>{const row=await this.db.prepare('SELECT payload FROM tcg_records WHERE owner=? AND id=? AND kind=?').bind(this.owner,id,kind).first();if(!row)throw new DomainError('未找到请求的资源',404);return JSON.parse(row.payload);}
  async maybe(kind:string,id:string){try{return await this.get(kind,id);}catch(e){if(e instanceof DomainError&&e.status===404)return null;throw e;}}
  async list(kind:string,scope:Json={}):Promise<Json[]>{
    let sql='SELECT payload FROM tcg_records WHERE owner=? AND kind=?';const args:any[]=[this.owner,kind];
    for(const key of ['project_id','chat_id'])if(scope[key]!==undefined){sql+=` AND ${key}=?`;args.push(scope[key]);}
    const result=await this.db.prepare(sql).bind(...args).all();return result.results.map((r:any)=>JSON.parse(r.payload));
  }
  async transaction<T>(fn:(tx:Tx)=>Promise<T>,fence?:Fence):Promise<T>{
    for(let attempt=0;attempt<5;attempt++){
      const row=await this.db.prepare('SELECT revision FROM tcg_owners WHERE owner=?').bind(this.owner).first();
      if(!row)throw new DomainError('云端存储尚未初始化',503);
      if(fence)await this.checkFence(fence);
      const tx=new Tx(this,row.revision,fence);const value=await fn(tx);
      if(await tx.commit())return value;
      if(fence)await this.checkFence(fence);
    }
    throw new Conflict();
  }
  async fencedBatch(fence:Fence,build:(guard:string,args:any[])=>any[]){
    const gate=uid('cp_');const auth=this.db.prepare('UPDATE tcg_owners SET revision=revision+1,gate=? WHERE owner=? AND EXISTS(SELECT 1 FROM tcg_leases WHERE owner=? AND run_id=? AND token=? AND expires>unixepoch()*1000)').bind(gate,this.owner,this.owner,fence.runId,fence.token);
    const result=await this.db.batch([auth,...build('EXISTS(SELECT 1 FROM tcg_owners WHERE owner=? AND gate=?)',[this.owner,gate])]);if(!result[0].meta.changes)throw new LeaseLost();
  }
  async ensureDefault(){if((await this.list('project')).length)return;await this.transaction(async tx=>{if((await tx.list('project')).length)return;await tx.createProject('默认项目');});}
  async checkFence(fence:Fence){const row=await this.db.prepare('SELECT token FROM tcg_leases WHERE owner=? AND run_id=? AND token=? AND expires>unixepoch()*1000').bind(this.owner,fence.runId,fence.token).first();if(!row)throw new LeaseLost();}
  async claim(runId:string):Promise<Fence|null>{
    const token=uid('lease_');
    const r=await this.db.prepare(`INSERT INTO tcg_leases(owner,run_id,token,expires)
      SELECT owner,id,?,unixepoch()*1000+45000 FROM tcg_records WHERE owner=? AND id=? AND kind='run'
      AND (json_extract(payload,'$.status') IN ('queued','running') OR (json_extract(payload,'$.status')='waiting' AND json_extract(payload,'$._pending_edit') IS NOT NULL))
      ON CONFLICT(owner,run_id) DO UPDATE SET token=excluded.token,expires=excluded.expires WHERE tcg_leases.expires<=unixepoch()*1000`).bind(token,this.owner,runId).run();
    return r.meta.changes?{runId,token}:null;
  }
  async renew(fence:Fence){const r=await this.db.prepare('UPDATE tcg_leases SET expires=unixepoch()*1000+45000 WHERE owner=? AND run_id=? AND token=? AND expires>unixepoch()*1000').bind(this.owner,fence.runId,fence.token).run();if(!r.meta.changes)throw new LeaseLost();}
  async release(fence:Fence){await this.db.prepare('DELETE FROM tcg_leases WHERE owner=? AND run_id=? AND token=?').bind(this.owner,fence.runId,fence.token).run();}
  async event(runId:string,kind:string,data:Json,fence?:Fence){
    const args:any[]=[this.owner,runId,kind,encode(data),now()];let guard='';
    if(fence){guard=' WHERE EXISTS(SELECT 1 FROM tcg_leases WHERE owner=? AND run_id=? AND token=? AND expires>unixepoch()*1000)';args.push(this.owner,fence.runId,fence.token);}
    const r=await this.db.prepare('INSERT INTO tcg_events(owner,run_id,kind,payload,created_at) SELECT ?,?,?,?,?'+guard).bind(...args).run();
    if(fence&&!r.meta.changes)throw new LeaseLost();
  }
  async events(runId:string,after=0){const r=await this.db.prepare('SELECT id,kind,payload FROM tcg_events WHERE owner=? AND run_id=? AND id>? ORDER BY id LIMIT 200').bind(this.owner,runId,after).all();return r.results.map((e:any)=>({id:e.id,kind:e.kind,data:JSON.parse(e.payload)}));}
  async runView(run:Json){const latest=await this.db.prepare("SELECT payload FROM tcg_events WHERE owner=? AND run_id=? AND kind='progress' ORDER BY id DESC LIMIT 1").bind(this.owner,run.id).first();return {...publicValue(run),runtime:'cloud',...(latest?{diagnostic:JSON.parse(latest.payload)}:{})};}
  async evidence(sourceIds:string[]):Promise<Json[]>{const groups=await Promise.all(sourceIds.map(async id=>{const source=await this.get('source',id);return (await this.list('chunk',{chat_id:source.chat_id})).filter(c=>c.source_id===id).sort((a,b)=>Number(a.id.split('#P')[1])-Number(b.id.split('#P')[1]));}));return groups.flat();}
  async cache(runId:string,key:string){return (await this.maybe('cache',`cache:${runId}:${key}`))?.value??null;}
  async setCache(runId:string,key:string,value:any,fence:Fence){await this.transaction(async tx=>{await tx.assertRunning(runId);tx.put('cache',{id:`cache:${runId}:${key}`,run_id:runId,value});},fence);return value;}
  async updateRun(runId:string,changes:Json,fence?:Fence){return this.transaction(async tx=>{const run=await tx.get('run',runId);if(run.status==='cancelled'&&changes.status!=='cancelled')throw new LeaseLost();return tx.saveRun({...run,...changes});},fence);}
}
export class Tx {
  staged=new Map<string,{kind:string;value:Json}>();events:{runId:string;kind:string;data:Json}[]=[];deletes=new Set<string>();leaseDeletes=new Set<string>();
  constructor(public store:Store,public revision:number,public fence?:Fence){}
  async get(kind:string,id:string):Promise<Json>{if(this.deletes.has(id))throw new DomainError('未找到请求的资源',404);const staged=this.staged.get(id);if(staged){if(staged.kind!==kind)throw new DomainError('资源类型不符',404);return copy(staged.value);}return this.store.get(kind,id);}
  async maybe(kind:string,id:string){try{return await this.get(kind,id);}catch(e){if(e instanceof DomainError&&e.status===404)return null;throw e;}}
  async list(kind:string,scope:Json={}):Promise<Json[]>{const rows=new Map((await this.store.list(kind,scope)).map(row=>[row.id,row]));for(const [id,row] of this.staged)if(row.kind===kind&&Object.entries(scope).every(([k,v])=>row.value[k]===v))rows.set(id,copy(row.value));for(const id of this.deletes)rows.delete(id);return [...rows.values()];}
  put(kind:string,value:Json){encode(value);this.staged.set(value.id,{kind,value:copy(value)});this.deletes.delete(value.id);return value;}
  remove(id:string){this.staged.delete(id);this.deletes.add(id);}
  event(runId:string,kind:string,data:Json){this.events.push({runId,kind,data:copy(data)});}
  saveRun(run:Json){run.updated_at=now();this.put('run',run);this.event(run.id,'update',publicValue(run));return run;}
  async assertRunning(id:string){const run=await this.get('run',id);if(!['queued','running'].includes(run.status)&&!(run.status==='waiting'&&run._pending_edit))throw new LeaseLost();return run;}
  async createProject(name:string){const project=this.put('project',{id:uid('prj_'),name,created_at:now()});this.put('profile',{id:uid('prof_'),project_id:project.id,name:'Default',version:1,config:profileConfig({})});return project;}
  async addSource(chatId:string,name:string,role:string,text:string,chunks:Json[],fileKey?:string,sourceId=uid('src_')){
    const chat=await this.get('chat',chatId);const source=this.put('source',{id:sourceId,project_id:chat.project_id,chat_id:chatId,name,role,characters:text.length,created_at:now(),_active:true,_file:fileKey??null});
    chunks.forEach((chunk,i)=>this.put('chunk',{id:`${sourceId}#P${i+1}`,project_id:chat.project_id,chat_id:chatId,source_id:sourceId,role,...chunk}));return source;
  }
  async evidence(sourceIds:string[]){const groups=await Promise.all(sourceIds.map(async id=>{const source=await this.get('source',id);return (await this.list('chunk',{chat_id:source.chat_id})).filter(c=>c.source_id===id).sort((a,b)=>Number(a.id.split('#P')[1])-Number(b.id.split('#P')[1]));}));return groups.flat();}
  async createRun(chatId:string,request:Json){
    const chat=await this.get('chat',chatId);const runs=await this.list('run',{chat_id:chatId});if(runs.some(r=>ACTIVE.includes(r.status)))throw new DomainError('此对话已有运行中的任务，请先继续、取消或等待完成',409);
    const profile=request.profile_id?await this.get('profile',request.profile_id):(await this.list('profile',{project_id:chat.project_id}))[0];if(!profile||profile.project_id!==chat.project_id)throw new DomainError('Profile 不属于当前项目');
    const sources=request.source_ids??(await this.list('source',{chat_id:chatId})).filter(s=>s._active).map(s=>s.id);
    if(!Array.isArray(sources)||new Set(sources).size!==sources.length)throw new DomainError('来源必须为不重复的数组');
    for(const id of sources){const src=await this.get('source',id);if(src.project_id!==chat.project_id||!src._active)throw new DomainError('来源不属于当前项目或已停用');}
    let artifact:Json|null=null;
    if(request.artifact_id)artifact=await this.get('artifact',request.artifact_id);
    else if(['auto','query','modify','review_case','learn_template'].includes(request.intent)){
      const visible=(await this.list('artifact',{chat_id:chatId})).filter(a=>a._visible&&(request.intent!=='review_case'||a.type==='cases')).sort((a,b)=>a.created_at.localeCompare(b.created_at));artifact=visible.at(-1)??null;if(artifact)request.artifact_id=artifact.id;
    }
    if(artifact&&(artifact.project_id!==chat.project_id||artifact.chat_id!==chatId))throw new DomainError('产物不属于当前对话');
    if(request.selected_ids&&(!artifact||!Array.isArray(request.selected_ids)||request.selected_ids.some((id:string)=>!artifact!.items.some((i:Json)=>i.id===id))))throw new DomainError('所选条目不属于当前产物');
    const runId=uid('run_');const message=this.put('message',{id:uid('msg_'),project_id:chat.project_id,chat_id:chatId,role:'user',content:request.content,created_at:now(),metadata:{run_id:runId}});
    const history=(await this.list('message',{chat_id:chatId})).sort((a,b)=>a.created_at.localeCompare(b.created_at)||a.id.localeCompare(b.id));
    const run=this.saveRun({id:runId,chat_id:chatId,project_id:chat.project_id,status:'queued',intent:request.intent,mode:request.mode,stage:'queued',created_at:now(),artifact_ids:[],_request:request,_profile:profile.config,_profile_id:profile.id,_source_ids:sources,_artifact_snapshot:artifact,_artifact_source_ids:artifact?._source_ids??[],_conversation:history.slice(-12).map(m=>({role:m.role,content:m.content,metadata:m.metadata})),_history_total:history.length,_resume:null});
    this.put('chat',{...chat,updated_at:now()});return {message,run};
  }
  async artifact(runId:string,key:string,type:string,title:string,items:Json[],report?:Json){
    const run=await this.assertRunning(runId);const cached=await this.maybe('cache',`cache:${runId}:${key}`);if(cached)return this.get('artifact',cached.value.id);
    validateItems(type,items,Object.fromEntries((await this.evidence(run._source_ids)).map(e=>[e.id,e])));
    const art=this.put('artifact',{id:uid('art_'),chat_id:run.chat_id,project_id:run.project_id,type,title,revision:1,items,created_at:now(),_source_ids:run._source_ids,_profile:run._profile,_visible:false,...(report?{report}:{})});
    this.put('revision',{id:`rev:${art.id}:1`,artifact_id:art.id,revision:1,created_at:now(),reason:'generated',diff:{added:items.map(i=>i.id),updated:[],deleted:[]},value:art});
    this.put('cache',{id:`cache:${runId}:${key}`,value:{id:art.id}});return art;
  }
  async revise(id:string,expected:number,items:Json[],reason='manual_edit',runId?:string,key?:string){
    if(runId){await this.assertRunning(runId);if(key&&await this.maybe('cache',`cache:${runId}:${key}`))return this.get('artifact',id);}
    const old=await this.get('artifact',id);if(old.revision!==expected)throw new DomainError('产物已更新，请刷新后再保存',409);
    const run=runId?await this.get('run',runId):null;const sources=[...new Set([...old._source_ids,...(run?._source_ids??[])])];validateItems(old.type,items,Object.fromEntries((await this.evidence(sources)).map(e=>[e.id,e])));
    const before=new Map(old.items.map((i:Json)=>[i.id,i])),after=new Map(items.map(i=>[i.id,i]));
    const diff={added:[...after.keys()].filter(id=>!before.has(id)),deleted:[...before.keys()].filter(id=>!after.has(id)),updated:[...after.keys()].filter(id=>before.has(id)&&JSON.stringify(before.get(id))!==JSON.stringify(after.get(id)))};
    const art=this.put('artifact',{...old,items,revision:expected+1,_source_ids:sources});
    this.put('revision',{id:`rev:${id}:${art.revision}`,artifact_id:id,revision:art.revision,created_at:now(),reason,diff,value:art});
    if(runId&&key)this.put('cache',{id:`cache:${runId}:${key}`,value:{id}});return art;
  }
  async publish(runId:string,ids:string[],content='已完成，请查看下方结果。',waiting=false,proposal?:Json){
    const run=await this.assertRunning(runId);const key=`cache:${runId}:published:${ids.join(':')}:${waiting?'waiting':'final'}`;
    if(!await this.maybe('cache',key)){
      for(const id of ids){const art=await this.get('artifact',id);this.put('artifact',{...art,_visible:true});}
      this.put('message',{id:uid('msg_'),chat_id:run.chat_id,project_id:run.project_id,role:'assistant',content,created_at:now(),metadata:{run_id:runId,artifact_ids:ids,...(proposal?{proposal}:{})}});
      this.put('cache',{id:key,value:{done:true}});
    }
    run.artifact_ids=[...new Set([...run.artifact_ids,...ids])];if(!waiting){run.status='completed';run.stage='completed';delete run.interrupt;delete run.error;}return this.saveRun(run);
  }
  async commit():Promise<boolean>{
    const s=this.store;const gate=uid('tx_');let auth='owner=? AND revision=?';const authArgs:any[]=[s.owner,this.revision];
    if(this.fence){auth+=' AND EXISTS(SELECT 1 FROM tcg_leases WHERE owner=? AND run_id=? AND token=? AND expires>unixepoch()*1000)';authArgs.push(s.owner,this.fence.runId,this.fence.token);}
    // Authorize once at database execution time. The batch keeps this marker stable.
    const statements:any[]=[s.db.prepare(`UPDATE tcg_owners SET revision=revision+1,gate=? WHERE ${auth}`).bind(gate,...authArgs)];
    const guard='EXISTS(SELECT 1 FROM tcg_owners WHERE owner=? AND gate=?)',args=[s.owner,gate];
    for(const id of this.deletes)statements.push(s.db.prepare(`DELETE FROM tcg_records WHERE owner=? AND id=? AND ${guard}`).bind(s.owner,id,...args));
    for(const {kind,value} of this.staged.values())statements.push(s.db.prepare(`INSERT INTO tcg_records(owner,id,kind,project_id,chat_id,payload) SELECT ?,?,?,?,?,? WHERE ${guard} ON CONFLICT(owner,id) DO UPDATE SET kind=excluded.kind,project_id=excluded.project_id,chat_id=excluded.chat_id,payload=excluded.payload`).bind(s.owner,value.id,kind,value.project_id??null,value.chat_id??null,encode(value),...args));
    for(const e of this.events)statements.push(s.db.prepare(`INSERT INTO tcg_events(owner,run_id,kind,payload,created_at) SELECT ?,?,?,?,? WHERE ${guard}`).bind(s.owner,e.runId,e.kind,encode(e.data),now(),...args));
    // Cancellation invalidates the lease in the same atomic batch as the run state.
    for(const id of this.leaseDeletes)statements.push(s.db.prepare(`DELETE FROM tcg_leases WHERE owner=? AND run_id=? AND ${guard}`).bind(s.owner,id,...args));
    const result=await s.db.batch(statements);return !!result[0].meta.changes;
  }
}
