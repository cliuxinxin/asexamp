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
const {render,fireEvent,screen,waitFor,cleanup,within}=await import('@testing-library/react');
const {App}=await import('../src/App');
const originalFetch=globalThis.fetch;
afterEach(()=>{cleanup();globalThis.fetch=originalFetch;});
const json=(value:unknown,status=200)=>new Response(JSON.stringify(value),{status,headers:{'Content-Type':'application/json'}});

function fixture(workspaceOverride?:(value:any,path:string)=>any){
 const calls:{path:string;method:string;body:any}[]=[];
 const artifacts:Record<string,any>={
  analysis:{id:'analysis',chat_id:'chat',project_id:'project',type:'analysis',title:'登录需求理解',revision:2,items:[{id:'R-1',title:'登录规则',description:'有效账号可登录。'}]},
  scenes:{id:'scenes',chat_id:'chat',project_id:'project',type:'scenarios',title:'登录场景',revision:3,items:[{id:'S-2',title:'锁定登录',description:'等待解锁'},{id:'S-1',title:'成功登录'}]},
  cases:{id:'cases',chat_id:'chat',project_id:'project',type:'cases',title:'登录测试用例',revision:1,items:[{id:'C-2',title:'无效密码锁定',scenario_id:'S-2',steps:[{action:'重复输入错误密码',expected:'账号锁定'}]},{id:'C-1',title:'有效账号登录',scenario_id:'S-1',steps:[{action:'输入正确密码',expected:'进入首页'}]}]},
 };
 const state:any={chat:{id:'chat',project_id:'project',title:'对话'},sources:[],messages:[
  {id:'initial',role:'assistant',content:'场景已整理。',metadata:{artifact_ids:['scenes']}},
  {id:'saved',role:'assistant',content:'场景修改已保存。',metadata:{turn_response:{id:'saved-turn',client_message_id:'saved-client',status:'succeeded',message:'场景修改已保存。',parts:[{type:'artifact',artifact_id:'scenes',revision:3}],pending:[],actions:[]}}},
 ],runs:[{id:'run',chat_id:'chat',status:'waiting',intent:'generate_case',mode:'hitp',stage:'scenario_review',updated_at:'2026-09-11T00:00:00Z',artifact_ids:['scenes'],interrupt:{type:'scenario_review',artifact_id:'scenes'}}]};
 let proposal:any=null,applied=false;
 function workspace(){return {artifact_id:'scenes',stages:[
  {key:'analysis',label:'需求理解',artifact_id:'analysis',title:'登录需求理解',revision:2,count:1,status:'current'},
  {key:'scenarios',label:'测试场景',artifact_id:'scenes',title:'登录场景',revision:3,count:2,status:'current'},
  {key:'cases',label:'测试用例',artifact_id:'cases',title:'登录测试用例',revision:artifacts.cases.revision,count:2,status:applied?'current':'stale'},
  {key:'review',label:'评审',artifact_id:null,revision:null,count:0,status:'missing'},
 ],impact:{status:applied?'current':'pending',summary:applied?'成果已更新':'需更新 1 条场景、2 条用例',affected:[],source_ids:[]},next_action:proposal?{kind:'apply',label:'应用更新',arguments:{proposal_id:proposal.id}}:applied?{kind:'confirm',label:'确认场景，继续生成用例',arguments:{run_id:'run'}}:{kind:'reconcile',label:'预览更新受影响成果',arguments:{artifact_id:'scenes',expected_revision:3,related_artifact_ids:['cases']}},current_gate:{run_id:'run',interrupt_id:'scene-gate',type:'scenario_review',artifact_id:'scenes',revision:3,control_version:1},pending_proposal:proposal};}
 function persist(parts:any[],message:string,status='succeeded'){const result={id:'turn-'+state.messages.length,client_message_id:'client',status,message,parts,pending:[],actions:[]};state.messages.push({id:result.id,role:'assistant',content:message,metadata:{turn_response:result}});return result;}
 globalThis.fetch=(async(input:any,init:RequestInit={})=>{
  const path=String(input).replace(/^\/api/,''),body=init.body?JSON.parse(String(init.body)):undefined;calls.push({path,method:init.method??'GET',body});
  if(path==='/projects')return json([{id:'project',name:'演示项目'}]);
  if(path==='/settings')return json({model:'mock'});
  if(path==='/projects/project/profiles')return json([{id:'profile',name:'Default',version:1,config:{}}]);
  if(path==='/projects/project/chats')return json([state.chat]);
  if(path==='/chats/chat')return json(state);
  if(path.startsWith('/chats/chat/workspace-state')){const value=workspace();return json(workspaceOverride?workspaceOverride(value,path):value);}
  const artifactMatch=path.match(/^\/artifacts\/([^/]+)(?:\/revisions\/\d+)?$/);
  if(artifactMatch&&artifacts[artifactMatch[1]])return json(artifacts[artifactMatch[1]]);
  if(/^\/artifacts\/[^/]+\/workspace(?:\?|$)/.test(path))return json({coverage:{totals:{},scenarios:[]},related_artifacts:[],sources:[]});
  if(path==='/chats/chat/turns'){
   if(body.command?.name==='artifact.preview'){
    proposal={id:'advanced-preview',artifact_id:'scenes',summary:'场景标题修改已准备好。',changes:[{artifact_id:'scenes',expected_revision:3,before_items:artifacts.scenes.items,items:artifacts.scenes.items.map((item:any)=>item.id==='S-2'?{...item,title:'按最新规则锁定登录'}:item)}]};
    return json(persist([{type:'diff',proposal_id:proposal.id,changes:proposal.changes}],proposal.summary,'needs_confirmation'));
   }
   if(body.command?.name==='workspace.reconcile'){
    proposal={id:'proposal-v280',artifact_id:'scenes',summary:'更新关联用例的锁定说明。',changes:[{artifact_id:'cases',expected_revision:1,before_items:artifacts.cases.items,items:artifacts.cases.items.map((item:any)=>item.id==='C-2'?{...item,title:'按最新场景验证账号锁定'}:item)}]};
    return json(persist([{type:'diff',proposal_id:proposal.id,changes:proposal.changes}],proposal.summary,'needs_confirmation'));
   }
   if(body.command?.name==='artifact.apply'){
    if(body.command.arguments.proposal_id!==proposal?.id)return json({detail:'Proposal ID is missing or stale'},409);
    artifacts.cases={...artifacts.cases,revision:2,items:proposal.changes[0].items};proposal=null;applied=true;
    return json(persist([{type:'artifact',artifact_id:'cases',revision:2}],'关联用例更新已保存。'));
   }
   return json(persist([],'已记录所选条目的要求。'));
  }
  return json({detail:'Unexpected API request: '+path},404);
 }) as typeof fetch;
 return {calls,artifacts,state};
}

