import {JSDOM} from 'jsdom';
import {test,afterEach} from 'node:test';
import assert from 'node:assert/strict';
const dom=new JSDOM('<!doctype html><html><body></body></html>',{url:'http://localhost/'});
for(const key of ['window','document','HTMLElement','MutationObserver','Node','Event'])Object.defineProperty(globalThis,key,{value:(dom.window as any)[key],configurable:true});
Object.defineProperty(globalThis,'navigator',{value:dom.window.navigator,configurable:true});
(globalThis as any).IS_REACT_ACT_ENVIRONMENT=true;
const React=await import('react');
const {render,screen,fireEvent,waitFor,cleanup}=await import('@testing-library/react');
const {FieldDriftSuggestion,ExportFieldDrift}=await import('../src/FieldDrift');
const originalFetch=globalThis.fetch;
afterEach(()=>{cleanup();globalThis.fetch=originalFetch;});
const drift={artifact_id:'a',revision:2,head_revision:3,profile_id:'p',profile_version:4,candidates:[{field:'test_data',header:'测试数据',item_ids:['C1']},{field:'note',header:'备注',item_ids:['C2']}]};
test('composer suggestion stages frozen metadata once and opens existing Profile confirmation',async()=>{
 const calls:any[]=[];let opened='';
 globalThis.fetch=(async(input:any,init:any={})=>{calls.push({path:String(input),body:init.body&&JSON.parse(init.body)});return new Response(JSON.stringify(init.method==='POST'?{pending:[{id:'prompt-p'}]}:drift));}) as typeof fetch;
 render(<FieldDriftSuggestion chatId="chat" profileId="p" refreshKey="1" onProposed={id=>opened=id}/>);
 fireEvent.click(await screen.findByRole('button',{name:/同步到 Profile/}));
 await waitFor(()=>assert.equal(opened,'prompt-p'));
 assert.deepEqual(calls.find(c=>c.body).body,{artifact_id:'a',revision:2,head_revision:3,profile_id:'p',profile_version:4,fields:['test_data','note']});
 assert.equal(calls.filter(c=>c.body).length,1);
});
test('navigation ignores stale detection and does not offer old chat fields',async()=>{
 let finish:((r:Response)=>void)|undefined;
 globalThis.fetch=(async(input:any)=>String(input).includes('/old/')?await new Promise<Response>(resolve=>finish=resolve):new Response(JSON.stringify({...drift,candidates:[]}))) as typeof fetch;
 const view=render(<FieldDriftSuggestion chatId="old" refreshKey="1" onProposed={()=>assert.fail()}/>);
 view.rerender(<FieldDriftSuggestion chatId="new" refreshKey="1" onProposed={()=>assert.fail()}/>);
 await React.act(async()=>finish!(new Response(JSON.stringify(drift))));
 assert.equal(screen.queryByRole('button',{name:/同步到 Profile/}),null);
});
test('export detects only exported rows, snapshots warn without mutation, current Profile stages selected fields',async()=>{
 const calls:any[]=[];let opened='';
 globalThis.fetch=(async(input:any,init:any)=>{calls.push(JSON.parse(init.body));return new Response(JSON.stringify({pending:[{id:'p1'}]}));}) as typeof fetch;
 const props={chatId:'chat',artifactId:'a',revision:2,options:{head_revision:3,snapshot_drift:drift.candidates,profiles:[{id:'p',version:4,field_drift:drift.candidates}]},exportIds:['C1'],onProposed:(id:string)=>opened=id};
 const view=render(<ExportFieldDrift {...props} profileId=""/>);
 assert.ok(screen.getByText(/测试数据/));assert.equal(screen.queryByText(/备注/),null);
 assert.equal(screen.queryByRole('button',{name:/补充到模板/}),null);assert.equal(calls.length,0);
 view.rerender(<ExportFieldDrift {...props} profileId="p"/>);
 fireEvent.click(screen.getByRole('button',{name:/补充到模板/}));
 await waitFor(()=>assert.equal(opened,'p1'));
 assert.deepEqual(calls[0].fields,['test_data']);
});

test('a version conflict can refresh the binding without automatically resubmitting',async()=>{
 let gets=0,posts=0;const sent:any[]=[];
 globalThis.fetch=(async(input:any,init:any={})=>{
  if(init.method==='POST'){posts++;sent.push(JSON.parse(init.body));return posts===1?new Response(JSON.stringify({detail:'Profile 已改变'}),{status:409}):new Response(JSON.stringify({pending:[{id:'fresh'}]}));}
  gets++;return new Response(JSON.stringify({...drift,profile_version:gets===1?4:5}));
 }) as typeof fetch;
 render(<FieldDriftSuggestion chatId="chat" refreshKey="1" onProposed={()=>{}}/>);
 fireEvent.click(await screen.findByRole('button',{name:/同步到 Profile/}));
 fireEvent.click(await screen.findByRole('button',{name:'刷新字段建议'}));
 await waitFor(()=>assert.equal(gets,2));assert.equal(posts,1);
 fireEvent.click(await screen.findByRole('button',{name:/同步到 Profile/}));
 await waitFor(()=>assert.equal(posts,2));assert.equal(sent[1].profile_version,5);
});
