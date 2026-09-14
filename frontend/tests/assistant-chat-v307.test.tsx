import {JSDOM} from 'jsdom';
import {afterEach,test} from 'node:test';
import assert from 'node:assert/strict';
import type {TurnResponse} from '../src/types';

const dom=new JSDOM('<!doctype html><html><body></body></html>',{url:'http://localhost/'});
for(const key of ['window','document','HTMLElement','HTMLDialogElement','MutationObserver','Node','Event','MouseEvent','CustomEvent'])Object.defineProperty(globalThis,key,{value:(dom.window as any)[key],configurable:true,writable:true});
Object.defineProperty(globalThis,'navigator',{value:dom.window.navigator,configurable:true});
(globalThis as any).IS_REACT_ACT_ENVIRONMENT=true;
(globalThis as any).EventSource=class{addEventListener(){}close(){}};
dom.window.HTMLElement.prototype.scrollIntoView=function(){};
dom.window.HTMLDialogElement.prototype.showModal=function(){this.open=true;};
dom.window.HTMLDialogElement.prototype.close=function(){this.open=false;};
const React=await import('react');
const {render,screen,cleanup,fireEvent,waitFor}=await import('@testing-library/react');
const {App}=await import('../src/App');
const {AssistantReply,assistantReplyTexts}=await import('../src/AssistantReply');
const {ConversationParts}=await import('../src/ConversationParts');
const originalFetch=globalThis.fetch;
afterEach(()=>{cleanup();globalThis.fetch=originalFetch;});
const json=(value:unknown)=>new Response(JSON.stringify(value),{status:200,headers:{'Content-Type':'application/json'}});
const turn=(changes:Partial<TurnResponse>={}):TurnResponse=>({id:'turn',client_message_id:'client',status:'running',message:'',parts:[],pending:[],actions:[],...changes});

function Reply({response}:{response:TurnResponse}){
 const texts=assistantReplyTexts(response.message,response);
 return <article className="message assistant"><AssistantReply texts={texts}/><ConversationParts chatOnly compact displayedTexts={texts} response={response} onTarget={()=>{}} onChanged={()=>{}}/></article>;
}

test('public tool-call commentary appears immediately and remains before a final failure',()=>{
 const note='我先查看当前场景，再估算用例数量。';
 const response=turn({parts:[{type:'assistant_note',text:note}]});
 const view=render(<Reply response={response}/>);
 assert.equal(screen.getAllByText(note).length,1);
 assert.equal(document.querySelector('details'),null);
 const failed=turn({status:'failed',message:'本次操作未完成：模型连接中断。',parts:[...response.parts,{type:'diagnostic',reference_id:'call-1',category:'connection',message:'连接服务器失败',hints:[],log_path:'data/logs/tcg.log'}]});
 view.rerender(<Reply response={failed}/>);
 assert.equal(screen.getAllByText(note).length,1);
 const final=screen.getByText(failed.message);
 assert.ok(screen.getByText(note).compareDocumentPosition(final)&Node.DOCUMENT_POSITION_FOLLOWING);
 assert.ok(screen.getByText('查看排查信息'));
 assert.equal(document.querySelectorAll('.message.assistant').length,1);
});

test('App shows public AI replies inside chat once, with cited answer and draft preserved',async()=>{
 const note='我会按当前场景说明用例覆盖范围。';
 const final='这组用例覆盖登录成功和凭据错误两个分支。';
 const response=turn({status:'succeeded',message:final,parts:[
  {type:'assistant_note',text:note},{type:'assistant_note',text:note},
  {type:'assistant_note',text:final},{type:'answer',text:final,refs:['src-login#P1']},
  {type:'reasoning',text:'PRIVATE_REASONING'},{type:'tool_call',arguments:{secret:'PRIVATE_ARGUMENTS'}},
 ] as any});
 const chat={id:'chat',project_id:'project',title:'登录设计'};
 const snapshot={chat,sources:[],runs:[],conversation_prompt:null,messages:[
  {id:'user',role:'user',content:'解释当前用例。',metadata:{}},
  {id:'reply',role:'assistant',content:final,metadata:{turn_response:response}},
 ]};
 globalThis.fetch=(async(input:any)=>{
  const path=String(input).replace(/^\/api/,'');
  if(path==='/projects')return json([{id:'project',name:'登录项目'}]);
  if(path==='/settings')return json({model:'mock'});
  if(path==='/projects/project/profiles')return json([{id:'profile',name:'Default',version:1,config:{}}]);
  if(path==='/projects/project/chats')return json([chat]);
  if(path==='/chats/chat')return json(snapshot);
  if(path.startsWith('/chats/chat/workspace-state'))return json({stages:[],impact:{status:'current',affected:[],source_ids:[]},next_action:{kind:'none'}});
  return json([]);
 }) as typeof fetch;
 render(<App/>);
 await screen.findByText(note);
 assert.equal(screen.getAllByText(note).length,1);
 assert.equal(screen.getAllByText(final).length,1);
 assert.ok(screen.getByText(/依据：src-login#P1/));
 assert.equal(screen.getByText(note).closest('article'),screen.getByText(final).closest('article'));
 assert.ok(screen.getByText(note).compareDocumentPosition(screen.getByText(final))&Node.DOCUMENT_POSITION_FOLLOWING);
 assert.equal(screen.queryByText('PRIVATE_REASONING'),null);
 assert.equal(screen.queryByText('PRIVATE_ARGUMENTS'),null);
 const input=screen.getByLabelText('聊天输入') as HTMLTextAreaElement;
 fireEvent.change(input,{target:{value:'再解释异常分支。'}});
 assert.equal(input.value,'再解释异常分支。');
 assert.equal(input.value.includes(note),false);
});

test('older replies stay readable and response text can fill an empty receipt',()=>{
 assert.deepEqual(assistantReplyTexts('旧版回答'),['旧版回答']);
 assert.deepEqual(assistantReplyTexts('',turn({message:'已保存更新。',parts:[{type:'assistant_note',text:'   '},{type:'assistant_note',text:'先核对当前对象。'}]})),['先核对当前对象。','已保存更新。']);
});

test('a dialogue parent preview displays added rows without loading nonexistent revision zero',async()=>{
 const calls:string[]=[];
 globalThis.fetch=(async(input:any)=>{calls.push(String(input));return new Response('',{status:404});}) as typeof fetch;
 const response=turn({status:'needs_confirmation',message:'已整理这次对话补充，请确认后保存。',parts:[{type:'diff',proposal_id:'proposal',changes:[{
  artifact_id:'new-analysis',title:'对话补充需求',expected_revision:0,before_items:[],
  items:[{id:'R-CHAT-1',title:'并发登录校验',description:'用户要求验证同一账号同时登录的行为。',refs:['src-chat#P1']}],
 }]}]});
 render(<Reply response={response}/>);
 await waitFor(()=>assert.ok(screen.getByText('R-CHAT-1 · 并发登录校验 · 新增')));
 assert.ok(screen.getByText('对话补充需求 · v0 → 新版本'));
 assert.ok(screen.getByText('新增 1 · 更新 0 · 删除 0'));
 assert.deepEqual(calls,[]);
 assert.equal(screen.queryByText('应用此修改'),null);
});