async function ready(action=/^(预览更新受影响成果|应用更新)$/){render(<App/>);const workspace=await screen.findByRole('region',{name:'用例工作区'});await within(workspace).findByRole('checkbox',{name:'选择 S-2',exact:true});await within(workspace).findByRole('button',{name:action});return workspace;}
const posts=(calls:ReturnType<typeof fixture>['calls'])=>calls.filter(call=>call.method==='POST');
function submit(content:string){fireEvent.change(screen.getByLabelText('聊天输入'),{target:{value:content}});fireEvent.click(screen.getByRole('button',{name:'发送消息',exact:true}));}

test('workspace remains visible with one current artifact table and one authoritative impact action',async()=>{
 fixture();const workspace=await ready();
 assert.equal(screen.getAllByRole('region',{name:'用例工作区'}).length,1);
 assert.equal(screen.getAllByRole('checkbox',{name:'选择 S-2',exact:true}).length,1,'chat receipts and run cards must not duplicate the editable artifact');
 assert.equal(screen.getAllByRole('table').length,1,'only the current artifact is expanded initially');
 assert.equal(screen.getAllByRole('button',{name:'预览更新受影响成果',exact:true}).length,1);
 assert.ok(within(workspace).getByText('需更新 1 条场景、2 条用例'));
 assert.equal(screen.queryByRole('button',{name:'确认场景，继续生成用例',exact:true}),null,'unresolved impact takes precedence over continuation');
});

