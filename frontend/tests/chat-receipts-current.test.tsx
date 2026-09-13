// Retained current contracts extracted from conversation-v260.test.tsx; archived legacy controls remain in legacy-tests/frontend.
import {JSDOM} from 'jsdom';
import {afterEach,test} from 'node:test';
import assert from 'node:assert/strict';

const dom=new JSDOM('<!doctype html><html><body></body></html>',{url:'http://localhost:8000/'});
for(const key of ['window','document','HTMLElement','HTMLDialogElement','MutationObserver','Node','Event','MouseEvent','CustomEvent'])Object.defineProperty(globalThis,key,{value:(dom.window as any)[key],configurable:true,writable:true});
Object.defineProperty(globalThis,'navigator',{value:dom.window.navigator,configurable:true});
(globalThis as any).IS_REACT_ACT_ENVIRONMENT=true;
(globalThis as any).EventSource=class{addEventListener(){}close(){}};
dom.window.HTMLElement.prototype.scrollIntoView=function(){};
dom.window.HTMLDialogElement.prototype.showModal=function(){this.open=true;};
dom.window.HTMLDialogElement.prototype.close=function(){this.open=false;};
const React=await import('react');
const {render,fireEvent,screen,waitFor,cleanup,act,within}=await import('@testing-library/react');
const {App}=await import('../src/App');
const originalFetch=globalThis.fetch;
afterEach(()=>{cleanup();globalThis.fetch=originalFetch;});
const json=(value:any,status=200)=>new Response(JSON.stringify(value),{status,headers:{'Content-Type':'application/json'}});
const estimate={artifact_id:'scenes',artifact_revision:3,title:'登录场景',min_count:3,max_count:5,scenarios:[{scenario_id:'S-2',title:'登录失败锁定',min_count:3,max_count:5,rationale:'阈值边界',assumptions:['单一账号类型']}]};
function fixture(status='waiting',interrupt='scenario_review',turn?:(body:any)=>Promise<Response>){
 const calls:{path:string;body:any;method:string}[]=[];
 const artifact={id:'scenes',chat_id:'chat',project_id:'project',type:'scenarios',title:'登录场景',revision:3,items:[{id:'S-2',title:'锁定登录',description:'等待解锁'},{id:'S-1',title:'成功登录'}]};
 const state:any={chat:{id:'chat',project_id:'project',title:'对话'},sources:[],messages:[{id:'initial',role:'assistant',content:'场景已整理。',metadata:{artifact_ids:['scenes']}}],runs:status?[{id:'run',chat_id:'chat',status,intent:'generate_case',mode:'hitp',stage:interrupt,updated_at:'2026-09-11T00:00:00Z',artifact_ids:['scenes'],interrupt:{type:interrupt,artifact_id:'scenes',questions:interrupt==='clarification'?['是否锁定？']:[]}}]:[]};
 function persist(parts:any[],message='已处理。'){const result={id:'turn-'+state.messages.length,client_message_id:'client',status:'succeeded',message,parts,pending:[],actions:[]};state.messages.push({id:result.id,role:'assistant',content:message,metadata:{turn_response:result}});return result;}
 globalThis.fetch=(async(input:any,init:RequestInit={})=>{
  const path=String(input).replace('/api',''),body=init.body?JSON.parse(String(init.body)):undefined;calls.push({path,body,method:init.method??'GET'});
  if(path==='/projects')return json([{id:'project',name:'演示项目'}]);
  if(path==='/settings')return json({model:'mock'});
  if(path.endsWith('/profiles'))return json([{id:'profile',name:'Default',version:1,config:{}}]);
  if(path==='/projects/project/chats')return json([state.chat]);
  if(path==='/chats/chat')return json(state);
  if(path==='/artifacts/scenes'||path==='/artifacts/scenes/revisions/3')return json(artifact);
  if(path==='/chats/chat/turns'){
   if(turn)return turn(body);
   return json(persist([{type:'estimate',data:estimate}]));
  }
  if(path.includes('/workspace'))return json({coverage:{totals:{},scenarios:[]},related_artifacts:[],sources:[]});
  return json({detail:'Unexpected API request: '+path},404);
 }) as typeof fetch;
 return{calls,state,artifact,persist,};
}
async function ready(){render(<App/>);const open=await screen.findByRole('button',{name:/查看当前成果/});fireEvent.click(open);const row=await screen.findByLabelText('选择 S-2');fireEvent.click(row);fireEvent.click(screen.getByRole('button',{name:'在对话中修改所选'}));}
function submit(text:string){fireEvent.change(screen.getByLabelText('聊天输入'),{target:{value:text}});fireEvent.click(screen.getByRole('button',{name:'发送消息',exact:true}));}
const posts=(calls:any[])=>calls.filter(call=>call.method==='POST');

