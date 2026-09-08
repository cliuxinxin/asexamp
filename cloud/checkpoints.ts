import {BaseCheckpointSaver,WRITES_IDX_MAP,copyCheckpoint} from '@langchain/langgraph-checkpoint';
import {Store,type Fence,encode} from './store';
import {LeaseLost} from './domain';
export class D1Checkpointer extends BaseCheckpointSaver {
 constructor(public store:Store,public fence:Fence){super();}
 async pack(value:any){const [type,data]=await this.serde.dumpsTyped(value);return encode({type,data:btoa(Array.from(data,b=>String.fromCharCode(b)).join(''))});}
 async unpack(value:string){const {type,data}=JSON.parse(value);return this.serde.loadsTyped(type,Uint8Array.from(atob(data),c=>c.charCodeAt(0)));}
 guard(){return {sql:'EXISTS(SELECT 1 FROM tcg_leases WHERE owner=? AND run_id=? AND token=? AND expires>unixepoch()*1000)',args:[this.store.owner,this.fence.runId,this.fence.token]};}
 async getTuple(config:any):Promise<any>{
  const c=config.configurable??{},thread=c.thread_id,ns=c.checkpoint_ns??'';if(!thread)return undefined;
  const args=[this.store.owner,thread,ns];let sql='SELECT * FROM tcg_checkpoints WHERE owner=? AND thread=? AND ns=?';if(c.checkpoint_id){sql+=' AND id=?';args.push(c.checkpoint_id);}sql+=' ORDER BY id DESC LIMIT 1';
  const row=await this.store.db.prepare(sql).bind(...args).first();if(!row)return undefined;
  const w=await this.store.db.prepare('SELECT task,channel,payload FROM tcg_writes WHERE owner=? AND thread=? AND ns=? AND checkpoint=? ORDER BY task,idx').bind(this.store.owner,thread,ns,row.id).all();
  return {config:{configurable:{thread_id:thread,checkpoint_ns:ns,checkpoint_id:row.id}},checkpoint:await this.unpack(row.payload),metadata:await this.unpack(row.metadata),pendingWrites:await Promise.all(w.results.map(async(r:any)=>[r.task,r.channel,await this.unpack(r.payload)])),...(row.parent?{parentConfig:{configurable:{thread_id:thread,checkpoint_ns:ns,checkpoint_id:row.parent}}}:{})};
 }
 async *list(config:any,options:any={}):AsyncGenerator<any>{
  const c=config.configurable??{};const rows=await this.store.db.prepare('SELECT thread,ns,id FROM tcg_checkpoints WHERE owner=? AND thread=? ORDER BY id DESC').bind(this.store.owner,c.thread_id??this.fence.runId).all();let count=0;
  for(const row of rows.results){if(c.checkpoint_ns!==undefined&&c.checkpoint_ns!==row.ns||options.before?.configurable?.checkpoint_id&&row.id>=options.before.configurable.checkpoint_id)continue;const tuple=await this.getTuple({configurable:{thread_id:row.thread,checkpoint_ns:row.ns,checkpoint_id:row.id}});if(options.filter&&!Object.entries(options.filter).every(([k,v])=>tuple.metadata[k]===v))continue;if(options.limit!==undefined&&count>=options.limit)break;count++;yield tuple;}
 }
 async put(config:any,checkpoint:any,metadata:any):Promise<any>{
  const c=config.configurable??{},thread=c.thread_id,ns=c.checkpoint_ns??'';if(thread!==this.fence.runId)throw new LeaseLost();
  const g=this.guard();const r=await this.store.db.prepare(`INSERT INTO tcg_checkpoints(owner,thread,ns,id,parent,payload,metadata) SELECT ?,?,?,?,?,?,? WHERE ${g.sql} ON CONFLICT(owner,thread,ns,id) DO UPDATE SET payload=excluded.payload,metadata=excluded.metadata`).bind(this.store.owner,thread,ns,checkpoint.id,c.checkpoint_id??null,await this.pack(copyCheckpoint(checkpoint)),await this.pack(metadata),...g.args).run();if(!r.meta.changes)throw new LeaseLost();
  return {configurable:{thread_id:thread,checkpoint_ns:ns,checkpoint_id:checkpoint.id}};
 }
 async putWrites(config:any,writes:any[],taskId:string):Promise<void>{
  if(!writes.length)return;const c=config.configurable??{};if(c.thread_id!==this.fence.runId)throw new LeaseLost();
  const packed=await Promise.all(writes.map(async([channel,value],index)=>({channel,payload:await this.pack(value),idx:WRITES_IDX_MAP[channel]??index})));
  await this.store.fencedBatch(this.fence,(guard,args)=>packed.map(({channel,payload,idx})=>this.store.db.prepare(`INSERT INTO tcg_writes(owner,thread,ns,checkpoint,task,idx,channel,payload) SELECT ?,?,?,?,?,?,?,? WHERE ${guard} ON CONFLICT(owner,thread,ns,checkpoint,task,idx) ${idx<0?'DO UPDATE SET channel=excluded.channel,payload=excluded.payload':'DO NOTHING'}`).bind(this.store.owner,c.thread_id,c.checkpoint_ns??'',c.checkpoint_id,taskId,idx,channel,payload,...args)));

 }
 async deleteThread(thread:string){if(thread!==this.fence.runId)throw new LeaseLost();await this.store.fencedBatch(this.fence,(guard,args)=>['tcg_writes','tcg_checkpoints'].map(table=>this.store.db.prepare(`DELETE FROM ${table} WHERE owner=? AND thread=? AND ${guard}`).bind(this.store.owner,thread,...args)));}
}
