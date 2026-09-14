// Retained current contracts extracted from project-context-v2512.test.tsx.
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

test('Profile sample removal preserves other settings and remains a draft until saved',async()=>{
 const {ProfileEditor}=await import('../src/ProfileEditor');let next:any;
 const config={sample_cases:[{title:'有效登录',steps:[{action:'输入密码',expected:'登录成功'}]},{title:'账号锁定'}],additional_rules:'保留此规则',excel_columns:[]};
 function Editor(){const [value,setValue]=React.useState(JSON.stringify(config));return <ProfileEditor value={value} onChange={value=>{next=JSON.parse(value);setValue(value);}}/>;}
 try{render(<Editor/>);fireEvent.click(screen.getByRole('button',{name:'移除样例 1',hidden:true}));assert.deepEqual(next.sample_cases,[{title:'账号锁定'}]);assert.equal(next.additional_rules,'保留此规则');fireEvent.click(screen.getByRole('button',{name:'移除全部样例'}));assert.deepEqual(next.sample_cases,[]);assert.equal(next.additional_rules,'保留此规则');}finally{cleanup();}
});