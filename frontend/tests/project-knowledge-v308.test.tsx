import {JSDOM} from 'jsdom';
import {test,afterEach} from 'node:test';
import assert from 'node:assert/strict';

const dom=new JSDOM('<!doctype html><html><body></body></html>',{url:'http://localhost:8000/'});
for(const key of ['window','document','HTMLElement','HTMLDialogElement','MutationObserver','Node','Event','MouseEvent'])Object.defineProperty(globalThis,key,{value:(dom.window as any)[key],configurable:true,writable:true});
Object.defineProperty(globalThis,'navigator',{value:dom.window.navigator,configurable:true});
(globalThis as any).IS_REACT_ACT_ENVIRONMENT=true;
dom.window.HTMLDialogElement.prototype.showModal=function(){this.open=true;};
dom.window.HTMLDialogElement.prototype.close=function(){this.open=false;};
const React=await import('react');
const {render,fireEvent,screen,waitFor,cleanup}=await import('@testing-library/react');
const {MemoryDialog}=await import('../src/MemoryDialog');
const originalFetch=globalThis.fetch;
afterEach(()=>{cleanup();globalThis.fetch=originalFetch;});
const response=(value:unknown,status=200)=>new Response(JSON.stringify(value),{status,headers:{'Content-Type':'application/json'}});
const rule={id:'shared-1',name:'登录澄清',text:'账号锁定 5 分钟',created_at:'2026-09-08T09:00:00Z',active:true,chat_id:'origin',origin_chat_title:'登录模块测试',origin_created_at:'2026-09-08T09:00:00Z',source_version:1,enabled_in_chat:true};

test('knowledge rule can be disabled and re-enabled only for this chat while keeping its origin and project sharing',async()=>{
 const calls:{method:string;path:string;body?:any}[]=[];let changed=0;
 globalThis.fetch=(async(input:any,init:any={})=>{
  const path=String(input),method=init.method??'GET';calls.push({path,method,body:init.body?JSON.parse(init.body):undefined});
  if(path.includes('/project-knowledge/'))return response({clarifications:[{...rule,enabled_in_chat:JSON.parse(init.body).enabled}],samples:[],preference_version:method==='PATCH'?calls.filter(c=>c.method==='PATCH').length+1:1,requires_rebuild:true,message:'本次对话规则已更新，下次继续将重新理解需求。'});
  if(path.includes('/shared-context'))return response({clarifications:[rule],samples:[],preference_version:1});
  if(path.endsWith('/memory'))return response([]);
  throw new Error(path);
 }) as typeof fetch;
 render(<MemoryDialog projectId="project" chatId="chat" onClose={()=>{}} onChanged={()=>{changed++;}}/>);
 await screen.findByText(/登录模块测试/);
 assert.ok(screen.getByText(/2026-09-08/));
 fireEvent.click(screen.getByRole('switch',{name:'本次对话使用 登录澄清'}));
 await waitFor(()=>assert.equal(screen.getByRole('switch',{name:'本次对话使用 登录澄清'}).getAttribute('aria-checked'),'false'));
 assert.ok(screen.getByText('账号锁定 5 分钟'));assert.ok(screen.getByRole('button',{name:'取消共享 登录澄清'}));
 assert.ok(screen.getByText(/下次继续将重新理解需求/));
 fireEvent.click(screen.getByRole('switch',{name:'本次对话使用 登录澄清'}));
 await waitFor(()=>assert.equal(changed,2));
 const writes=calls.filter(call=>call.method==='PATCH');
 assert.deepEqual(writes.map(call=>call.body),[{enabled:false,expected_version:1},{enabled:true,expected_version:2}]);
 assert.ok(writes.every(call=>call.path.endsWith('/chats/chat/project-knowledge/shared-1')));
 assert.ok(calls.some(call=>call.path.endsWith('/shared-context?chat_id=chat')));
 assert.ok(!calls.some(call=>call.method==='DELETE'));
});