test('phase navigation opens the explicit artifact and keeps the authoritative reconciliation target',async()=>{
 const {calls}=fixture();const workspace=await ready();
 const phases=within(workspace).getByRole('navigation',{name:'生成流程'});
 fireEvent.click(within(phases).getByRole('button',{name:/测试用例/}));
 await within(workspace).findByRole('checkbox',{name:'选择 C-2',exact:true});
 assert.ok(calls.some(call=>call.path==='/artifacts/cases'));
 assert.equal(screen.queryByRole('checkbox',{name:'选择 S-2',exact:true}),null);
 fireEvent.click(within(workspace).getByRole('button',{name:'预览更新受影响成果',exact:true}));
 await waitFor(()=>assert.equal(posts(calls).length,1));
 assert.equal(posts(calls)[0].body.command.name,'workspace.reconcile');
 assert.equal(posts(calls)[0].body.command.arguments.artifact_id,'scenes','viewing cases must not retarget the pending scenario update');
 assert.equal(posts(calls)[0].body.command.arguments.expected_revision,3);
 assert.deepEqual(posts(calls)[0].body.command.arguments.related_artifact_ids,['cases']);
});

test('selected scenario and case rows bind chat requests to their artifact, revision and visible order',async()=>{
 const {calls}=fixture();const workspace=await ready();
 fireEvent.click(within(workspace).getByRole('checkbox',{name:'选择 S-2',exact:true}));
 fireEvent.click(within(workspace).getByRole('button',{name:/让 AI 修改选中 1 条/}));
 submit('细化这个场景的锁定条件。');
 await waitFor(()=>assert.equal(posts(calls).length,1));
 assert.equal(posts(calls)[0].body.artifact_id,'scenes');assert.equal(posts(calls)[0].body.artifact_revision,3);
 assert.deepEqual(posts(calls)[0].body.selected_ids,['S-2']);assert.deepEqual(posts(calls)[0].body.view_order,['S-2','S-1']);
 await waitFor(()=>assert.equal((screen.getByLabelText('聊天输入') as HTMLTextAreaElement).value,''));
 fireEvent.click(within(within(workspace).getByRole('navigation',{name:'生成流程'})).getByRole('button',{name:/测试用例/}));
 fireEvent.click(await within(workspace).findByRole('checkbox',{name:'选择 C-1',exact:true}));
 fireEvent.click(within(workspace).getByRole('button',{name:/让 AI 修改选中 1 条/}));
 submit('补充这条用例的预期结果。');
 await waitFor(()=>assert.equal(posts(calls).length,2));
 assert.equal(posts(calls)[1].body.artifact_id,'cases');assert.equal(posts(calls)[1].body.artifact_revision,1);
 assert.deepEqual(posts(calls)[1].body.selected_ids,['C-1']);assert.deepEqual(posts(calls)[1].body.view_order,['C-2','C-1']);
});

test('reconciliation restores one apply action after remount and applies the persisted proposal ID',async()=>{
 const {calls}=fixture();const workspace=await ready();
 fireEvent.click(within(workspace).getByRole('button',{name:'预览更新受影响成果',exact:true}));
 await screen.findByRole('button',{name:'应用更新',exact:true});
 assert.equal(screen.getAllByRole('button',{name:/应用.*修改|确认应用|应用更新/}).length,1,'chat history must not render a second actionable proposal');
 assert.equal(screen.queryByRole('button',{name:'预览更新受影响成果',exact:true}),null);
 assert.equal(screen.queryByRole('button',{name:'确认场景，继续生成用例',exact:true}),null);
 assert.equal(screen.getAllByRole('checkbox',{name:'选择 S-2',exact:true}).length,1);
 cleanup();await ready();
 assert.equal(screen.getAllByRole('button',{name:/应用.*修改|确认应用|应用更新/}).length,1,'the saved proposal remains owned by the workspace after remount');
 assert.equal(posts(calls).length,1,'restoring the proposal must not regenerate it');
 fireEvent.click(screen.getByRole('button',{name:'应用更新',exact:true}));
 await screen.findAllByText('关联用例更新已保存。');
 await waitFor(()=>assert.equal(posts(calls).length,2));
 assert.deepEqual(posts(calls).map(call=>call.body.command?.name),['workspace.reconcile','artifact.apply']);
 assert.equal(posts(calls)[1].body.command.arguments.proposal_id,'proposal-v280');
 await waitFor(()=>assert.equal(screen.queryByRole('button',{name:'应用更新',exact:true}),null));
});

