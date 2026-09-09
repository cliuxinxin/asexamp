import {JSDOM} from 'jsdom';
import {test} from 'node:test';
import assert from 'node:assert/strict';
const dom=new JSDOM('<!doctype html><html><body></body></html>',{url:'http://localhost:8000/'});
for(const key of ['window','document','HTMLElement','HTMLDialogElement','MutationObserver','Node','Event','MouseEvent','File','FormData'])Object.defineProperty(globalThis,key,{value:(dom.window as any)[key],configurable:true,writable:true});
Object.defineProperty(globalThis,'navigator',{value:dom.window.navigator,configurable:true});
(globalThis as any).IS_REACT_ACT_ENVIRONMENT=true;
(globalThis as any).EventSource=class{addEventListener(){}close(){}};
dom.window.HTMLElement.prototype.scrollIntoView=function(){};
dom.window.HTMLDialogElement.prototype.showModal=function(){this.open=true;};
dom.window.HTMLDialogElement.prototype.close=function(){this.open=false;};
const React=await import('react');
const {render,fireEvent,screen,waitFor,cleanup,within}=await import('@testing-library/react');
const {App}=await import('../src/App');
// This narrow component smoke uses deterministic API state, without browser/network dependencies.
test('workspace generation opens table and subsequent chat targets the existing result',async()=>{
 let generated=false,modified=false;const requests:any[]=[];
 const artifact={id:'a1',type:'cases',title:'测试用例',revision:1,items:[{id:'TC-1',title:'Login',type:'Business',priority:'P1',scenario_id:'',preconditions:'Account',steps:[{action:'Login',expected:'Home'}],refs:['s1#P1']}]};
 const response=(v:any)=>Promise.resolve(new Response(JSON.stringify(v),{status:200,headers:{'Content-Type':'application/json'}}));
 const originalFetch=globalThis.fetch;
 globalThis.fetch=(async(url:any,init:any)=>{
  const path=String(url);
  if(path==='/api/projects')return response([{id:'p1',name:'Smoke project'}]);
  if(path==='/api/settings')return response({provider:'openai',model:'smoke'});
  if(path.endsWith('/profiles'))return response([{id:'profile',name:'Default',config:{}}]);
  if(path==='/api/projects/p1/chats')return response([{id:'c1',project_id:'p1',title:'Smoke'}]);
  if(path==='/api/chats/c1')return response({chat:{id:'c1',title:'Smoke'},sources:[],runs:[],messages:generated?[{id:'m1',role:'assistant',content:'已完成',metadata:{artifact_ids:['a1']}}]:[]});
  if(path==='/api/chats/c1/messages'){const body=JSON.parse(init.body);requests.push(body);if(generated){modified=true;artifact.revision++;artifact.items[0].title='Updated login';}generated=true;return response({});}
  if(path==='/api/artifacts/a1')return response(artifact);
  throw new Error('Unexpected API '+path);
 }) as typeof fetch;
 try{
  render(<App/>);
  await waitFor(()=>assert.equal(screen.getByRole('button',{name:'发送消息',exact:true}).hasAttribute('disabled'),true));
  await screen.findByText('Smoke project');
  await waitFor(()=>assert.equal(screen.getByRole('button',{name:'新建生成会话'}).hasAttribute('disabled'),false));
  fireEvent.change(screen.getByLabelText('聊天输入'),{target:{value:'用户登录需求'}});
  await waitFor(()=>assert.equal(screen.getByRole('button',{name:'发送消息',exact:true}).hasAttribute('disabled'),false));
  fireEvent.click(screen.getByRole('button',{name:'发送消息',exact:true}));
  const workspace=screen.getByRole('region',{name:'用例工作区'});
  await within(workspace).findByText('Login');
  assert.equal(requests[0].intent,'generate_case');assert.equal(requests[0].as_requirement,true);
  fireEvent.change(screen.getByLabelText('聊天输入'),{target:{value:'修改标题'}});
  fireEvent.click(screen.getByRole('button',{name:'发送消息',exact:true}));
  await waitFor(()=>assert.equal(modified,true));
  assert.equal(requests[1].intent,'modify');assert.equal(requests[1].artifact_id,'a1');
  fireEvent.click(within(workspace).getByRole('button',{name:'导出 Excel'}));
  await screen.findByRole('button',{name:'下载 XLSX'});
 }finally{cleanup();globalThis.fetch=originalFetch;}
});
