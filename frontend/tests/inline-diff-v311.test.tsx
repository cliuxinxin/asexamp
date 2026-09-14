import {JSDOM} from 'jsdom';
import {afterEach,test} from 'node:test';
import assert from 'node:assert/strict';
const dom=new JSDOM('<!doctype html><html><body></body></html>',{url:'http://localhost/'});
for(const key of ['window','document','HTMLElement','MutationObserver','Node','Event','MouseEvent','CustomEvent'])Object.defineProperty(globalThis,key,{value:(dom.window as any)[key],configurable:true,writable:true});
Object.defineProperty(globalThis,'navigator',{value:dom.window.navigator,configurable:true});
(globalThis as any).IS_REACT_ACT_ENVIRONMENT=true;
const React=await import('react');
const {render,fireEvent,screen,waitFor,cleanup,act}=await import('@testing-library/react');
const {ConversationParts}=await import('../src/ConversationParts');
const {ConversationContext}=await import('../src/conversation');
const originalFetch=globalThis.fetch;
afterEach(async()=>{await act(async()=>{});cleanup();globalThis.fetch=originalFetch;});
const json=(value:unknown,status=200)=>new Response(JSON.stringify(value),{status,headers:{'Content-Type':'application/json'}});
const prompt={id:'revision:p1:2',kind:'artifact_proposal',title:'确认修改预览',message:'建议调整登录场景。',proposal_id:'p1',artifact_id:'scenes',artifact_revision:2};
const changes=[{artifact_id:'scenes',type:'scenarios',title:'登录场景',expected_revision:2,before_items:[{id:'S1',title:'普通登录',requirement_ids:['R1']}],items:[{id:'S1',title:'并发登录',requirement_ids:[]}]}];
const response={id:'turn',client_message_id:'message',status:'needs_confirmation',message:'建议调整登录场景。',parts:[{type:'diff',proposal_id:'p1',changes}],pending:[],actions:[]};
function fixture(status='succeeded'){
 const writes:any[]=[];
 globalThis.fetch=(async(input:any,init:any={})=>{
  const path=String(input).replace(/^\/api/,'');
  if(path==='/chats/chat/workspace-state')return json({pending_proposal:{id:'p1',prompt_id:prompt.id,summary:'建议调整登录场景。',changes}});
  if(path==='/chats/chat/turns'){writes.push(JSON.parse(init.body));return json({id:'saved',status,message:status==='succeeded'?'已处理这项修改。':'修改预览已改变，请查看当前提示。',parts:[],pending:[],actions:[]});}
  throw new Error('Unexpected '+path);
 }) as typeof fetch;
 return writes;
}
function View(props:any={}){
 return <ConversationContext.Provider value={{chatId:'chat',onChanged:()=>{}}}><ConversationParts {...{response,currentPrompt:prompt,onTarget:()=>{},onChanged:()=>{},compact:true,chatOnly:true,...props} as any}/><textarea aria-label="输入草稿" defaultValue="接着还想问一个问题"/></ConversationContext.Provider>;
}

test('active changes are visible inside the message and accept through the exact prompt without consuming input',async()=>{
 const writes=fixture();let changed=0;render(<View onChanged={()=>{changed++;}}/>);
 await screen.findByRole('table',{name:'S1 修改对比'});
 assert.equal(screen.queryByRole('dialog'),null);
 assert.ok(screen.getByText('N/A'));assert.equal(writes.length,0);
 const accept=screen.getByRole('button',{name:'接受修改'});
 fireEvent.click(accept);fireEvent.click(accept);
 await screen.findByText('已处理这项修改。');
 assert.equal(writes.length,1);assert.equal(writes[0].reply_to,prompt.id);assert.equal(writes[0].reply_kind,'confirm');
 assert.equal(writes[0].command.name,'artifact.apply');assert.equal(writes[0].command.arguments.proposal_id,'p1');
 assert.equal((screen.getByLabelText('输入草稿') as HTMLTextAreaElement).value,'接着还想问一个问题');
 assert.equal(changed,1);assert.equal(screen.queryByRole('button',{name:'接受修改'}),null);
});