test('newly saved phases become visible until the user explicitly chooses an earlier phase',async()=>{
 const {state}=fixture();const workspace=await ready();
 state.messages.push({id:'new-cases',role:'assistant',content:'用例已生成。',metadata:{artifact_ids:['cases']}});
 submit('查看当前进度。');
 await within(workspace).findByRole('checkbox',{name:'选择 C-2',exact:true});
 fireEvent.click(within(within(workspace).getByRole('navigation',{name:'生成流程'})).getByRole('button',{name:/需求理解/}));
 await within(workspace).findByRole('heading',{name:'登录需求理解',exact:true});
 state.messages.push({id:'new-scenes',role:'assistant',content:'场景已保存。',metadata:{artifact_ids:['scenes']}});
 await waitFor(()=>assert.equal((screen.getByLabelText('聊天输入') as HTMLTextAreaElement).value,''));
 submit('解释这个需求。');
 await waitFor(()=>assert.equal((screen.getByLabelText('聊天输入') as HTMLTextAreaElement).value,''));
 assert.ok(within(workspace).getByRole('heading',{name:'登录需求理解',exact:true}));
 assert.equal(within(workspace).queryByRole('checkbox',{name:'选择 S-2',exact:true}),null);
});

test('an unrelated waiting gate does not add a second primary action to the selected branch',async()=>{
 const {state,artifacts}=fixture(value=>({...value,current_gate:null,impact:{status:'current',summary:'当前场景已保存，可以生成用例。',affected:[],source_ids:[]},stages:value.stages.map((stage:any)=>stage.key==='cases'?{...stage,artifact_id:null,revision:null,count:0,status:'missing'}:stage),next_action:{kind:'generate',label:'生成测试用例',arguments:{intent:'generate_case',content:'生成测试用例',artifact_id:'scenes',as_requirement:false}}}));
 artifacts['other-scenes']={...artifacts.scenes,id:'other-scenes',title:'订单分支场景'};
 state.runs[0].artifact_ids=['other-scenes'];state.runs[0].interrupt.artifact_id='other-scenes';
 const workspace=await ready(/^生成测试用例$/);
 assert.equal(within(workspace).getAllByRole('button',{name:'生成测试用例',exact:true}).length,1);
 assert.equal((within(workspace).getByRole('button',{name:'生成测试用例',exact:true}) as HTMLButtonElement).disabled,false);
 assert.equal(screen.queryByRole('button',{name:'确认场景，继续生成用例',exact:true})===null,true,'the active branch endpoint has no gate; another branch’s waiting run must not render a competing confirmation');
});

test('opening review from scenarios retains the review phase after loading cases and expands its report',async()=>{
 const {artifacts,calls}=fixture(value=>({...value,stages:value.stages.map((stage:any)=>stage.key==='review'?{...stage,artifact_id:'cases',revision:1,count:2,status:'completed'}:stage)}));
 artifacts.cases.report={review_reports:[{summary:'已检查锁定边界，补充解锁后的恢复验证。'}]};
 const workspace=await ready();const phases=within(workspace).getByRole('navigation',{name:'生成流程'});
 fireEvent.click(within(phases).getByRole('button',{name:/评审/}));
 await within(workspace).findByRole('checkbox',{name:'选择 C-2',exact:true});
 assert.ok(calls.some(call=>call.path==='/artifacts/cases'));
 await waitFor(()=>assert.equal(within(phases).getByRole('button',{name:/评审/}).getAttribute('aria-current'),'step','loading the case artifact must preserve the explicit review selection'));
 assert.equal(within(phases).getByRole('button',{name:/测试用例/}).getAttribute('aria-current'),null);
 const summary=within(workspace).getByText('评审意见与说明',{exact:true});
 assert.equal((summary.closest('details') as HTMLDetailsElement).open,true,'review opens its report without an extra disclosure click');
 assert.ok(within(workspace).getByText('已检查锁定边界，补充解锁后的恢复验证。'));
});

