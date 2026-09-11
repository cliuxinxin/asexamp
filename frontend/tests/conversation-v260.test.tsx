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
 let draft:any={id:'draft',run_id:'run',revision:1,question_set_version:'qv1',questions:[{id:'q-lock',question:'是否锁定？',answer:'',suggestion:{answer:'锁定 10 分钟',basis:'待业务确认',refs:[],confidence:'assumption'},adopted:false}],answer:'',submitted:false,source_id:null,shared:false};
 function persist(parts:any[],message='已处理。'){const result={id:'turn-'+state.messages.length,client_message_id:'client',status:'succeeded',message,parts,pending:[],actions:[]};state.messages.push({id:result.id,role:'assistant',content:message,metadata:{turn_response:result}});return result;}
 globalThis.fetch=(async(input:any,init:RequestInit={})=>{
  const path=String(input).replace('/api',''),body=init.body?JSON.parse(String(init.body)):undefined;calls.push({path,body,method:init.method??'GET'});
  if(path==='/projects')return json([{id:'project',name:'演示项目'}]);
  if(path==='/settings')return json({model:'mock'});
  if(path.endsWith('/profiles'))return json([{id:'profile',name:'Default',version:1,config:{}}]);
  if(path==='/projects/project/chats')return json([state.chat]);
  if(path==='/chats/chat')return json(state);
  if(path==='/artifacts/scenes'||path==='/artifacts/scenes/revisions/3')return json(artifact);
  if(path==='/runs/run/clarification-draft'){
   if(init.method==='PATCH'){
    if(body.expected_revision!==draft.revision)return json({detail:'草稿已更新，请重新读取。'},409);
    if(body.adopt_all||body.adopt_ids?.length){draft.questions[0].answer=draft.questions[0].suggestion.answer;draft.questions[0].adopted=true;draft.answer='问题：是否锁定？\n回答：锁定 10 分钟';}
    if(body.answer!==undefined){draft.answer=body.answer;draft.questions[0].answer=body.answer;draft.questions[0].adopted=!!body.answer.trim();}
    if(body.answers){draft.questions[0].answer=body.answers['q-lock']??draft.questions[0].answer;draft.answer='问题：是否锁定？\n回答：'+draft.questions[0].answer;}
    draft={...draft,questions:draft.questions.map((q:any)=>({...q})),revision:draft.revision+1};
   }
   return json(draft);
  }
  if(path==='/chats/chat/turns'){
   if(turn)return turn(body);
   if(body.command?.name==='clarification.save'){draft={...draft,submitted:true,source_id:'source',revision:draft.revision+1};return json(persist([{type:'clarification_draft',draft}], '答案已保存，仍等待确认。'));}
   if(body.command?.name==='clarification.share'){draft={...draft,shared:true,revision:draft.revision+1};return json(persist([{type:'clarification_draft',draft}], '已共享到项目。'));}
   if(body.command)return json(persist([], '操作已执行。'));
   return json(persist([{type:'estimate',data:estimate}]));
  }
  if(path.includes('/workspace'))return json({coverage:{totals:{},scenarios:[]},related_artifacts:[],sources:[]});
  return json({detail:'Unexpected API request: '+path},404);
 }) as typeof fetch;
 return{calls,state,artifact,persist,getDraft:()=>draft};
}
async function ready(){render(<App/>);await screen.findByRole('button',{name:'并排查看结果'});}
function submit(text:string){fireEvent.change(screen.getByLabelText('聊天输入'),{target:{value:text}});fireEvent.click(screen.getByRole('button',{name:'发送消息',exact:true}));}
const posts=(calls:any[])=>calls.filter(call=>call.method==='POST');

test('running composer sends one unified turn with stable view context and renders a saved estimate',async()=>{
 const {calls}=fixture('running');await ready();submit('这些场景需要多少用例？');
 await screen.findByRole('table',{name:'用例数量估算'});
 assert.equal(posts(calls).length,1);assert.equal(posts(calls)[0].path,'/chats/chat/turns');
 assert.equal(posts(calls)[0].body.artifact_revision,3);assert.deepEqual(posts(calls)[0].body.view_order,['S-2','S-1']);assert.ok(posts(calls)[0].body.client_message_id);
 assert.ok(screen.getByText('来源：登录场景 · v3'));
});