test('running composer sends one unified turn with stable view context and renders a saved estimate',async()=>{
 const {calls}=fixture('running');await ready();submit('这些场景需要多少用例？');
 await screen.findByRole('table',{name:'用例数量估算'});
 assert.equal(posts(calls).length,1);assert.equal(posts(calls)[0].path,'/chats/chat/turns');
 assert.equal(posts(calls)[0].body.artifact_revision,3);assert.deepEqual(posts(calls)[0].body.view_order,['S-2','S-1']);assert.ok(posts(calls)[0].body.client_message_id);
 assert.ok(screen.getByText('来源：登录场景 · v3'));
});

test('failed response preserves edited composer and retry reuses the client message id',async()=>{
 let count=0;const {calls}=fixture('','',async()=>++count===1?json({detail:'网络暂不可用'},503):json({status:'succeeded',message:'已处理',parts:[],pending:[],actions:[]}));await ready();submit('总结已有成果');
 await screen.findByText('网络暂不可用');assert.equal((screen.getByLabelText('聊天输入') as HTMLTextAreaElement).value,'总结已有成果');submit('总结已有成果');
 await waitFor(()=>assert.equal(posts(calls).length,2));assert.equal(posts(calls)[0].body.client_message_id,posts(calls)[1].body.client_message_id);
});

test('later hold instruction can be sent while a prior edit is awaiting its response',async()=>{
 let resolve!:(response:Response)=>void;const pending=new Promise<Response>(done=>{resolve=done;});
 const {calls}=fixture('running','cases',body=>body.content==='先别继续。'?Promise.resolve(json({id:'hold',status:'succeeded',message:'已记录暂停要求',parts:[],pending:[],actions:[]})):pending);
 await ready();submit('修改第三个场景，并继续生成。');await waitFor(()=>assert.equal(posts(calls).length,1));
 submit('先别继续。');await waitFor(()=>assert.equal(posts(calls).length,2));assert.equal(posts(calls)[1].body.content,'先别继续。');assert.notEqual(posts(calls)[0].body.client_message_id,posts(calls)[1].body.client_message_id);
 await act(async()=>resolve(json({id:'first',status:'succeeded',message:'修改已保存，遵循暂停要求',parts:[],pending:[],actions:[]})));
});

test('a saved artifact turn advances the composer version used by the next request',async()=>{
 const {artifact,persist,calls}=fixture('','',async()=>{artifact.revision=4;return json(persist([{type:'artifact',artifact_id:'scenes',revision:4}], '场景修改已保存。'));});await ready();submit('把第二个场景条件写清楚。');
 await screen.findByText('场景修改已保存。');await waitFor(()=>assert.equal((screen.getByLabelText('聊天输入') as HTMLTextAreaElement).value,''));submit('再总结当前场景。');
 await waitFor(()=>assert.equal(posts(calls).length,2));assert.equal(posts(calls)[1].body.artifact_revision,4);
});

test('conversational Profile selection becomes the next composer profile without reverting an unsent local choice',async()=>{
 const {calls,state}=fixture('','',async()=>{state.chat.profile_id='profile-two';return json({id:'profile-turn',status:'succeeded',message:'已切换 Profile',parts:[],pending:[],actions:[]});});
 const prior=globalThis.fetch;globalThis.fetch=(async(input:any,init:any)=>String(input).endsWith('/profiles')?json([{id:'profile',name:'Default',version:1,config:{}},{id:'profile-two',name:'另一模板',version:1,config:{}}]):prior(input,init)) as typeof fetch;
 await ready();submit('切换到另一模板。');await waitFor(()=>assert.equal((screen.getByLabelText('运行 Profile') as HTMLSelectElement).value,'profile-two'));
 submit('总结当前范围。');await waitFor(()=>assert.equal(posts(calls).length,2));assert.equal(posts(calls)[1].body.profile_id,'profile-two');
 fireEvent.change(screen.getByLabelText('运行 Profile'),{target:{value:'profile'}});fireEvent.change(screen.getByLabelText('聊天输入'),{target:{value:'这是新的本地草稿'}});assert.equal((screen.getByLabelText('运行 Profile') as HTMLSelectElement).value,'profile');
});

test('a persisted failed receipt shows the actual failure and retains the composer text',async()=>{
 fixture('','',async()=>json({id:'failed-turn',status:'failed',message:'修改未保存：成果版本已变化。',parts:[],pending:[],actions:[]}));await ready();submit('修改第三个场景。');
 await screen.findByText('修改未保存：成果版本已变化。');assert.equal((screen.getByLabelText('聊天输入') as HTMLTextAreaElement).value,'修改第三个场景。');
});
