import {Engine} from './graph';
import {Store,type Fence} from './store';
import {type ModelInvoke} from './model';
import {DomainError,now} from './domain';

export async function eventsResponse(request:Request,store:Store,runId:string,invoke:ModelInvoke,ctx:any,interval=500){
 await store.get('run',runId);
 const raw=request.headers.get('last-event-id')||new URL(request.url).searchParams.get('after')||'0';
 let cursor=Number(raw);if(!Number.isSafeInteger(cursor)||cursor<0)cursor=0;
 const stopped=new AbortController();let fence:Fence|null=null,drive:Promise<void>|null=null,lastRenew=0;
 const abort=()=>stopped.abort(new DomainError('页面连接已断开，保留已完成进度'));
 request.signal.addEventListener('abort',abort,{once:true});if(request.signal.aborted)abort();
 const encoder=new TextEncoder();
 const stream=new ReadableStream<Uint8Array>({
  start(controller){
   const send=(text:string)=>{if(!stopped.signal.aborted)controller.enqueue(encoder.encode(text));};
   const pump=async()=>{
    try{
     send('retry: 1500\n\n');
     while(!stopped.signal.aborted){
      let run=await store.get('run',runId);
      if(!drive&&(['queued','running'].includes(run.status)||(run.status==='waiting'&&run._pending_edit))){
       const acquired=await store.claim(runId);
       if(acquired){
        fence=acquired;lastRenew=Date.now();
        const engine=new Engine(store,acquired,stopped.signal,invoke);
        // The open response owns execution. Checkpoints survive response termination.
        drive=engine.execute().catch(()=>{abort();}).finally(async()=>{await store.release(acquired).catch(()=>{});fence=null;drive=null;});
       }
      }
      if(fence&&Date.now()-lastRenew>10000){
       try{await store.renew(fence);lastRenew=Date.now();}catch{abort();break;}
      }
      if((controller.desiredSize??1)>0){
       const rows=await store.events(runId,cursor);
       for(const row of rows){cursor=row.id;send(`id: ${row.id}\nevent: ${row.kind}\ndata: ${JSON.stringify(row.data)}\n\n`);}
       run=await store.get('run',runId);
       if(['completed','failed','cancelled'].includes(run.status)&&!drive&&!(await store.events(runId,cursor)).length){send(`event: done\ndata: ${JSON.stringify(await store.runView(run))}\n\n`);break;}
       if(rows.length===200)continue;
       send(': keepalive '+now()+'\n\n');
      }
      await new Promise<void>(resolve=>{const done=()=>{clearTimeout(timer);stopped.signal.removeEventListener('abort',done);resolve();};const timer=setTimeout(done,interval);stopped.signal.addEventListener('abort',done,{once:true});if(stopped.signal.aborted)done();});
     }
    }catch{abort();}
    finally{request.signal.removeEventListener('abort',abort);if(drive)await drive;try{controller.close();}catch{}}
   };
   const work=pump();ctx?.waitUntil?.(work);
  },
  cancel(){abort();},
 });
 return new Response(stream,{headers:{'Content-Type':'text/event-stream; charset=utf-8','Cache-Control':'no-cache, no-store','X-Accel-Buffering':'no'}});
}