test('reject is a separate anchored action and a stale response leaves the visible diff locked',async()=>{
 const writes=fixture('needs_input');render(<View/>);
 await screen.findByRole('table',{name:'S1 修改对比'});
 fireEvent.click(screen.getByRole('button',{name:'拒绝修改'}));
 await screen.findByRole('alert');
 assert.equal(writes[0].command.name,'artifact.discard');assert.equal(writes[0].reply_to,prompt.id);
 assert.ok(screen.getByRole('table',{name:'S1 修改对比'}));
 assert.ok((screen.getByRole('button',{name:'接受修改'}) as HTMLButtonElement).disabled);
 assert.ok((screen.getByRole('button',{name:'拒绝修改'}) as HTMLButtonElement).disabled);
 assert.equal((screen.getByLabelText('输入草稿') as HTMLTextAreaElement).value,'接着还想问一个问题');
});

test('historical changes stay read-only even while a different proposal is active',async()=>{
 const writes=fixture();render(<View currentPrompt={{...prompt,proposal_id:'p2',id:'revision:p2:2'}}/>);
 const archived=screen.getByText('修改预览 · 查看当时的差异').closest('details')!;
 archived.open=true;
 fireEvent(archived,new Event('toggle'));
 await screen.findByRole('table',{name:'S1 修改对比'});
 assert.equal(screen.queryByRole('button',{name:'接受修改'}),null);
 assert.equal(writes.length,0);
});

test('review card shows proposed expected results and only explicit acceptance resumes the pipeline',async()=>{
 const writes:any[]=[];let revise=0;
 const gate={id:'gate:review:1',kind:'case_result_review',title:'评审建议',message:'请核对建议',run_id:'run',artifact_id:'cases',artifact_revision:1,review_proposal_id:'review_1'};
 const before={id:'TC1',title:'登录',steps:[{action:'点击登录',expected:'成功'}]};
 const after={...before,steps:[{action:'输入有效账号，再点击登录',expected:'显示用户首页'}]};
 globalThis.fetch=(async(input:any,init:any={})=>{
  const path=String(input).replace(/^\/api/,'');
  if(path==='/runs/run/review-proposals/review_1')return json({id:'review_1',artifact_id:'cases',expected_revision:1,report:{summary:'补充操作与预期'},changes:[{op:'update',id:'TC1',before,after,fields:['steps']}]});
  if(path==='/chats/chat/turns'){writes.push(JSON.parse(init.body));return json({id:'saved',status:'succeeded',message:'已确认评审建议',parts:[],pending:[],actions:[]});}
  throw new Error('Unexpected '+path);
 }) as typeof fetch;
 render(<View currentPrompt={gate} response={{...response,parts:[{type:'diff',proposal_id:'review_1',changes:[]}]}} onRevise={()=>{revise++;}}/>);
 await screen.findByText('显示用户首页');
 assert.equal(writes.length,0);fireEvent.click(screen.getByRole('button',{name:'补充评审意见'}));
 assert.equal(revise,1);assert.equal(writes.length,0);
 fireEvent.click(screen.getByRole('button',{name:'接受评审建议'}));
 await screen.findByText('已确认评审建议');
 assert.equal(writes[0].reply_to,gate.id);assert.equal(writes[0].command.name,'workflow.resume');assert.equal(writes[0].command.arguments.action,'approved');
});

test('review rejection explicitly preserves the current cases and releases the shared busy state',async()=>{
 const writes:any[]=[],busy:boolean[]=[];
 const gate={id:'gate:review:1',kind:'case_result_review',title:'评审建议',message:'请核对建议',run_id:'run',artifact_id:'cases',artifact_revision:1,review_proposal_id:'review_1'};
 globalThis.fetch=(async(input:any,init:any={})=>{
  const path=String(input).replace(/^\/api/,'');
  if(path==='/runs/run/review-proposals/review_1')return json({id:'review_1',artifact_id:'cases',expected_revision:1,report:{summary:'暂无条目更改'},changes:[]});
  if(path==='/chats/chat/turns'){writes.push(JSON.parse(init.body));return json({id:'saved',status:'succeeded',message:'已拒绝评审建议，保留当前用例。',parts:[],pending:[],actions:[]});}
  throw new Error('Unexpected '+path);
 }) as typeof fetch;
 render(<View currentPrompt={gate} response={{...response,parts:[{type:'diff',proposal_id:'review_1',changes:[]}]}} onBusyChange={(value:boolean)=>busy.push(value)}/>);
 await waitFor(()=>assert.ok(!(screen.getByRole('button',{name:'拒绝评审建议'}) as HTMLButtonElement).disabled));
 fireEvent.click(screen.getByRole('button',{name:'拒绝评审建议'}));
 await screen.findByText('已拒绝评审建议，保留当前用例。');
 assert.equal(writes[0].reply_to,gate.id);assert.equal(writes[0].command.arguments.action,'rejected');
 assert.match(writes[0].content,/保留当前用例/);assert.deepEqual(busy,[true,false]);
});

