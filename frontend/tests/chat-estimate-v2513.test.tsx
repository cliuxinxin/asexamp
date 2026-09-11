import {JSDOM} from 'jsdom';
import {afterEach,test} from 'node:test';
import assert from 'node:assert/strict';

const dom=new JSDOM('<!doctype html><html><body></body></html>',{url:'http://localhost:8000/'});
for(const key of ['window','document','HTMLElement','HTMLDialogElement','MutationObserver','Node','Event','MouseEvent'])Object.defineProperty(globalThis,key,{value:(dom.window as any)[key],configurable:true,writable:true});
Object.defineProperty(globalThis,'navigator',{value:dom.window.navigator,configurable:true});
(globalThis as any).IS_REACT_ACT_ENVIRONMENT=true;
(globalThis as any).EventSource=class{addEventListener(){}close(){}};
dom.window.HTMLElement.prototype.scrollIntoView=function(){};
dom.window.HTMLDialogElement.prototype.showModal=function(){this.open=true;};
dom.window.HTMLDialogElement.prototype.close=function(){this.open=false;};
const React=await import('react');
const {render,fireEvent,screen,waitFor,cleanup,act}=await import('@testing-library/react');
const {App}=await import('../src/App');
const originalFetch=globalThis.fetch;
afterEach(()=>{cleanup();globalThis.fetch=originalFetch;});
const json=(value:any,status=200)=>new Response(JSON.stringify(value),{status,headers:{'Content-Type':'application/json'}});
const estimate={artifact_id:'scenes',artifact_revision:3,title:'登录场景',min_count:3,max_count:5,scenarios:[{scenario_id:'S-2',title:'登录失败锁定',min_count:3,max_count:5,rationale:'包括成功、失败与阈值边界。',assumptions:['按单一账号类型估算。']}]};
function fixture(interrupt?:string,interpret?:(body:any)=>Promise<Response>){
 const calls:{path:string;body:any;method:string}[]=[];
 const artifact={id:'scenes',type:'scenarios',title:'登录场景',revision:3,items:[{id:'S-1',title:'成功登录'},{id:'S-2',title:'登录失败锁定'}]};
 const state:any={chat:{id:'chat',project_id:'project',title:'对话'},sources:[],messages:[{id:'initial',role:'assistant',content:'场景已整理。',metadata:{artifact_ids:['scenes']}}],runs:interrupt?[{id:'paused',status:'waiting',intent:'generate_case',mode:'hitp',stage:interrupt,updated_at:'2026-09-11T00:00:00Z',artifact_ids:['scenes'],draft_artifact_id:'scenes',interrupt:{type:interrupt,artifact_id:'scenes',questions:interrupt==='clarification'?['是否锁定？']:[]}}]:[]};
 globalThis.fetch=(async(input:any,init:RequestInit={})=>{
  const path=String(input).replace('/api',''),body=init.body?JSON.parse(String(init.body)):undefined;calls.push({path,body,method:init.method??'GET'});
  if(path==='/projects')return json([{id:'project',name:'演示项目'}]);
  if(path==='/settings')return json({model:'mock'});
  if(path.endsWith('/profiles'))return json([{id:'profile',name:'Default',config:{}}]);
  if(path==='/projects/project/chats')return json([state.chat]);
  if(path==='/chats/chat')return json(state);
  if(path==='/artifacts/scenes')return json(artifact);
  if(path==='/chats/chat/interpret'){
   if(interpret)return interpret(body);
   state.messages.push({id:'estimate-'+state.messages.length,role:'assistant',content:'已估算当前范围。',metadata:{case_estimate:estimate}});
   return json({handled:true,kind:'estimate',estimate});
  }
  if(path==='/chats/chat/messages'||path==='/runs/paused/dialogue'||path==='/runs/paused/resume')return json({});
  throw new Error('Unexpected API request: '+path);
 }) as typeof fetch;
 return{calls,state,artifact};
}
async function ready(){render(<App/>);await screen.findByRole('button',{name:'并排查看结果'});}
function submit(text:string){fireEvent.change(screen.getByLabelText('聊天输入'),{target:{value:text}});fireEvent.click(screen.getByRole('button',{name:'发送消息',exact:true}));}
const posts=(calls:any[])=>calls.filter(call=>call.method==='POST');

