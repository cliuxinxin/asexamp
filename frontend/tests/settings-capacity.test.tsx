import {JSDOM} from 'jsdom';
import {test} from 'node:test';
import assert from 'node:assert/strict';

const dom=new JSDOM('<!doctype html><html><body></body></html>',{url:'http://localhost:8000/'});
for(const key of ['window','document','HTMLElement','HTMLDialogElement','MutationObserver','Node','Event','MouseEvent'])Object.defineProperty(globalThis,key,{value:(dom.window as any)[key],configurable:true,writable:true});
Object.defineProperty(globalThis,'navigator',{value:dom.window.navigator,configurable:true});
(globalThis as any).IS_REACT_ACT_ENVIRONMENT=true;
(globalThis as any).EventSource=class{addEventListener(){}close(){}};
dom.window.HTMLDialogElement.prototype.showModal=function(){this.open=true;};
dom.window.HTMLDialogElement.prototype.close=function(){this.open=false;};

const React=await import('react');
const {render,fireEvent,screen,waitFor,cleanup}=await import('@testing-library/react');
const {SettingsDialog}=await import('../src/SettingsDialog');
const {RunCard}=await import('../src/RunCard');
const {ConversationContext}=await import('../src/conversation');

function json(value:unknown){return new Response(JSON.stringify(value),{status:200,headers:{'Content-Type':'application/json'}});}

test('model settings save context and request output capacity',async()=>{
 const original=globalThis.fetch;let saved:any;
 globalThis.fetch=(async(input:any,init:any)=>{
  if(init?.method==='GET')return json({provider:'openai',base_url:'http://gateway',model:'model',has_api_key:false,timeout_seconds:300,context_window:32768,output_tokens:8192,output_limit_mode:'request',server_output_tokens:null});
  saved=JSON.parse(init.body);return json({...saved,has_api_key:false});
 }) as typeof fetch;
 try{
  render(<SettingsDialog projectId="project" profiles={[]} onClose={()=>{}} onSaved={()=>{}}/>);
  assert.equal((await screen.findByLabelText('上下文窗口 Token 数') as HTMLInputElement).value,'32768');
  fireEvent.change(screen.getByLabelText('单次输出预留 Token 数'),{target:{value:'7000'}});
  fireEvent.click(screen.getByRole('button',{name:'保存'}));
  await waitFor(()=>assert.equal(saved?.output_tokens,7000));
  assert.equal(saved.context_window,32768);assert.equal(saved.output_limit_mode,'request');assert.equal('server_output_tokens' in saved,true);
 }finally{cleanup();globalThis.fetch=original;}
});

test('server enforced mode requires a positive explicit limit and capacity margin',async()=>{
 const original=globalThis.fetch;let saves=0;
 globalThis.fetch=(async(_input:any,init:any)=>{if(init?.method==='GET')return json({provider:'openai',base_url:'http://gateway',model:'model',has_api_key:false,timeout_seconds:300,context_window:10000,output_tokens:9000,output_limit_mode:'request',server_output_tokens:null});saves++;return json({});}) as typeof fetch;
 try{
  render(<SettingsDialog projectId="project" profiles={[]} onClose={()=>{}} onSaved={()=>{}}/>);await screen.findByLabelText('上下文窗口 Token 数');
  fireEvent.click(screen.getByLabelText('由服务端强制限制'));
  fireEvent.click(screen.getByRole('button',{name:'保存'}));
  assert.ok(await screen.findByText(/服务端输出上限必须大于 0/));assert.equal(saves,0);
  fireEvent.change(screen.getByLabelText('服务端强制输出 Token 数'),{target:{value:'9000'}});
  fireEvent.click(screen.getByRole('button',{name:'保存'}));
  assert.ok(await screen.findByText(/至少预留 1024 Token/));assert.equal(saves,0);
 }finally{cleanup();globalThis.fetch=original;}
});

test('confirmation binds the interrupt, control and artifact revisions',async()=>{
 const original=globalThis.fetch;let request:any;
 globalThis.fetch=(async(input:any,init:any)=>{if(String(input).includes('/events'))return json({events:[]});request=JSON.parse(init.body);return json({status:'succeeded',parts:[]});}) as typeof fetch;
 try{
  render(<ConversationContext.Provider value={{chatId:'chat',onChanged:()=>{}}}><RunCard run={{id:'run',chat_id:'chat',status:'waiting',intent:'generate_case',mode:'hitp',stage:'scenario_review',updated_at:'2026-09-11',artifact_ids:[],interrupt_id:'gate',control_version:4,artifact_revision:7,interrupt:{type:'workflow_paused'}}} onChanged={()=>{}} onTarget={()=>{}}/></ConversationContext.Provider>);
  fireEvent.click(screen.getByRole('button',{name:'继续当前任务'}));
  await waitFor(()=>assert.equal(request?.command?.arguments?.expected_revision,7));
  assert.equal(request.command.arguments.interrupt_id,'gate');assert.equal(request.command.arguments.expected_control_version,4);
 }finally{cleanup();globalThis.fetch=original;}
});
