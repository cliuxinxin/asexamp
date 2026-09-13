import {JSDOM} from 'jsdom';
import {afterEach,test} from 'node:test';
import assert from 'node:assert/strict';
const dom=new JSDOM('<!doctype html><html><body></body></html>',{url:'http://localhost/'});
for(const key of ['window','document','HTMLElement','HTMLDialogElement','MutationObserver','Node','Event','MouseEvent','CustomEvent','File','FormData'])Object.defineProperty(globalThis,key,{value:(dom.window as any)[key],configurable:true,writable:true});
Object.defineProperty(globalThis,'navigator',{value:dom.window.navigator,configurable:true});
(globalThis as any).IS_REACT_ACT_ENVIRONMENT=true;
(globalThis as any).EventSource=class{addEventListener(){}close(){}};
dom.window.HTMLElement.prototype.scrollIntoView=function(){};
dom.window.HTMLDialogElement.prototype.showModal=function(){this.open=true;};
dom.window.HTMLDialogElement.prototype.close=function(){this.open=false;};
const React=await import('react');
const {render,fireEvent,screen,waitFor,cleanup,within}=await import('@testing-library/react');
const {App}=await import('../src/App');
const originalFetch=globalThis.fetch;
afterEach(()=>{cleanup();globalThis.fetch=originalFetch;});
const json=(v:unknown)=>new Response(JSON.stringify(v),{status:200,headers:{'Content-Type':'application/json'}});
function fixture(options:{snapshotReady?:Promise<void>;firstTurnReady?:Promise<void>}={}){
 const calls:any[]=[];const state:any={chat:{id:'chat',project_id:'project',title:'登录测试'},sources:[],messages:[],runs:[]};
 globalThis.fetch=(async(input:any,init:any={})=>{
  const path=String(input).replace(/^\/api/,'');const body=init.body instanceof FormData?init.body:init.body?JSON.parse(init.body):undefined;
  calls.push({path,body});
  if(path==='/projects')return json([{id:'project',name:'登录项目'}]);
  if(path==='/settings')return json({model:'mock'});
  if(path==='/projects/project/profiles')return json([{id:'profile',name:'Default',version:1,config:{}}]);
  if(path==='/projects/project/chats')return json([state.chat]);
  if(path==='/chats/chat'){await options.snapshotReady;return json(state);}
  if(path==='/chats/chat/sources'){
   assert.ok(body instanceof FormData);const file=body.get('file') as File;
   state.sources.push({id:'source',name:file.name,role:body.get('role'),characters:100});return json(state.sources[0]);
  }
  if(path==='/chats/chat/turns'){if(calls.filter(c=>c.path===path).length===1)await options.firstTurnReady;return json({id:'turn',status:'succeeded',message:'已启动',parts:[],pending:[],actions:[]});}
  if(path.startsWith('/chats/chat/workspace-state'))return json({stages:[],impact:{status:'current',affected:[],source_ids:[]},next_action:{kind:'none'}});
  return json([]);
 }) as typeof fetch;return{calls,state};
}
async function ready(){render(<App/>);await waitFor(()=>assert.equal((screen.getByRole('button',{name:'上传需求文档'}) as HTMLButtonElement).disabled,false));}
function send(text:string){fireEvent.change(screen.getByLabelText('聊天输入'),{target:{value:text}});fireEvent.click(screen.getByRole('button',{name:'发送消息'}));}

test('original controls stay beside the compact composer on the centered start page',async()=>{
 fixture();await ready();await screen.findByText('你想测试什么？');
 const composer=screen.getByLabelText('聊天输入').closest('.composer-wrap')!;
 for(const label of ['运行模式','附件用途','运行 Profile','正文作为需求'])assert.ok(composer.contains(screen.getByLabelText(label)),label);
 const shortcuts=within(composer as HTMLElement).getByRole('group',{name:'选择任务'});
 assert.ok(within(shortcuts).getByRole('button',{name:'生成用例',exact:true}));
 assert.ok(composer.contains(screen.getByRole('button',{name:'任务设置',exact:true})));
 assert.equal(document.querySelector('.topbar')?.contains(screen.getByRole('button',{name:'任务设置',exact:true})),false);
 assert.equal(screen.getByLabelText('聊天输入').getAttribute('rows'),'2');
 assert.equal(screen.queryByRole('region',{name:'用例工作区'}),null);
});

