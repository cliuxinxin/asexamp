import {test} from 'node:test';
import assert from 'node:assert/strict';
import {Gateway,Settings,visibleText} from '../model';
import {Store} from '../store';
import {TestD1} from './d1';
import {Engine} from '../graph';
import {ValidationError,checkPage} from '../domain';

test('real LangChain transport streams visible text and uses a coherent key/endpoint snapshot',{timeout:10000},async()=>{
 const store=new Store(new TestD1(),'alice');await store.initialize();const settings=new Settings(store,{TCG_SECRET_KEY:btoa('x'.repeat(32))});await settings.save({provider:'openai',base_url:'https://old.example/v1',model:'fixture',api_key:'OLD-FAKE'});
 const original=globalThis.fetch;let sent:any,endpoint:any;
 globalThis.fetch=async(url,init)=>{endpoint=String(url);sent=init;return new Response(['{"ok":', 'true}'].map((content,index)=>'data: '+JSON.stringify({id:'fixture',object:'chat.completion.chunk',model:'fixture',choices:[{index:0,delta:{content,reasoning_content:'private reasoning'},finish_reason:index===1?'stop':null}]})+'\n\n').join('')+'data: [DONE]\n\n',{headers:{'Content-Type':'text/event-stream'}});};
 try{
  let text='',snapshot:any;const result=await new Gateway(settings).invoke('connection_test',{},async t=>{text+=t;},new AbortController().signal,async r=>{snapshot=r;await settings.save({provider:'openai',base_url:'https://new.example/v1',model:'fixture',api_key:'NEW-FAKE'});});
  assert.deepEqual(result,{ok:true});assert.equal(text,'{"ok":true}');assert.equal(endpoint,'https://old.example/v1/chat/completions');assert.equal(new Headers(sent.headers).get('Authorization'),'Bearer OLD-FAKE');
  const actual=JSON.parse(sent.body);assert.equal(actual.stream,true);assert.deepEqual(actual.response_format,{type:'json_object'});assert.deepEqual(actual.messages,snapshot.messages);assert.equal(snapshot.timeout_seconds,3600);assert.ok(!JSON.stringify(snapshot).includes('FAKE'));
 }finally{globalThis.fetch=original;}
 assert.equal(visibleText([{type:'reasoning',text:'hidden'},{type:'text',text:'visible'}]),'visible');
});

test('Ollama rejects trailing provider errors and EOF without completion; accepts final done without newline',async()=>{
 const store=new Store(new TestD1(),'alice');await store.initialize();const settings=new Settings(store,{TCG_SECRET_KEY:btoa('x'.repeat(32))});await settings.save({provider:'ollama',base_url:'https://ollama.example',model:'fixture'});const original=globalThis.fetch;
 const call=()=>new Gateway(settings).invoke('connection_test',{},async()=>{},new AbortController().signal,async()=>{});
 try{
  const content=JSON.stringify({message:{content:'{"ok":true}'}})+'\n';
  globalThis.fetch=async()=>new Response(content+JSON.stringify({error:'failed'}));await assert.rejects(call,/Ollama 返回错误/);
  globalThis.fetch=async()=>new Response(content);await assert.rejects(call,/没有完成标记/);
  globalThis.fetch=async()=>new Response(content+JSON.stringify({done:true,done_reason:'stop'}));assert.deepEqual(await call(),{ok:true});
 }finally{globalThis.fetch=original;}
});

test('repair preserves valid sibling steps and custom fields while allowing duplicate IDs to be corrected',()=>{
 const good={id:'a',title:'case',type:'Business',priority:'P0',preconditions:'',scenario_id:'s',refs:['e'],steps:[{action:'a',expected:'b'}],custom:'preserve'},old={items:[good,{...good,id:'b',steps:[{action:'a',expected:12}]}],has_more:false,next_cursor:null};
 const repaired=structuredClone(old);repaired.items[1].steps[0].expected='b' as any;
 const preserve=Engine.prototype.preserveRepair,evidence={e:{role:'primary'}};
 preserve(old,repaired,evidence,new Set(['s']));
 const corrupt=structuredClone(repaired);corrupt.items[0].steps[0].action='silently changed';assert.throws(()=>preserve(old,corrupt,evidence,new Set(['s'])),ValidationError);
 const duplicates={items:[good,{...good}],has_more:false},fixed={items:[good,{...good,id:'second'}],has_more:false};preserve(duplicates,fixed,evidence,new Set(['s']));
 const reordered={...good,id:'new',steps:[{expected:'b',action:'a'}]};
 preserve({items:[good],has_more:false},{items:[{...reordered,id:'a'}],has_more:false},evidence,new Set(['s']));
 assert.throws(()=>checkPage({items:[reordered],has_more:false},[good],evidence,'cases',null,[],new Set(['s'])),/重复条目/);
});