test('pending read keeps actual scenario confirmation available and confirm uses a bound command',async()=>{
 let resolve!:(value:Response)=>void;const pending=new Promise<Response>(value=>{resolve=value;});
 const {calls}=fixture('waiting','scenario_review',body=>body.command?Promise.resolve(json({status:'succeeded',message:'继续',parts:[],pending:[],actions:[]})):pending);
 await ready();submit('解释第三个场景。');await waitFor(()=>assert.equal(posts(calls).length,1));
 const confirm=screen.getByRole('button',{name:'确认场景，继续生成用例'}) as HTMLButtonElement;assert.equal(confirm.disabled,false);
 fireEvent.click(confirm);await waitFor(()=>assert.equal(posts(calls).length,2));assert.equal(posts(calls)[1].body.command.name,'workflow.continue');assert.equal(posts(calls)[1].body.command.arguments.run_id,'run');
 await act(async()=>resolve(json({status:'succeeded',message:'已解释',parts:[],pending:[],actions:[]})));
});

test('failed response preserves edited composer and retry reuses the client message id',async()=>{
 let count=0;const {calls}=fixture('','',async()=>++count===1?json({detail:'网络暂不可用'},503):json({status:'succeeded',message:'已处理',parts:[],pending:[],actions:[]}));await ready();submit('总结已有成果');
 await screen.findByText('网络暂不可用');assert.equal((screen.getByLabelText('聊天输入') as HTMLTextAreaElement).value,'总结已有成果');submit('总结已有成果');
 await waitFor(()=>assert.equal(posts(calls).length,2));assert.equal(posts(calls)[0].body.client_message_id,posts(calls)[1].body.client_message_id);
});

test('stored typed parts display actual steps, before and after differences, coverage and separate downloads',async()=>{
 const {persist}=fixture('');persist([{type:'answer',text:'已有用例基于确认规则。',refs:['source#1']},{type:'case_details',artifact_id:'cases',revision:2,items:[{id:'C-2',title:'锁定测试',steps:[{action:'连续输错密码',expected:'账号锁定 10 分钟'}]}]},{type:'diff',proposal_id:'proposal',changes:[{artifact_id:'scenes',expected_revision:3,before_items:[{id:'S-2',title:'锁定登录',description:'等待解锁'}],items:[{id:'S-2',title:'锁定登录',description:'十分钟后解锁'}]}]},{type:'coverage',data:{totals:{scenarios:1,scenarios_with_cases:1,cases:1},scenarios:[{id:'S-2',title:'锁定登录',case_ids:['C-2'],case_count:1}]}},{type:'files',files:[{name:'测试场景.xlsx',url:'/api/exports/scenarios'},{name:'测试用例.xlsx',url:'/api/exports/cases'}]}]);await ready();
 assert.ok(screen.getByRole('table',{name:'C-2 步骤与预期结果'}));assert.ok(screen.getByText('连续输错密码'));assert.ok(screen.getByText('账号锁定 10 分钟'));assert.ok(screen.getByText('十分钟后解锁'));assert.ok(screen.getByText('修改前'));assert.ok(screen.getByText('修改后'));
 assert.equal(screen.getByRole('link',{name:'测试场景.xlsx'}).getAttribute('href'),'/api/exports/scenarios');assert.equal(screen.getByRole('link',{name:'测试用例.xlsx'}).getAttribute('href'),'/api/exports/cases');assert.ok(screen.getByRole('region',{name:'场景和用例覆盖'}));
});

test('clarification adoption persists, reload restores edits, save and share never imply continue',async()=>{
 const {calls,getDraft}=fixture('waiting','clarification');await ready();await screen.findByRole('button',{name:'采用全部建议'});fireEvent.click(screen.getByRole('button',{name:'采用全部建议'}));
 await waitFor(()=>assert.equal(getDraft().revision,2));assert.equal(screen.queryByRole('button',{name:'采用全部建议'}),null);
 const answer=screen.getByLabelText('回答澄清问题');fireEvent.change(answer,{target:{value:'明确锁定 15 分钟。'}});fireEvent.blur(answer);
 await waitFor(()=>assert.equal(getDraft().answer,'明确锁定 15 分钟。'));cleanup();await ready();await waitFor(()=>assert.equal((screen.getByLabelText('回答澄清问题') as HTMLTextAreaElement).value,'明确锁定 15 分钟。'));
 fireEvent.click(screen.getByRole('button',{name:'保存答案',exact:true}));await waitFor(()=>assert.ok(getDraft().submitted));
 fireEvent.click(screen.getByRole('button',{name:'共享到项目',exact:true}));await waitFor(()=>assert.ok(getDraft().shared));
 assert.deepEqual(posts(calls).map(call=>call.body.command?.name),['clarification.save','clarification.share']);
 assert.ok(calls.filter(call=>call.method==='PATCH').every(call=>Number.isInteger(call.body.expected_revision)));
});

