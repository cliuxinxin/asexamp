import {JSDOM} from 'jsdom';
import {afterEach,test} from 'node:test';
import assert from 'node:assert/strict';

const dom=new JSDOM('<!doctype html><html><body></body></html>',{url:'http://localhost/'});
for(const key of ['window','document','HTMLElement','MutationObserver','Node','Event','MouseEvent','CustomEvent'])Object.defineProperty(globalThis,key,{value:(dom.window as any)[key],configurable:true,writable:true});
Object.defineProperty(globalThis,'navigator',{value:dom.window.navigator,configurable:true});
(globalThis as any).IS_REACT_ACT_ENVIRONMENT=true;
const React=await import('react');
const {render,fireEvent,screen,waitFor,cleanup,within}=await import('@testing-library/react');
const {ClarificationDraftEditor}=await import('../src/ClarificationDraftEditor');
const {ConversationContext}=await import('../src/conversation');
const originalFetch=globalThis.fetch;
afterEach(()=>{cleanup();globalThis.fetch=originalFetch;});
const json=(value:unknown,status=200)=>new Response(JSON.stringify(value),{status,headers:{'Content-Type':'application/json'}});

const firstQuestion={id:'q-lock',question:'错误密码后锁定多久？',answer:'',adopted:false,suggestion:{answer:'锁定 10 分钟',basis:'等待业务确认',refs:[],confidence:'assumption'}};
const secondQuestion={id:'q-revoke',question:'移出白名单后何时撤销权限？',answer:'',adopted:false,suggestion:{answer:'立即撤销访问权限',basis:'权限需求第 2 段',refs:['source#2'],confidence:'supported'}};
const firstAnswer='问题：错误密码后锁定多久？\n回答：锁定 10 分钟';

function fixture({partial=false,provisional=false,manual=false}={}){
 const calls:{path:string;method:string;body:any}[]=[];
 const questions=partial?[firstQuestion,secondQuestion]:[firstQuestion];
 const initial={id:'draft',run_id:'run',revision:1,question_set_version:'questions-v1',questions:questions.map(question=>({...question})),answer:'',submitted:false,source_id:null,shared:false};
 const savedQuestions=[{...firstQuestion,answer:'锁定 10 分钟',adopted:!manual},...(partial?[secondQuestion]:[])];
 const patched={...initial,revision:2,questions:savedQuestions,answer:firstAnswer};
 const saved={...patched,revision:manual?3:2,submitted:true,source_id:'clarification-source',shared:!provisional,status:provisional?'provisional':'confirmed'};
 let current:any=initial;
 globalThis.fetch=(async(input:any,init:RequestInit={})=>{
  const path=String(input).replace(/^\/api/,''),method=init.method??'GET',body=init.body?JSON.parse(String(init.body)):undefined;
  calls.push({path,method,body});
  if(path==='/runs/run/clarification-draft'&&method==='GET')return json(current);
  if(path==='/runs/run/clarification-draft'&&method==='PATCH'){current=patched;return json(patched);}
  if(path==='/chats/chat/turns'&&method==='POST'){
   current=saved;
   return json({id:'turn',status:'succeeded',message:'答案已保存并更新理解，仍等待确认理解。',parts:[{type:'clarification_draft',draft:saved}],pending:[],actions:[]});
  }
  return json({detail:'Unexpected request: '+method+' '+path},404);
 }) as typeof fetch;
 render(<ConversationContext.Provider value={{chatId:'chat',onChanged:()=>{}}}><ClarificationDraftEditor runId="run" controlVersion={7} interruptId="clarification-gate" onChanged={()=>{}}/></ConversationContext.Provider>);
 return {calls};
}

const commands=(calls:ReturnType<typeof fixture>['calls'])=>calls.filter(call=>call.method==='POST').map(call=>call.body.command);

test('adopting one answer submits the bound clarification and removes the answered question without continuing the workflow',async()=>{
 const {calls}=fixture();fireEvent.click(await screen.findByRole('button',{name:'采用此答案'}));
 await waitFor(()=>assert.equal(screen.queryByText(firstQuestion.question)===null,true));
 assert.deepEqual(commands(calls),[{name:'clarification.adopt',arguments:{run_id:'run',expected_control_version:7,adopt_ids:['q-lock'],submit:true,status:'confirmed',save_to_project:true,expected_revision:1,question_set_version:'questions-v1'}}]);
 assert.equal((screen.getByLabelText('回答澄清问题') as HTMLTextAreaElement).value,firstAnswer);
 assert.match(screen.getByText(/草稿 v2/).textContent??'',/答案已保存/);
 assert.equal(screen.queryByRole('button',{name:'采用此答案'})===null,true);
});

test('partial adoption removes only the answered question and leaves the remaining suggestion actionable',async()=>{
 const {calls}=fixture({partial:true});
 const list=await screen.findByRole('list');
 const first=within(list).getAllByRole('listitem')[0];fireEvent.click(within(first).getByRole('button',{name:'采用此答案'}));
 await waitFor(()=>assert.equal(screen.queryByText(firstQuestion.question)===null,true));
 assert.ok(screen.getByText(secondQuestion.question));
 assert.equal(screen.getAllByRole('listitem').length,1);
 assert.equal(screen.getAllByRole('button',{name:'采用此答案'}).length,1);
 assert.deepEqual(commands(calls).map(command=>command.name),['clarification.adopt']);
 assert.deepEqual(commands(calls)[0].arguments.adopt_ids,['q-lock']);
 assert.equal(commands(calls)[0].arguments.submit,true);
});

test('the main submit flushes the typed answer then updates understanding while preserving unanswered questions and the gate',async()=>{
 const {calls}=fixture({partial:true,manual:true});
 const answer=await screen.findByLabelText('回答澄清问题');
 fireEvent.change(answer,{target:{value:firstAnswer}});
 fireEvent.click(screen.getByRole('button',{name:'提交答案并更新理解'}));
 await waitFor(()=>assert.match(screen.getByText(/草稿 v3/).textContent??'',/答案已保存/));
 assert.equal(screen.queryByText(firstQuestion.question)===null,true);
 assert.ok(screen.getByText(secondQuestion.question));
 const writes=calls.filter(call=>call.method!=='GET');
 assert.equal(writes.length,2);assert.equal(writes[0].method,'PATCH');
 assert.equal(writes[0].body.answer,firstAnswer);assert.equal(writes[0].body.expected_revision,1);
 assert.deepEqual(commands(calls),[{name:'clarification.save',arguments:{run_id:'run',expected_control_version:7,save_to_project:true,expected_revision:2,question_set_version:'questions-v1'}}]);
 assert.equal((answer as HTMLTextAreaElement).value,firstAnswer);
});

test('using a suggestion provisionally submits provisional status and does not confirm or continue the workflow',async()=>{
 const {calls}=fixture({provisional:true});fireEvent.click(await screen.findByRole('button',{name:'先按假设使用'}));
 await waitFor(()=>assert.equal(screen.queryByText(firstQuestion.question)===null,true));
 assert.deepEqual(commands(calls).map(command=>command.name),['clarification.adopt']);
 assert.equal(commands(calls)[0].arguments.status,'provisional');assert.equal(commands(calls)[0].arguments.submit,true);
 assert.deepEqual(commands(calls)[0].arguments.adopt_ids,['q-lock']);
 assert.match(screen.getByText(/草稿 v2/).textContent??'',/答案已保存/);
 assert.doesNotMatch(screen.getByText(/草稿 v2/).textContent??'',/已共享到项目/);
 assert.equal(screen.queryByRole('button',{name:/继续/})===null,true);
});