test('plain estimate overrides a stale generation selection, renders in chat and retains current source for follow-ups',async()=>{
 const {calls,artifact}=fixture();await ready();fireEvent.click(screen.getByRole('button',{name:'生成用例',exact:true}));
 submit('只按第二个场景估算需要多少 case，不要生成。');
 await screen.findByRole('table',{name:'用例数量估算'});
 assert.equal(posts(calls).length,1);assert.equal(posts(calls)[0].path,'/chats/chat/interpret');
 assert.equal(posts(calls)[0].body.intent_hint,'generate_case');assert.equal(posts(calls)[0].body.artifact_id,'scenes');
 assert.ok(screen.getByText('来源：登录场景 · v3'));assert.ok(screen.getByText('预计 3–5 条用例'));assert.ok(screen.getByText('本次范围：1 个场景 · S-2'));
 assert.ok(screen.getByText('未生成用例'));assert.ok(screen.getByText('按单一账号类型估算。'));
 await waitFor(()=>assert.equal((screen.getByLabelText('聊天输入') as HTMLTextAreaElement).value,''));
 submit('再估算全部场景。');await waitFor(()=>assert.equal(posts(calls).length,2));
 assert.equal(posts(calls)[1].body.artifact_id,'scenes');assert.equal(artifact.revision,3);assert.equal(artifact.items.length,2);
});

test('estimation while waiting never resumes and locks confirmation until the chat response arrives',async()=>{
 let resolve!:(value:Response)=>void;const pending=new Promise<Response>(value=>{resolve=value;});
 const {calls,state}=fixture('scenario_review',()=>pending);await ready();
 const confirm=screen.getByRole('button',{name:'确认场景，继续生成用例'}) as HTMLButtonElement;
 submit('这些场景大概需要多少用例？');assert.equal(confirm.disabled,true);
 await waitFor(()=>assert.equal(posts(calls).length,1));
 await act(async()=>{state.messages.push({id:'estimated',role:'assistant',content:'已估算。',metadata:{case_estimate:estimate}});resolve(json({handled:true,kind:'estimate'}));});
 await screen.findByRole('table',{name:'用例数量估算'});await waitFor(()=>assert.equal(confirm.disabled,false));
 assert.deepEqual(posts(calls).map(call=>call.path),['/chats/chat/interpret']);assert.equal(state.runs[0].status,'waiting');assert.equal(state.runs[0].id,'paused');
});

test('ordinary query at a clarification gate uses the returned intent and does not submit an answer',async()=>{
 const {calls}=fixture('clarification',async()=>json({handled:false,intent:'query'}));await ready();submit('为什么需要确认是否锁定？');
 await waitFor(()=>assert.equal(posts(calls).length,2));
 assert.deepEqual(posts(calls).map(call=>call.path),['/chats/chat/interpret','/runs/paused/dialogue']);
 assert.equal(posts(calls)[1].body.content,'为什么需要确认是否锁定？');
});

test('normal typed generation falls through with the interpreted intent and current scenario',async()=>{
 const {calls}=fixture(undefined,async()=>json({handled:false,intent:'generate_case'}));await ready();submit('根据当前场景生成测试用例。');
 await waitFor(()=>assert.equal(posts(calls).length,2));
 assert.equal(posts(calls)[1].path,'/chats/chat/messages');assert.equal(posts(calls)[1].body.intent,'generate_case');assert.equal(posts(calls)[1].body.artifact_id,'scenes');
});

test('interpretation errors retain the draft and never start a generation task',async()=>{
 const {calls}=fixture(undefined,async()=>json({detail:'场景已更新，请重试。'},409));await ready();submit('仅估算当前场景的 case 数量。');
 await screen.findByText('场景已更新，请重试。');assert.equal((screen.getByLabelText('聊天输入') as HTMLTextAreaElement).value,'仅估算当前场景的 case 数量。');
 assert.deepEqual(posts(calls).map(call=>call.path),['/chats/chat/interpret']);
});

test('explicit generation controls and saved requirement text keep their existing dispatch',async()=>{
 const {calls}=fixture();await ready();fireEvent.click(screen.getByRole('button',{name:'继续生成用例',exact:true}));
 await waitFor(()=>assert.equal(posts(calls).length,1));assert.equal(posts(calls)[0].path,'/chats/chat/messages');
 await waitFor(()=>assert.equal((screen.getByRole('button',{name:'发送消息',exact:true}) as HTMLButtonElement).disabled,true));
 fireEvent.click(screen.getByLabelText('正文作为需求'));submit('界面应允许估算数量，这是需求正文。');
 await waitFor(()=>assert.equal(posts(calls).length,2));assert.equal(posts(calls)[1].path,'/chats/chat/messages');assert.equal(posts(calls)[1].body.as_requirement,true);
});