test('upload and explicit generate choice start the existing workflow with the selected Human mode',async()=>{
 const {calls}=fixture();await ready();
 fireEvent.change(screen.getByLabelText('附件用途'),{target:{value:'primary'}});
 fireEvent.change(document.querySelector('input[type=file]')!,{target:{files:[new File(['登录需求'],'login.md',{type:'text/markdown'})]}});
 await screen.findByRole('button',{name:/login.md/});
 fireEvent.change(screen.getByLabelText('运行模式'),{target:{value:'hitp'}});
 fireEvent.click(screen.getByRole('button',{name:'生成用例',exact:true}));
 send('根据我上传的需求生成测试用例');
 await waitFor(()=>assert.ok(calls.find(c=>c.path==='/chats/chat/turns')));
 const sent=calls.find(c=>c.path==='/chats/chat/turns').body;
 assert.equal(sent.command.name,'workflow.start');assert.equal(sent.command.arguments.intent,'generate_case');
 assert.equal(sent.mode,'hitp');assert.equal(sent.profile_id,'profile');
 assert.equal(calls.filter(c=>c.path==='/chats/chat/turns').length,1);
 assert.ok(screen.getByRole('button',{name:/login.md/}));
});

test('default free conversation still lets the controller interpret the request',async()=>{
 const {calls}=fixture();await ready();send('根据上传的资料生成测试用例');
 await waitFor(()=>assert.ok(calls.find(c=>c.path==='/chats/chat/turns')));
 assert.equal(calls.find(c=>c.path==='/chats/chat/turns').body.command,undefined);
});

test('an unloaded conversation is not assumed to be an empty generation task',async()=>{
 let release!:()=>void;const snapshotReady=new Promise<void>(resolve=>{release=resolve;});
 const {calls}=fixture({snapshotReady});await ready();
 fireEvent.click(screen.getByRole('button',{name:'生成用例',exact:true}));
 send('先解释一下当前结果');
 try{
  await waitFor(()=>assert.ok(calls.find(c=>c.path==='/chats/chat/turns')));
  assert.equal(calls.find(c=>c.path==='/chats/chat/turns').body.command,undefined);
 }finally{release();}
 await waitFor(()=>assert.equal((screen.getByLabelText('运行模式') as HTMLSelectElement).disabled,false));
});

test('a follow-up during the initial request stays conversational instead of starting or confirming a workflow',async()=>{
 let release!:()=>void;const firstTurnReady=new Promise<void>(resolve=>{release=resolve;});
 const {calls}=fixture({firstTurnReady});await ready();
 // Uploading also verifies that the first snapshot has arrived.
 fireEvent.change(document.querySelector('input[type=file]')!,{target:{files:[new File(['需求'],'login.md')]}});
 await screen.findByRole('button',{name:/login.md/});
 fireEvent.click(screen.getByRole('button',{name:'生成用例',exact:true}));send('生成测试用例');
 try{
  await waitFor(()=>assert.equal(calls.filter(c=>c.path==='/chats/chat/turns').length,1));
  send('先暂停，解释一下当前结果');
  await waitFor(()=>assert.equal(calls.filter(c=>c.path==='/chats/chat/turns').length,2));
  const turns=calls.filter(c=>c.path==='/chats/chat/turns');
  assert.equal(turns[0].body.command.name,'workflow.start');
  assert.equal(turns[1].body.command,undefined);
 }finally{release();}
 await waitFor(()=>assert.equal((screen.getByLabelText('运行模式') as HTMLSelectElement).disabled,false));
});