test('reconciliation covers the branch even when the composer targets a selected row',async()=>{
 const {calls}=fixture();const workspace=await ready();
 fireEvent.click(within(workspace).getByRole('checkbox',{name:'选择 S-2',exact:true}));
 fireEvent.click(within(workspace).getByRole('button',{name:/让 AI 修改选中 1 条/}));
 fireEvent.change(screen.getByLabelText('聊天输入'),{target:{value:'只解释我选中的这个场景。'}});
 fireEvent.click(within(workspace).getByRole('button',{name:'预览更新受影响成果',exact:true}));
 await screen.findByRole('button',{name:'应用更新',exact:true});
 assert.equal(posts(calls).length,1);
 const body=posts(calls)[0].body;
 assert.equal(body.command.name,'workspace.reconcile');assert.equal(body.command.arguments.scope,'all');
 assert.equal(Object.hasOwn(body.command.arguments,'selected_ids'),false,'the branch action must not inherit the row selection');
 assert.equal(Object.hasOwn(body,'selected_ids'),false);
 assert.equal((screen.getByLabelText('聊天输入') as HTMLTextAreaElement).value,'只解释我选中的这个场景。','previewing branch updates must preserve the unsent selected-row chat draft');
});

test('the review next action starts the review workflow with the selected confirmation mode',async()=>{
 const {calls,state}=fixture(value=>({...value,artifact_id:'cases',current_gate:null,impact:{status:'current',summary:'测试用例已保存，可以开始评审。',affected:[],source_ids:[]},stages:value.stages.map((stage:any)=>stage.key==='cases'?{...stage,status:'current'}:stage),next_action:{kind:'generate',label:'评审测试用例',arguments:{artifact_id:'cases',intent:'review_case',content:'评审测试用例',as_requirement:false}}}));
 state.runs=[];
 state.messages=[{id:'saved-cases',role:'assistant',content:'用例已生成。',metadata:{artifact_ids:['cases']}}];
 render(<App/>);const workspace=await screen.findByRole('region',{name:'用例工作区'});
 await within(workspace).findByRole('checkbox',{name:'选择 C-2',exact:true});
 const review=await within(workspace).findByRole('button',{name:'评审测试用例',exact:true});
 fireEvent.click(screen.getByRole('button',{name:'任务设置',exact:true}));
 fireEvent.change(screen.getByLabelText('运行模式'),{target:{value:'hitp'}});
 fireEvent.click(review);
 await waitFor(()=>assert.equal(posts(calls).length,1));
 const body=posts(calls)[0].body;
 assert.equal(body.command.name,'workflow.start','the review phase must enter the workflow so its confirmation gates are retained');
 assert.equal(body.command.arguments.intent,'review_case');
 assert.equal(body.artifact_id,'cases');
 assert.equal(body.mode,'hitp');
 assert.equal(body.as_requirement,false);
});

test('advanced AI edits hand their preview back to the single workspace apply surface',async()=>{
 fixture();const workspace=await ready();
 fireEvent.click(within(workspace).getByText('更多工具与依据'));
 fireEvent.click(within(workspace).getByRole('button',{name:'AI 微调',exact:true}));
 fireEvent.change(screen.getByLabelText('本次微调或分析要求'),{target:{value:'改进场景标题。'}});
 fireEvent.click(screen.getByRole('button',{name:'预览修改',exact:true}));
 await within(workspace).findByRole('button',{name:'应用更新',exact:true});
 await waitFor(()=>assert.equal(screen.queryByRole('dialog')===null,true));
 assert.equal(screen.queryByRole('button',{name:'确认应用修改',exact:true}),null);
 assert.equal(screen.getAllByRole('button',{name:'应用更新',exact:true}).length,1);
});
