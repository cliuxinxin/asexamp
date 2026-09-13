import {JSDOM} from 'jsdom';
import {test} from 'node:test';
import assert from 'node:assert/strict';

const dom=new JSDOM('<!doctype html><html><body></body></html>',{url:'http://localhost:8000/'});
for(const key of ['window','document','HTMLElement','HTMLDialogElement','MutationObserver','Node','Event','MouseEvent'])Object.defineProperty(globalThis,key,{value:(dom.window as any)[key],configurable:true,writable:true});
Object.defineProperty(globalThis,'navigator',{value:dom.window.navigator,configurable:true});
(globalThis as any).IS_REACT_ACT_ENVIRONMENT=true;
dom.window.HTMLDialogElement.prototype.showModal=function(){this.open=true;};
dom.window.HTMLDialogElement.prototype.close=function(){this.open=false;};
const React=await import('react');
const {render,fireEvent,screen,waitFor,cleanup}=await import('@testing-library/react');
const {SettingsDialog}=await import('../src/SettingsDialog');

const baseSettings={provider:'openai',base_url:'http://gateway',model:'model',has_api_key:false,timeout_seconds:3600,context_window:32768,output_tokens:8192,output_limit_mode:'request',server_output_tokens:null,auth_mode:'bearer',has_headers:false,header_names:[]};
function json(value:unknown){return new Response(JSON.stringify(value),{status:200,headers:{'Content-Type':'application/json'}});}

test('saving an output budget does not depend on a legacy context window',async()=>{
 const original=globalThis.fetch;let saved:any;
 globalThis.fetch=(async(_input:any,init:any)=>{
  if(init?.method==='GET')return json(baseSettings);
  saved=JSON.parse(init.body);return json({...baseSettings,...saved});
 }) as typeof fetch;
 try{
  render(<SettingsDialog projectId="project" profiles={[]} onClose={()=>{}} onSaved={()=>{}}/>);
  await screen.findByLabelText('单次输出预留 Token 数');
  fireEvent.change(screen.getByLabelText('单次输出预留 Token 数'),{target:{value:'65536'}});
  fireEvent.click(screen.getByRole('button',{name:'保存'}));
  await waitFor(()=>assert.equal(saved?.output_tokens,65536));
  assert.equal(screen.queryByLabelText('上下文窗口 Token 数'),null);
  assert.ok(screen.getByText(/上下文容量由模型服务器判断/));
  assert.ok(screen.getByText('模型配置已保存在本机'));
 }finally{cleanup();globalThis.fetch=original;}
});

test('environment managed output settings remain locked while testing an unbounded context',async()=>{
 const original=globalThis.fetch;const calls:string[]=[];
 globalThis.fetch=(async(input:any,init:any)=>{
  calls.push(`${init.method} ${input}`);
  if(init?.method==='GET')return json({...baseSettings,context_window:0,environment_managed:true});
  return json({ok:true,message:'连接成功'});
 }) as typeof fetch;
 try{
  render(<SettingsDialog projectId="project" profiles={[]} onClose={()=>{}} onSaved={()=>{}}/>);
  assert.equal((await screen.findByLabelText('单次输出预留 Token 数') as HTMLInputElement).disabled,true);
  assert.equal((screen.getByLabelText('由服务端强制限制') as HTMLInputElement).disabled,true);
  assert.equal(screen.queryByRole('button',{name:'保存'}),null);
  fireEvent.click(screen.getByRole('button',{name:'测试连接'}));
  assert.ok(await screen.findByText('连接成功'));
  assert.deepEqual(calls,['GET /api/settings','POST /api/settings/test']);
 }finally{cleanup();globalThis.fetch=original;}
});

test('removing the context limit preserves positive output validation',async()=>{
 const original=globalThis.fetch;let saves=0;
 globalThis.fetch=(async(_input:any,init:any)=>{
  if(init?.method==='GET')return json(baseSettings);
  saves++;return json(baseSettings);
 }) as typeof fetch;
 try{
  render(<SettingsDialog projectId="project" profiles={[]} onClose={()=>{}} onSaved={()=>{}}/>);
  await screen.findByLabelText('单次输出预留 Token 数');
  fireEvent.change(screen.getByLabelText('单次输出预留 Token 数'),{target:{value:'0'}});
  fireEvent.click(screen.getByRole('button',{name:'保存'}));
  assert.ok(await screen.findByText(/输出预留必须大于 0/));
  assert.equal(saves,0);
 }finally{cleanup();globalThis.fetch=original;}
});
