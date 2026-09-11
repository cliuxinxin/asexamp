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
const response=(value:unknown,status=200)=>new Response(JSON.stringify(value),{status,headers:{'Content-Type':'application/json'}});

test('project memory shows shared clarifications and samples and unshares without deleting memory',async()=>{
 const {MemoryDialog}=await import('../src/MemoryDialog');const original=globalThis.fetch;const calls:string[]=[];
 globalThis.fetch=(async(url:any,init:any={})=>{const path=String(url);calls.push(init.method+' '+path);if(path.endsWith('/shared-context/shared-1')){assert.equal(init.method,'DELETE');return response({});}if(path.endsWith('/shared-context'))return response({clarifications:[{id:'shared-1',name:'登录澄清',text:'账号锁定 5 分钟',created_at:'2026-09-11',active:true,chat_id:'c1'}],samples:[{profile_id:'p1',profile_name:'默认设计',version:2,count:2}]});if(path.endsWith('/memory'))return response([{id:'m1',kind:'preference',content:'中文输出',active:true}]);throw new Error(path);}) as typeof fetch;
 try{render(<MemoryDialog projectId="project" onClose={()=>{}}/>);await screen.findByText('账号锁定 5 分钟');assert.ok(screen.getByText('默认设计 · 2 条样例 · v2'));fireEvent.click(screen.getByRole('button',{name:'取消共享 登录澄清'}));await waitFor(()=>assert.equal(Boolean(screen.queryByText('账号锁定 5 分钟')),false));assert.ok(screen.getByText('中文输出'));assert.equal(calls.filter(call=>call.startsWith('DELETE')).length,1);}finally{cleanup();globalThis.fetch=original;}
});

for(const share of [true,false])test(`clarification adoption hides prompts and submits explicit project sharing ${share}`,async()=>{
 const {RunCard}=await import('../src/RunCard');const original=globalThis.fetch;let submitted:any;
 globalThis.fetch=(async(url:any,init:any)=>{assert.equal(String(url),'/api/runs/r-share/resume');submitted=JSON.parse(init.body);return response({});}) as typeof fetch;
 try{render(<RunCard run={{id:'r-share',status:'waiting',intent:'generate_case',mode:'hitp',stage:'clarification',updated_at:'2026-09-11',artifact_ids:[],interrupt:{type:'clarification',questions:['锁定多久？'],question_suggestions:[{question:'锁定多久？',answer:'5 分钟。',basis:'需求已明确',refs:['src#P1'],confidence:'supported'}]}}} onChanged={()=>{}} onTarget={()=>{}}/>);assert.equal((screen.getByLabelText('保存澄清到项目') as HTMLInputElement).checked,true);if(!share)fireEvent.click(screen.getByLabelText('保存澄清到项目'));fireEvent.click(screen.getByRole('button',{name:'采用全部建议'}));assert.equal(Boolean(screen.queryByRole('button',{name:'采用此答案'})),false);assert.equal(submitted,undefined);fireEvent.click(screen.getByRole('button',{name:'提交并继续'}));await waitFor(()=>assert.equal(submitted.save_to_project,share));assert.match(submitted.answer,/5 分钟/);}finally{cleanup();globalThis.fetch=original;}
});

test('clarification sharing reports changes to its parent and honors controlled preference',async()=>{
 const {RunCard}=await import('../src/RunCard');const original=globalThis.fetch;let preference=true,submitted:any;
 globalThis.fetch=(async(url:any,init:any)=>{submitted=JSON.parse(init.body);return response({});}) as typeof fetch;
 function Card(){const [shared,setShared]=React.useState(true);return <RunCard run={{id:'r-controlled',status:'waiting',intent:'generate_case',mode:'hitp',stage:'clarification',updated_at:'2026-09-11',artifact_ids:[],interrupt:{type:'clarification',questions:['锁定多久？']}}} saveToProject={shared} onSaveToProjectChange={value=>{preference=value;setShared(value);}} onChanged={()=>{}} onTarget={()=>{}}/>;}
 try{render(<Card/>);fireEvent.click(screen.getByLabelText('保存澄清到项目'));assert.equal(preference,false);assert.equal((screen.getByLabelText('保存澄清到项目') as HTMLInputElement).checked,false);fireEvent.change(screen.getByLabelText('回答澄清问题'),{target:{value:'5 分钟'}});fireEvent.click(screen.getByRole('button',{name:'提交并继续'}));await waitFor(()=>assert.equal(submitted.save_to_project,false));}finally{cleanup();globalThis.fetch=original;}
});