test('artifact estimate shortcut calls the same read capability and renders its typed response',async()=>{
 const {calls}=fixture('waiting','scenario_review',async()=>json({id:'estimate-turn',status:'succeeded',message:'已估算当前场景',parts:[{type:'estimate',data:estimate}],pending:[],actions:[]}));await ready();
 fireEvent.click(screen.getAllByRole('button',{name:'估算用例数量（不生成）'})[0]);fireEvent.click(screen.getByRole('button',{name:'开始估算'}));
 await waitFor(()=>assert.equal(posts(calls).length,1));assert.equal(posts(calls)[0].path,'/chats/chat/turns');assert.equal(posts(calls)[0].body.command.name,'artifact.estimate');
 assert.ok(await screen.findByText(/预计 3–5 条用例/));
});

test('chat adopts suggestions in the authoritative draft while the visible run card updates',async()=>{
 const {getDraft,persist}=fixture('waiting','clarification',async()=>{
  const draft=getDraft();draft.revision++;draft.answer='问题：是否锁定？\n回答：锁定 10 分钟';draft.questions[0].answer='锁定 10 分钟';draft.questions[0].adopted=true;
  return json(persist([{type:'clarification_draft',draft}], '已采用建议，等待保存。'));
 });await ready();await screen.findByRole('button',{name:'采用全部建议'});submit('采用全部建议，先不保存。');
 await waitFor(()=>assert.ok(!screen.queryByRole('button',{name:'采用全部建议'})));assert.equal((screen.getByLabelText('回答澄清问题') as HTMLTextAreaElement).value,getDraft().answer);
 assert.equal(getDraft().submitted,false);
});

test('scenario export creates a real downloadable file through the unified export capability',async()=>{
 const {calls}=fixture('',undefined,async()=>json({id:'export-turn',status:'succeeded',message:'已导出',parts:[{type:'files',files:[{name:'登录场景.xlsx',url:'/api/conversation-files/file-one'}]}],pending:[],actions:[]}));
 const prior=globalThis.fetch;globalThis.fetch=(async(input:any,init:any)=>String(input).endsWith('/export-options')?json({profiles:[],snapshot:{}}):prior(input,init)) as typeof fetch;
 await ready();fireEvent.click(screen.getByRole('button',{name:'导出 Excel'}));fireEvent.click(await screen.findByRole('button',{name:'下载 XLSX'}));
 const download=await screen.findByRole('link',{name:'登录场景.xlsx'});assert.equal(download.getAttribute('href'),'/api/conversation-files/file-one');assert.equal(posts(calls)[0].body.command.name,'artifact.export');assert.equal(posts(calls)[0].body.command.arguments.artifact_id,'scenes');assert.equal(posts(calls)[0].body.command.arguments.revision,3);assert.equal(posts(calls)[0].body.command.arguments.use_snapshot,true);
});

test('later hold instruction can be sent while a prior edit is awaiting its response',async()=>{
 let resolve!:(response:Response)=>void;const pending=new Promise<Response>(done=>{resolve=done;});
 const {calls}=fixture('running','cases',body=>body.content==='先别继续。'?Promise.resolve(json({id:'hold',status:'succeeded',message:'已记录暂停要求',parts:[],pending:[],actions:[]})):pending);
 await ready();submit('修改第三个场景，并继续生成。');await waitFor(()=>assert.equal(posts(calls).length,1));
 submit('先别继续。');await waitFor(()=>assert.equal(posts(calls).length,2));assert.equal(posts(calls)[1].body.content,'先别继续。');assert.notEqual(posts(calls)[0].body.client_message_id,posts(calls)[1].body.client_message_id);
 await act(async()=>resolve(json({id:'first',status:'succeeded',message:'修改已保存，遵循暂停要求',parts:[],pending:[],actions:[]})));
});

