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
const {render,fireEvent,screen,waitFor,cleanup,within,act,configure}=await import('@testing-library/react');
configure({asyncUtilTimeout:5000});
const {App}=await import('../src/App');
const {ProfileChangeDialog}=await import('../src/ProfileChangeDialog');
const originalFetch=globalThis.fetch;
afterEach(async()=>{await act(async()=>{});cleanup();globalThis.fetch=originalFetch;});
const json=(value:unknown,status=200)=>new Response(JSON.stringify(value),{status,headers:{'Content-Type':'application/json'}});
const prompt={id:'template:one:3',kind:'profile',title:'确认模板建议',message:'调整用例导出列与工作表名称。'};
const preview={prompt_id:prompt.id,profile_id:'profile',profile_name:'团队规范',expected_version:3,summary:prompt.message,template_ids:['one'],changes:[
 {key:'excel_columns',label:'用例导出列',before_present:true,after_present:true,before:[{field:'title',header:'原始标题'}],after:[{field:'goal',header:'验证目的',definition:'说明测试要验证什么',value_source:'ai',required:true},{field:'status',header:'执行状态',value_source:'default',default_value:false,required:false}]},
 {key:'sheet_name',label:'用例工作表名称',before_present:true,after_present:true,before:'Test Cases',after:'验收用例'},
]};
function fixture(){
 const writes:any[]=[];const reads:string[]=[];let profilesReads=0;
 const state:any={chat:{id:'chat',project_id:'project',profile_id:'profile',title:'模板学习'},sources:[],messages:[{id:'assistant',role:'assistant',content:prompt.message,metadata:{}}],runs:[],conversation_prompt:prompt};
 globalThis.fetch=(async(input:any,init:any={})=>{
  const path=String(input).replace(/^\/api/,'');
  if(path==='/projects')return json([{id:'project',name:'登录项目'}]);
  if(path==='/settings')return json({model:'mock'});
  if(path==='/projects/project/profiles'){profilesReads++;return json([{id:'profile',name:'团队规范',version:3,config:{}}]);}
  if(path==='/projects/project/chats')return json([state.chat,{id:'other',project_id:'project',title:'其他会话'}]);
  if(path==='/chats/chat')return json(state);
  if(path==='/chats/other')return json({chat:{id:'other',project_id:'project',title:'其他会话'},sources:[],messages:[],runs:[]});
  if(path.startsWith('/chats/chat/profile-change?')){reads.push(path);return json(preview);}
  if(path==='/chats/chat/profile-change/apply'){writes.push(JSON.parse(init.body));state.conversation_prompt=null;return json({status:'succeeded',profile:{id:'profile',name:'团队规范',version:4,config:{}},message:'已应用所选更改。'});}
  if(path.endsWith('/workspace-state'))return json({});
  if(path.endsWith('/turns'))throw new Error('Profile preview must not send chat turns');
  return json([]);
 }) as typeof fetch;
 return {writes,reads,state,profileReads:()=>profilesReads};
}

test('Profile suggestion opens a read-only comparison above the composer and preserves its draft',async()=>{
 const requests=fixture();render(<App/>);
 const button=await screen.findByRole('button',{name:'查看 Profile 更改'});
 const input=screen.getByLabelText('聊天输入') as HTMLTextAreaElement;
 fireEvent.change(input,{target:{value:'先保留我的问题'}});
 assert.ok(button.compareDocumentPosition(input)&Node.DOCUMENT_POSITION_FOLLOWING);
 assert.equal(button.closest('.composer'),null);assert.equal(screen.queryByRole('dialog'),null);
 assert.equal(screen.queryByRole('region',{name:'当前对话提示'}),null);
 assert.equal(requests.reads.length,0);assert.equal(requests.writes.length,0);
 fireEvent.click(button);
 const dialog=await screen.findByRole('dialog',{name:'查看 Profile 更改'});
 await within(dialog).findByRole('table',{name:'Profile 更改对比'});
 assert.equal(requests.reads.length,1);assert.equal(requests.writes.length,0);
 assert.ok(within(dialog).getByText('说明测试要验证什么'));
 assert.ok(within(dialog).getByText('false'));
 fireEvent.click(within(dialog).getByRole('button',{name:'暂不应用'}));
 assert.equal(screen.queryByRole('dialog'),null);assert.equal(input.value,'先保留我的问题');
 assert.equal(requests.writes.length,0);
});

test('only checked Profile fields apply and a successful save removes the current suggestion',async()=>{
 const requests=fixture();render(<App/>);
 fireEvent.click(await screen.findByRole('button',{name:'查看 Profile 更改'}));
 const checkbox=await screen.findByRole('checkbox',{name:'应用用例工作表名称'});
 fireEvent.click(checkbox);
 fireEvent.click(screen.getByRole('button',{name:'确认应用所选更改'}));
 await waitFor(()=>assert.equal(requests.writes.length,1));
 assert.deepEqual(requests.writes[0],{prompt_id:prompt.id,expected_version:3,selected_keys:['excel_columns']});
 await waitFor(()=>assert.equal(screen.queryByRole('dialog'),null));
 assert.equal(screen.queryByRole('button',{name:'查看 Profile 更改'}),null);
 assert.ok(requests.profileReads()>1);
});

test('empty selection disables save and stale-version errors remain in the dialog',async()=>{
 let writes=0;let closed=false;
 globalThis.fetch=(async(_input:any,init:any={})=>init.body?(writes++,json({detail:'Profile 已改变，请重新查看更改。'},409)):json(preview)) as typeof fetch;
 render(<ProfileChangeDialog chatId="chat" promptId={prompt.id} onClose={()=>{closed=true;}} onApplied={()=>{}}/>);
 await screen.findByRole('checkbox',{name:'应用用例导出列'});
 fireEvent.click(screen.getByRole('checkbox',{name:'选择全部更改'}));
 assert.ok((screen.getByRole('button',{name:'确认应用所选更改'}) as HTMLButtonElement).disabled);
 fireEvent.click(screen.getByRole('checkbox',{name:'应用用例导出列'}));
 fireEvent.click(screen.getByRole('button',{name:'确认应用所选更改'}));
 await screen.findByRole('alert');
 assert.match(screen.getByRole('alert').textContent??'',/Profile 已改变/);
 assert.equal(writes,1);assert.equal(closed,false);
});

test('switching chats closes Profile preview and does not apply it',async()=>{
 const requests=fixture();render(<App/>);
 fireEvent.click(await screen.findByRole('button',{name:'查看 Profile 更改'}));
 await screen.findByRole('table',{name:'Profile 更改对比'});
 fireEvent.click(screen.getByRole('button',{name:/其他会话/}));
 await waitFor(()=>assert.equal(screen.queryByRole('dialog'),null));
 assert.equal(requests.writes.length,0);
});