test('supplement submits chosen staged files and text, locks confirmation, and retains draft after rejection',async()=>{
 const {RunCard}=await import('../src/RunCard');const original=globalThis.fetch;let resolveRequest:(value:Response)=>void=()=>{};let submitted:any;let changed=0;
 globalThis.fetch=(async(url:any,init:any)=>{assert.equal(String(url),'/api/runs/r-supplement/supplement');submitted=JSON.parse(init.body);return new Promise<Response>(resolve=>{resolveRequest=resolve;});}) as typeof fetch;
 try{render(<RunCard run={{id:'r-supplement',chat_id:'chat',status:'waiting',intent:'generate_case',mode:'hitp',stage:'scenario_review',updated_at:'2026-09-11',artifact_ids:[],interrupt:{type:'scenario_review'}}} sources={[{id:'s-new',name:'新增规则.txt',role:'supplement',characters:20}]} onChanged={()=>{changed++;}} onTarget={()=>{}}/>);fireEvent.click(screen.getByRole('button',{name:'补充资料'}));fireEvent.change(screen.getByLabelText('补充需求内容'),{target:{value:'新增管理员解锁要求'}});fireEvent.click(screen.getByLabelText('新增规则.txt'));fireEvent.click(screen.getByRole('button',{name:'应用补充，重新理解'}));await waitFor(()=>assert.deepEqual(submitted,{source_ids:['s-new'],content:'新增管理员解锁要求'}));assert.equal(screen.getByRole('button',{name:'确认场景，继续生成用例',hidden:true}).hasAttribute('disabled'),true);assert.equal(screen.getByRole('button',{name:'停止',hidden:true}).hasAttribute('disabled'),true);resolveRequest(response({detail:'任务正被另一项修改占用'},409));await screen.findByRole('alert');assert.equal((screen.getByLabelText('补充需求内容') as HTMLTextAreaElement).value,'新增管理员解锁要求');assert.equal((screen.getByLabelText('新增规则.txt') as HTMLInputElement).checked,true);assert.equal(changed,0);fireEvent.click(screen.getByRole('button',{name:'应用补充，重新理解'}));resolveRequest(response({run:{id:'new'},previous_run_id:'r-supplement',message:'已恢复'}));await waitFor(()=>assert.equal(changed,1));assert.equal(Boolean(screen.queryByRole('dialog')),false);}finally{cleanup();globalThis.fetch=original;}
});

test('sample pinning uses selected cases and target Profile version',async()=>{
 const {PinSamples}=await import('../src/ProjectSamples');const original=globalThis.fetch;let submitted:any;let changed=0;
 globalThis.fetch=(async(url:any,init:any={})=>{const path=String(url);if(path.endsWith('/export-options'))return response({profiles:[{id:'p1',name:'默认',version:3,config:{}},{id:'p2',name:'演示',version:7,config:{}}]});assert.equal(path,'/api/artifacts/cases/pin-samples');submitted=JSON.parse(init.body);return response({id:'p2',version:8});}) as typeof fetch;
 try{render(<PinSamples artifact={{id:'cases',type:'cases',title:'已有用例',revision:2,items:[{id:'C1',title:'有效登录'},{id:'C2',title:'账号锁定'}]}} selectedIds={['C2']} onChanged={()=>{changed++;}}/>);fireEvent.click(screen.getByRole('button',{name:'保存为项目样例'}));await screen.findByRole('option',{name:'演示 · v7'});assert.equal((screen.getByLabelText('样例 C1') as HTMLInputElement).checked,false);assert.equal((screen.getByLabelText('样例 C2') as HTMLInputElement).checked,true);fireEvent.change(screen.getByLabelText('样例目标 Profile'),{target:{value:'p2'}});fireEvent.click(screen.getByRole('button',{name:'保存样例'}));await waitFor(()=>assert.equal(changed,1));assert.deepEqual(submitted,{profile_id:'p2',expected_version:7,selected_ids:['C2']});assert.ok(screen.getByRole('status'));}finally{cleanup();globalThis.fetch=original;}
});

test('Profile sample removal preserves other settings and remains a draft until saved',async()=>{
 const {ProfileEditor}=await import('../src/ProfileEditor');let next:any;
 const config={sample_cases:[{title:'有效登录',steps:[{action:'输入密码',expected:'登录成功'}]},{title:'账号锁定'}],additional_rules:'保留此规则',excel_columns:[]};
 function Editor(){const [value,setValue]=React.useState(JSON.stringify(config));return <ProfileEditor value={value} onChange={value=>{next=JSON.parse(value);setValue(value);}}/>;}
 try{render(<Editor/>);fireEvent.click(screen.getByRole('button',{name:'移除样例 1',hidden:true}));assert.deepEqual(next.sample_cases,[{title:'账号锁定'}]);assert.equal(next.additional_rules,'保留此规则');fireEvent.click(screen.getByRole('button',{name:'移除全部样例'}));assert.deepEqual(next.sample_cases,[]);assert.equal(next.additional_rules,'保留此规则');}finally{cleanup();}
});