test('remote draft revision never silently rebases and overwrites an unsaved local answer',async()=>{
 const {calls,getDraft}=fixture('waiting','clarification');await ready();const input=await screen.findByLabelText('回答澄清问题');
 fireEvent.change(input,{target:{value:'本地编辑：锁定 15 分钟。'}});
 const remote=getDraft();remote.revision=2;remote.answer='另一处确认：锁定 20 分钟。';remote.questions[0].answer=remote.answer;
 await act(async()=>window.dispatchEvent(new CustomEvent('tcg:clarification-changed',{detail:{runId:'run'}})));
 assert.equal((input as HTMLTextAreaElement).value,'本地编辑：锁定 15 分钟。');fireEvent.blur(input);
 await screen.findByText('草稿已更新，请重新读取。');assert.equal(calls.find(call=>call.method==='PATCH')?.body.expected_revision,1);assert.equal(getDraft().answer,'另一处确认：锁定 20 分钟。');assert.equal((input as HTMLTextAreaElement).value,'本地编辑：锁定 15 分钟。');
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

test('draft submit binds the original control version so a later hold supersedes its pending continue',async()=>{
 let resolveSave!:(response:Response)=>void;const saving=new Promise<Response>(resolve=>{resolveSave=resolve;});
 const {state,calls,getDraft}=fixture('waiting','clarification',body=>{
  if(body.command?.name==='clarification.save')return saving;
  if(body.content==='先别继续。'){state.runs[0].control_version=4;return Promise.resolve(json({id:'hold',status:'succeeded',message:'已记录暂停',parts:[],pending:[],actions:[]}));}
  if(body.command?.name==='workflow.continue')return Promise.resolve(json({detail:'继续请求已被后来的暂停取代'},409));
  return Promise.resolve(json({id:'other',status:'succeeded',message:'已处理',parts:[],pending:[],actions:[]}));
 });state.runs[0].control_version=3;state.runs[0].interrupt_id='gate-one';
 await ready();fireEvent.click(await screen.findByRole('button',{name:'采用全部建议'}));await waitFor(()=>assert.equal(getDraft().revision,2));fireEvent.click(screen.getByLabelText('保存澄清到项目'));fireEvent.click(screen.getByRole('button',{name:'提交并继续'}));
 await waitFor(()=>assert.equal(posts(calls).length,1));submit('先别继续。');await waitFor(()=>assert.equal(posts(calls).length,2));
 await act(async()=>{const draft={...getDraft(),submitted:true,source_id:'source',revision:3};resolveSave(json({id:'saved',status:'succeeded',message:'答案已保存',parts:[{type:'clarification_draft',draft}],pending:[],actions:[]}));});
 await screen.findByText('继续请求已被后来的暂停取代');const resume=posts(calls).find(call=>call.body.command?.name==='workflow.continue');assert.equal(resume.body.command.arguments.expected_control_version,3);assert.equal(resume.body.command.arguments.interrupt_id,'gate-one');
});

test('typing during an owned draft save keeps the newer text and advances only its own saved base revision',async()=>{
 const {calls,getDraft}=fixture('waiting','clarification');const prior=globalThis.fetch;let deferred:{input:any;init:any;resolve:(response:Response)=>void}|undefined;let delay=true;
 globalThis.fetch=(async(input:any,init:any)=>{if(init?.method==='PATCH'&&delay){delay=false;return new Promise<Response>(resolve=>{deferred={input,init,resolve};});}return prior(input,init);}) as typeof fetch;
 await ready();const input=await screen.findByLabelText('回答澄清问题');fireEvent.change(input,{target:{value:'先填 10 分钟。'}});fireEvent.blur(input);await waitFor(()=>assert.ok(deferred));
 fireEvent.change(input,{target:{value:'继续编辑为 15 分钟。'}});
 await act(async()=>deferred!.resolve(await prior(deferred!.input,deferred!.init)));
 assert.equal((input as HTMLTextAreaElement).value,'继续编辑为 15 分钟。');fireEvent.blur(input);
 await waitFor(()=>assert.equal(calls.filter(call=>call.method==='PATCH').length,2));assert.equal(calls.filter(call=>call.method==='PATCH')[1].body.expected_revision,2);
 await waitFor(()=>assert.equal(getDraft().answer,'继续编辑为 15 分钟。'));
});

test('a persisted failed receipt shows the actual failure and retains the composer text',async()=>{
 fixture('','',async()=>json({id:'failed-turn',status:'failed',message:'修改未保存：成果版本已变化。',parts:[],pending:[],actions:[]}));await ready();submit('修改第三个场景。');
 await screen.findByText('修改未保存：成果版本已变化。');assert.equal((screen.getByLabelText('聊天输入') as HTMLTextAreaElement).value,'修改第三个场景。');
});

test('ambiguous object requests show the actual candidate order, IDs and saved revisions',async()=>{
 const {state}=fixture('');state.messages.push({id:'choose-artifact',role:'assistant',content:'请说明要查看哪份成果。',metadata:{turn_response:{id:'choose-turn',client_message_id:'choice',status:'needs_input',message:'请说明要查看哪份成果。',parts:[],actions:[],pending:[{id:'pending-choice',question:'请选择一份已保存的场景成果。',candidates:[{id:'scenes-a',title:'登录场景',revision:2},{id:'scenes-b',title:'订单场景',revision:5}]}]}}});
 await ready();const pending=screen.getByLabelText('待确认事项');const candidates=within(pending).getAllByRole('listitem');assert.equal(candidates.length,2);assert.match(candidates[0].textContent??'',/登录场景.*scenes-a.*v2/);assert.match(candidates[1].textContent??'',/订单场景.*scenes-b.*v5/);
});
