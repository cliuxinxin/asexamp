import {ChatOpenAI} from '@langchain/openai';
import {SystemMessage,HumanMessage} from '@langchain/core/messages';
import contracts from './contracts.json';
import {DomainError,now,uid,type Json} from './domain';
import {Store,type Fence} from './store';
export const TIMEOUT=3600;
const defaults={provider:'openai',base_url:'https://api.openai.com/v1',model:'',timeout_seconds:TIMEOUT};
export function address(value:string){
 let u:URL;try{u=new URL(value);}catch{throw new DomainError('模型地址必须为有效 HTTPS URL');}
 const host=u.hostname.replace(/^\[|\]$/g,'').toLowerCase();
 if(value.length>1000||u.protocol!=='https:'||u.username||u.password||u.search||u.hash||host==='localhost'||host.endsWith('.localhost')||host.endsWith('.local')||host==='::1'||host.startsWith('fe80:')||host.startsWith('fc')&&host.includes(':')||host.startsWith('fd')&&host.includes(':')||/^(0|10|127|169\.254|192\.168|172\.(1[6-9]|2\d|3[01]))\./.test(host))throw new DomainError('云端模型需要可访问的 HTTPS 地址；本机 Ollama 请使用本地部署');
 return value.replace(/\/+$/,'');
}
const b64=(bytes:Uint8Array)=>btoa(Array.from(bytes,b=>String.fromCharCode(b)).join(''));
const bytes=(text:string)=>Uint8Array.from(atob(text),c=>c.charCodeAt(0));
async function key(env:Json){if(!env.TCG_SECRET_KEY)throw new DomainError('云端密钥存储尚未配置',503);return crypto.subtle.importKey('raw',bytes(env.TCG_SECRET_KEY),'AES-GCM',false,['encrypt','decrypt']);}
async function encrypt(value:string,env:Json){const iv=crypto.getRandomValues(new Uint8Array(12));const result=await crypto.subtle.encrypt({name:'AES-GCM',iv},await key(env),new TextEncoder().encode(value));return b64(iv)+'.'+b64(new Uint8Array(result));}
async function decrypt(value:string,env:Json){if(!value)return '';try{const [iv,data]=value.split('.');return new TextDecoder().decode(await crypto.subtle.decrypt({name:'AES-GCM',iv:bytes(iv)},await key(env),bytes(data)));}catch{throw new DomainError('无法解密云端 API Key，请重新保存模型配置');}}
export class Settings {
 constructor(public store:Store,public env:Json){}
 async value():Promise<Json>{const saved=await this.store.maybe('settings','settings');const result={...defaults,...(saved??{})};if(this.env.TCG_MODEL_NAME){Object.assign(result,{provider:this.env.TCG_MODEL_PROVIDER||'openai',base_url:this.env.TCG_MODEL_BASE_URL||defaults.base_url,model:this.env.TCG_MODEL_NAME});}result.timeout_seconds=TIMEOUT;return result;}
 async secret(snapshot?:Json){if(this.env.TCG_MODEL_NAME)return this.env.TCG_MODEL_API_KEY||'';return decrypt((snapshot??await this.value())._api_key||'',this.env);}
 async public(){const value=await this.value();return {...Object.fromEntries(Object.keys(defaults).map(k=>[k,value[k]])),has_api_key:!!(this.env.TCG_MODEL_NAME?this.env.TCG_MODEL_API_KEY:value._api_key),environment_managed:!!this.env.TCG_MODEL_NAME,env_file:null,timeout_policy:'fixed_60_minutes',runtime:'cloud'};}
 async save(body:Json){
  if(this.env.TCG_MODEL_NAME)throw new DomainError('模型由云端环境变量管理，请修改云端部署配置',409);
  if(!['openai','ollama'].includes(body.provider)||typeof body.model!=='string'||body.model.length>200)throw new DomainError('模型配置无效');
  const url=address(body.base_url);let ciphertext:string|undefined;
  if(body.api_key!==undefined&&body.api_key!==null){if(typeof body.api_key!=='string'||body.api_key.length>2000)throw new DomainError('API Key 格式无效');ciphertext=body.api_key?await encrypt(body.api_key,this.env):'';}
  await this.store.transaction(async tx=>{const old=await tx.maybe('settings','settings');const same=old?.provider===body.provider&&old?.base_url===url;tx.put('settings',{id:'settings',provider:body.provider,base_url:url,model:body.model.trim(),timeout_seconds:TIMEOUT,_api_key:body.clear_api_key?'':ciphertext!==undefined?ciphertext:same?old?._api_key||'':''});});return this.public();
 }
}
export type ModelInvoke=(task:string,context:Json,onText:(text:string)=>Promise<void>,signal:AbortSignal,request:(record:Json)=>Promise<void>)=>Promise<Json>;
export function visibleText(content:any):string {return typeof content==='string'?content:Array.isArray(content)?content.filter(c=>c&&['text','output_text'].includes(c.type)&&typeof c.text==='string').map(c=>c.text).join(''):'';}
export class Gateway {
 constructor(public settings:Settings){}
 async invoke(task:string,context:Json,onText:(text:string)=>Promise<void>,signal:AbortSignal,record:(r:Json)=>Promise<void>):Promise<Json>{
  const instructions=(contracts.tasks as Json)[task];if(!instructions)throw new DomainError('不支持的模型任务');
  const value=await this.settings.value();if(!value.model)throw new DomainError('请先在模型与设置中保存模型名称和 API 地址');
  const baseUrl=address(value.base_url),apiKey=await this.settings.secret(value);const content=JSON.stringify(context);if(content.length>500000)throw new DomainError('当前节点上下文超过安全预算，请拆分需求或限定范围，内容未截断');
  const messages=[{role:'system',content:contracts.system+'\nTASK CONTRACT:\n'+instructions},{role:'user',content}];
  const parameters={temperature:0,stream:true,...(value.provider==='ollama'?{format:'json'}:{response_format:{type:'json_object'}})};
  await record({provider:value.provider,base_url:baseUrl,model:value.model,task,timeout_seconds:TIMEOUT,messages,parameters,representation:value.provider==='openai'?'langchain_messages_and_explicit_parameters':'ollama_messages_and_explicit_parameters'});
  let text='',finish:string|undefined,completed=false;
  const accept=async(delta:string)=>{text+=delta;if(text.length>1000000)throw new DomainError('模型输出超过安全预算，未接受截断结果');await onText(delta);};
  try{
   if(value.provider==='openai'){
    const model=new ChatOpenAI({model:value.model,apiKey:apiKey||'local-no-key',configuration:{baseURL:baseUrl,fetch:(url,init)=>fetch(url,{...init,redirect:'error'})},temperature:0,timeout:TIMEOUT*1000,maxRetries:0,streamUsage:false,modelKwargs:{response_format:{type:'json_object'}}});
    const stream=await model.stream([new SystemMessage(messages[0].content),new HumanMessage(content)],{signal});
    for await(const chunk of stream){signal.throwIfAborted();const delta=visibleText(chunk.content);if(delta)await accept(delta);if(typeof chunk.response_metadata?.finish_reason==='string')finish=chunk.response_metadata.finish_reason;}
   }else{
    const response=await fetch(baseUrl+'/api/chat',{method:'POST',headers:{'Content-Type':'application/json',...(apiKey?{Authorization:'Bearer '+apiKey}:{})},body:JSON.stringify({model:value.model,messages,stream:true,format:'json',options:{temperature:0}}),signal,redirect:'error'});
    if(!response.ok)throw new DomainError(`模型接口返回 HTTP ${response.status}，请检查地址、模型和密钥`,502);if(!response.body)throw new DomainError('模型没有返回响应正文',502);
    const reader=response.body.getReader(),decoder=new TextDecoder();let buffer='';
    const consume=async(line:string)=>{if(!line.trim())return;const data=JSON.parse(line);if(data.error)throw new DomainError('Ollama 返回错误，请检查模型服务',502);if(data.message?.content)await accept(data.message.content);if(data.done===true)completed=true;if(typeof data.done_reason==='string')finish=data.done_reason;};
    try{while(true){const r=await reader.read();buffer+=decoder.decode(r.value??new Uint8Array(),{stream:!r.done});let at:number;while((at=buffer.indexOf('\n'))>=0){const line=buffer.slice(0,at);buffer=buffer.slice(at+1);await consume(line);}if(r.done)break;}await consume(buffer);}finally{await reader.cancel().catch(()=>{});}

   }
  }catch(e:any){if(signal.aborted)throw signal.reason;if(e instanceof DomainError)throw e;const status=Number(e?.status);throw new DomainError(Number.isInteger(status)&&status>=400?`模型接口返回 HTTP ${status}，请检查连接和配置`:'模型连接失败，请检查 HTTPS 地址、模型名称和服务状态',502);}
  if(['length','max_tokens'].includes(finish??''))throw new DomainError('模型输出达到长度限制，未接受截断结果');
  if(value.provider==='openai'?finish!=='stop':!completed)throw new DomainError('模型响应没有完成标记，未接受可能不完整的结果，请重试');
  let result:any;try{result=JSON.parse(text.trim().replace(/^```(?:json)?\s*\n?/i,'').replace(/\n?```\s*$/,''));}catch{throw new DomainError('模型返回的内容不是完整 JSON，请重试当前阶段');}
  if(!result||typeof result!=='object'||Array.isArray(result))throw new DomainError('模型必须返回 JSON 对象');return result;
 }
}
export class ModelCalls {
 node='route';
 constructor(public store:Store,public fence:Fence,public signal:AbortSignal,public invoke:ModelInvoke){}
 async trace(event:string,fields:Json={}){const run=await this.store.get('run',this.fence.runId);await this.store.event(run.id,'progress',{at:now(),event,run_id:run.id,chat_id:run.chat_id,stage:run.stage,node:this.node,...fields},this.fence);}
 async call(key:string,task:string,context:Json):Promise<Json>{
  this.signal.throwIfAborted();await this.store.updateRun(this.fence.runId,{_active_call_key:key},this.fence);
  const cached=await this.store.cache(this.fence.runId,key);if(cached!==null){await this.trace('model.cache_hit',{call_key:key});return cached;}
  for(let attempt=1;attempt<=2;attempt++){
   const id=uid('call_'),started=Date.now();const fields={call_id:id,call_key:key,task,attempt,max_attempts:2,timeout_seconds:TIMEOUT,streaming:true,...(context.batch_count?{batch_index:context.batch_index+1,batch_count:context.batch_count}:{})};
   await this.trace('model.start',fields);let pending='',last=Date.now();const controller=new AbortController();const abort=()=>controller.abort(this.signal.reason);this.signal.addEventListener('abort',abort,{once:true});
   if(this.signal.aborted)abort();const timer=setTimeout(()=>controller.abort(new DomainError('模型请求超过 60 分钟，已保存的进度保留',504)),TIMEOUT*1000);
   let heartbeat=Promise.resolve();
   const pulse=setInterval(()=>{heartbeat=heartbeat.then(()=>this.trace('model.waiting',{...fields,elapsed_ms:Date.now()-started})).catch(e=>{controller.abort(e);});},10000);
   const flush=async()=>{if(pending){const delta=pending;pending='';last=Date.now();await this.store.event(this.fence.runId,'model_delta',{run_id:this.fence.runId,call_id:id,text:delta},this.fence);}};
   try{
    const value=await this.invoke(task,context,async text=>{controller.signal.throwIfAborted();pending+=text;if(pending.length>=2048||Date.now()-last>400)await flush();},controller.signal,async request=>{
     await this.store.transaction(async tx=>{await tx.assertRunning(this.fence.runId);tx.put('model_request',{id:`request:${this.fence.runId}:${id}`,run_id:this.fence.runId,call_id:id,at:now(),node:this.node,attempt,call_key:key,...request});},this.fence);await this.trace('model.request_saved',{...fields,request_available:true});
    });
    clearInterval(pulse);await heartbeat;controller.signal.throwIfAborted();await flush();await this.trace('model.complete',{...fields,elapsed_ms:Date.now()-started});return await this.store.setCache(this.fence.runId,key,value,this.fence);
   }catch(e){await flush().catch(()=>{});if(this.signal.aborted)throw this.signal.reason;await this.store.checkFence(this.fence);await this.trace('model.error',{...fields,elapsed_ms:Date.now()-started,error_type:e instanceof Error?e.name:'Error'});if(attempt===2)throw e;await this.trace('model.retry',{...fields,next_attempt:2});}
   finally{clearInterval(pulse);await heartbeat;clearTimeout(timer);this.signal.removeEventListener('abort',abort);}
  }
  throw new Error('Unreachable');
 }
}
