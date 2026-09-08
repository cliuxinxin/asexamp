import {test} from 'node:test';
import assert from 'node:assert/strict';
import {Store} from '../store';
import {LeaseLost} from '../domain';
import {TestD1} from './d1';
export async function fixture(){const db=new TestD1(),store=new Store(db,'owner');await store.initialize();await store.ensureDefault();const project=(await store.list('project'))[0];const chat=await store.transaction(async tx=>tx.put('chat',{id:'chat',project_id:project.id,title:'test',created_at:new Date().toISOString(),updated_at:new Date().toISOString()}));const {run}=await store.transaction(tx=>tx.createRun(chat.id,{content:'分析需求',intent:'review_requirement',mode:'auto'}));return {db,store,run,project,chat};}
test('one active run per chat survives concurrent creation',async()=>{const {store}=await fixture();await assert.rejects(()=>store.transaction(tx=>tx.createRun('chat',{content:'another',intent:'query',mode:'auto'})),/已有运行/);});
test('one lease wins; cancellation fences late output',async()=>{
 const {store,run}=await fixture();const [a,b]=await Promise.all([store.claim(run.id),store.claim(run.id)]);assert.equal(Number(!!a)+Number(!!b),1);const fence=(a??b)!;
 await store.transaction(async tx=>{const r=await tx.get('run',run.id);tx.saveRun({...r,status:'cancelled'});tx.leaseDeletes.add(run.id);});
 await assert.rejects(()=>store.setCache(run.id,'late',{ok:true},fence),LeaseLost);assert.equal(await store.cache(run.id,'late'),null);
});
test('expired owner cannot commit after another connection takes over',async()=>{
 const {db,store,run}=await fixture();const first=(await store.claim(run.id))!;
 db.db.prepare('UPDATE tcg_leases SET expires=0').run();const second=await store.claim(run.id);assert.ok(second);assert.notEqual(first.token,second!.token);
 await assert.rejects(()=>store.updateRun(run.id,{stage:'bad'},first),LeaseLost);
 await store.updateRun(run.id,{stage:'good'},second!);assert.equal((await store.get('run',run.id)).stage,'good');
});
test('event cursor replay is ordered, bounded and owner-isolated',async()=>{
 const {db,store,run}=await fixture();for(let i=0;i<205;i++)await store.event(run.id,'progress',{event:'test',index:i});
 const first=await store.events(run.id,0);assert.equal(first.length,200);const next=await store.events(run.id,first.at(-1)!.id);assert.equal(next.length,6);
 assert.equal((await new Store(db,'other').events(run.id)).length,0);
});
