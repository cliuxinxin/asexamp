// Current model connection and output-capacity contracts.
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

function json(value:unknown){return new Response(JSON.stringify(value),{status:200,headers:{'Content-Type':'application/json'}});}

test('model settings save request output capacity without asking for a context window',async()=>{
 const original=globalThis.fetch;let saved:any;
 globalThis.fetch=(async(input:any,init:any)=>{
  if(init?.method==='GET')return json({provider:'openai',base_url:'http://gateway',model:'model',has_api_key:false,timeout_seconds:300,context_window:32768,output_tokens:8192,output_limit_mode:'request',server_output_tokens:null});
  saved=JSON.parse(init.body);return json({...saved,has_api_key:false});
 }) as typeof fetch;
 try{
  render(<SettingsDialog projectId="project" profiles={[]} onClose={()=>{}} onSaved={()=>{}}/>);
  assert.equal((await screen.findByLabelText('单次输出预留 Token 数') as HTMLInputElement).value,'8192');
  assert.equal(screen.queryByRole('option',{name:'Azure OpenAI · 环境配置'}),null);
  assert.equal(screen.queryByLabelText('上下文窗口 Token 数'),null);
  fireEvent.change(screen.getByLabelText('单次输出预留 Token 数'),{target:{value:'7000'}});
  fireEvent.click(screen.getByRole('button',{name:'保存'}));
  await waitFor(()=>assert.equal(saved?.output_tokens,7000));
  assert.equal(saved.context_window,32768);assert.equal(saved.output_limit_mode,'request');assert.equal('server_output_tokens' in saved,true);
 }finally{cleanup();globalThis.fetch=original;}
});

test('Azure environment settings expose deployment and API version while testing the managed connection without saving',async()=>{
 const original=globalThis.fetch;const requests:{url:string;method:string}[]=[];
 globalThis.fetch=(async(input:any,init:any)=>{
  requests.push({url:String(input),method:init?.method??'GET'});
  if(init?.method==='GET')return json({provider:'azure',base_url:'https://example.openai.azure.com',model:'test-deployment',api_version:'2025-01-01-preview',has_api_key:true,timeout_seconds:300,context_window:0,output_tokens:8192,output_limit_mode:'request',server_output_tokens:null,environment_managed:true,auth_mode:'bearer'});
  return json({ok:true,message:'连接成功'});
 }) as typeof fetch;
 try{
  render(<SettingsDialog projectId="project" profiles={[]} onClose={()=>{}} onSaved={()=>{}}/>);
  const provider=await screen.findByLabelText('模型服务') as HTMLSelectElement;
  assert.equal(provider.value,'azure');assert.equal(provider.disabled,true);
  const deployment=screen.getByLabelText('部署名称') as HTMLInputElement;
  assert.equal(deployment.value,'test-deployment');assert.equal(deployment.disabled,true);
  const version=screen.getByLabelText('API 版本') as HTMLInputElement;
  assert.equal(version.value,'2025-01-01-preview');assert.equal(version.readOnly,true);
  assert.ok(screen.getByRole('option',{name:'API Key · api-key',hidden:true}));
  assert.equal(screen.queryByRole('button',{name:'保存'}),null);
  fireEvent.click(screen.getByRole('button',{name:'测试连接'}));
  assert.ok(await screen.findByText('连接成功'));
  assert.deepEqual(requests.filter(request=>request.method!=='GET'),[{url:'/api/settings/test',method:'POST'}]);
 }finally{cleanup();globalThis.fetch=original;}
});

test('server enforced mode requires a positive explicit output limit without a local capacity margin',async()=>{
 const original=globalThis.fetch;let saves=0;
 globalThis.fetch=(async(_input:any,init:any)=>{if(init?.method==='GET')return json({provider:'openai',base_url:'http://gateway',model:'model',has_api_key:false,timeout_seconds:300,context_window:10000,output_tokens:9000,output_limit_mode:'request',server_output_tokens:null});saves++;return json({...JSON.parse(init.body),has_api_key:false});}) as typeof fetch;
 try{
  render(<SettingsDialog projectId="project" profiles={[]} onClose={()=>{}} onSaved={()=>{}}/>);await screen.findByLabelText('单次输出预留 Token 数');
  fireEvent.click(screen.getByLabelText('由服务端强制限制'));
  fireEvent.click(screen.getByRole('button',{name:'保存'}));
  assert.ok(await screen.findByText(/服务端输出上限必须大于 0/));assert.equal(saves,0);
  fireEvent.change(screen.getByLabelText('服务端强制输出 Token 数'),{target:{value:'9000'}});
  fireEvent.click(screen.getByRole('button',{name:'保存'}));
  assert.ok(await screen.findByText('模型配置已保存在本机'));assert.equal(saves,1);
 }finally{cleanup();globalThis.fetch=original;}
});