test('failed busy toggle preserves inclusion state and exposes actionable error',async()=>{
 globalThis.fetch=(async(input:any)=>String(input).includes('/project-knowledge/')?response({detail:'任务正在处理，请等待当前步骤完成后调整项目规则。'},409):response(String(input).endsWith('/memory')?[]:{clarifications:[rule],samples:[],preference_version:4})) as typeof fetch;
 render(<MemoryDialog projectId="project" chatId="chat" onClose={()=>{}}/>);
 fireEvent.click(await screen.findByRole('switch',{name:'本次对话使用 登录澄清'}));
 await screen.findByText('任务正在处理，请等待当前步骤完成后调整项目规则。');
 assert.equal(screen.getByRole('switch',{name:'本次对话使用 登录澄清'}).getAttribute('aria-checked'),'true');
});

test('usage receipt lists the actual captured rules and opens their management without advancing the workflow',async()=>{
 const {ConversationParts}=await import('../src/ConversationParts');let opened=0;
 render(<ConversationParts response={{id:'turn',client_message_id:'m',status:'succeeded',message:'正在理解',parts:[{type:'project_knowledge',count:1,facts:[{...rule,source_id:rule.id,refs:['shared-1#P1']}],run_id:'run'}],pending:[],actions:[]}} onOpenKnowledge={()=>{opened++;}} onTarget={()=>assert.fail('not an edit')} onChanged={()=>assert.fail('not a mutation')}/>);
 assert.ok(screen.getByText(/1 条项目历史规则/));
 fireEvent.click(screen.getByText('查看本次引入的规则'));
 assert.ok(screen.getByText('账号锁定 5 分钟'));assert.ok(screen.getByText(/登录模块测试/));
 fireEvent.click(screen.getByRole('button',{name:'查看 / 管理项目知识库'}));assert.equal(opened,1);
});

test('provenance badges use exact provided refs, distinguish supplements and samples, and never fetch full source bodies',async()=>{
 const {SourceBadges}=await import('../src/SourceBadges');
 globalThis.fetch=(async()=>{assert.fail('labels must use saved provenance metadata');}) as typeof fetch;
 render(<SourceBadges refs={['doc#P1','history#P2','chat#P1','sample#P1','doc#P99']} sources={[
  {source_id:'doc',classification:'current_document',refs:['doc#P1'],name:'需求.docx'},
  {source_id:'history',classification:'project_knowledge',refs:['history#P2'],name:'登录规则'},
  {source_id:'chat',classification:'chat_supplement',refs:['chat#P1']},
  {source_id:'sample',classification:'format_sample',refs:['sample#P1']},
 ]}/>);
 assert.ok(screen.getByText('当前文档'));assert.ok(screen.getByText('项目历史记忆'));
 assert.ok(screen.getByText('对话补充'));assert.ok(screen.getByText('格式样例'));assert.ok(screen.getByText('来源待查看'));
});

test('requirement and scenario viewers expose saved history provenance directly next to their content',async()=>{
 const {ArtifactCard}=await import('../src/ArtifactCard');
 const artifact={id:'scenario',chat_id:'chat',type:'scenarios',title:'场景结果',revision:2,items:[{id:'S1',title:'账号解锁',refs:['history#P1']}],report:{source_provenance:[{source_id:'history',name:'解锁澄清',classification:'project_knowledge',source_version:1,origin_chat_title:'登录模块测试',refs:['history#P1']}]}};
 globalThis.fetch=(async(input:any)=>{assert.ok(!String(input).includes('/sources/'));return response({lineage_rows:[]});}) as typeof fetch;
 const props={onTarget:()=>{},onChanged:()=>{},simplified:true,readOnly:true};
 const view=render(<ArtifactCard {...props} id={artifact.id} snapshot={artifact}/>);
 assert.ok(screen.getByText('项目历史记忆'));assert.ok(screen.getByTitle(/解锁澄清 · 来源 v1 · 来自《登录模块测试》/));
 view.rerender(<ArtifactCard {...props} id="analysis" snapshot={{...artifact,id:'analysis',type:'analysis',title:'需求理解'}}/>);
 await waitFor(()=>assert.ok(screen.getByText('需求理解')));
 assert.ok(screen.getByText('项目历史记忆'));
});