test('large inline diffs expose more rows progressively without hiding additional changed fields forever',async()=>{
 const rows=Array.from({length:9},(_,index)=>({id:'S'+(index+1),title:'新场景'+index,description:'说明',priority:'P1',type:'Business',requirement_ids:[],refs:['source#P1']}));
 const bulk=[{...changes[0],before_items:[],items:rows}];
 globalThis.fetch=(async()=>json({pending_proposal:{id:'p1',prompt_id:prompt.id,changes:bulk}})) as typeof fetch;
 render(<View response={{...response,parts:[{type:'diff',proposal_id:'p1',changes:bulk}]}}/>);
 await screen.findByRole('table',{name:'S1 修改对比'});
 assert.equal(screen.getAllByRole('table').length,2);
 fireEvent.click(screen.getByRole('button',{name:'再查看 5 条修改'}));
 assert.equal(screen.getAllByRole('table').length,7);
 fireEvent.click(screen.getAllByRole('button',{name:'展开其余 3 个字段'})[0]);
 assert.ok(screen.getByText('需求依据'));
});

for(const failure of ['load','resolve'])test(`${failure} failure can refresh the unchanged prompt and accept the revalidated proposal`,async()=>{
 let reads=0;const writes:any[]=[];
 globalThis.fetch=(async(input:any,init:any={})=>{
  const path=String(input).replace(/^\/api/,'');
  if(path==='/chats/chat/workspace-state'){
   reads++;
   if(failure==='load'&&reads===1)return json({detail:'临时连接失败'},503);
   return json({pending_proposal:{id:'p1',prompt_id:prompt.id,changes}});
  }
  if(path==='/chats/chat/turns'){
   writes.push(JSON.parse(init.body));
   if(failure==='resolve'&&writes.length===1)return json({detail:'临时连接失败'},503);
   return json({id:'saved',status:'succeeded',message:'刷新后已接受修改。',parts:[],pending:[],actions:[]});
  }
  throw new Error('Unexpected '+path);
 }) as typeof fetch;
 // onChanged is deliberately a no-op: the prompt and surrounding props stay identical.
 render(<View/>);
 if(failure==='resolve'){
  await screen.findByRole('table',{name:'S1 修改对比'});
  fireEvent.click(screen.getByRole('button',{name:'接受修改'}));
 }
 await screen.findByRole('alert');
 assert.ok((screen.getByRole('button',{name:'接受修改'}) as HTMLButtonElement).disabled);
 fireEvent.click(screen.getByRole('button',{name:'刷新当前提示'}));
 await waitFor(()=>assert.ok(!(screen.getByRole('button',{name:'接受修改'}) as HTMLButtonElement).disabled));
 assert.equal(reads,2);assert.equal(screen.queryByRole('alert'),null);
 fireEvent.click(screen.getByRole('button',{name:'接受修改'}));
 await screen.findByText('刷新后已接受修改。');
 assert.equal(writes.length,failure==='resolve'?2:1);
 assert.ok(writes.every(body=>body.reply_to===prompt.id&&body.command.arguments.proposal_id==='p1'));
 assert.equal((screen.getByLabelText('输入草稿') as HTMLTextAreaElement).value,'接着还想问一个问题');
});

test('refreshing an errored preview never re-enables acceptance if the authoritative proposal changed',async()=>{
 let reads=0;const writes:any[]=[];
 globalThis.fetch=(async(input:any,init:any={})=>{
  const path=String(input).replace(/^\/api/,'');
  if(path==='/chats/chat/workspace-state'){
   reads++;
   if(reads===1)return json({detail:'临时连接失败'},503);
   return json({pending_proposal:{id:'p2',prompt_id:'revision:p2:3',changes}});
  }
  writes.push(JSON.parse(init.body));return json({});
 }) as typeof fetch;
 render(<View/>);
 await screen.findByRole('alert');
 fireEvent.click(screen.getByRole('button',{name:'刷新当前提示'}));
 await waitFor(()=>assert.match(screen.getByRole('alert').textContent??'',/修改预览已改变/));
 assert.equal(reads,2);assert.ok((screen.getByRole('button',{name:'接受修改'}) as HTMLButtonElement).disabled);
 fireEvent.click(screen.getByRole('button',{name:'接受修改'}));assert.equal(writes.length,0);
});
