// Retained current contracts extracted from workspace-smoke.test.tsx; archived legacy controls remain in legacy-tests/frontend.
import {JSDOM} from 'jsdom';
import {test} from 'node:test';
import assert from 'node:assert/strict';
const dom=new JSDOM('<!doctype html><html><body></body></html>',{url:'http://localhost:8000/'});
for(const key of ['window','document','HTMLElement','HTMLDialogElement','MutationObserver','Node','Event','MouseEvent','File','FormData'])Object.defineProperty(globalThis,key,{value:(dom.window as any)[key],configurable:true,writable:true});
Object.defineProperty(globalThis,'navigator',{value:dom.window.navigator,configurable:true});
(globalThis as any).IS_REACT_ACT_ENVIRONMENT=true;
(globalThis as any).EventSource=class{addEventListener(){}close(){}};
dom.window.HTMLElement.prototype.scrollIntoView=function(){};
dom.window.HTMLDialogElement.prototype.showModal=function(){this.open=true;};
dom.window.HTMLDialogElement.prototype.close=function(){this.open=false;};
const React=await import('react');
const {render,fireEvent,screen,waitFor,cleanup,within}=await import('@testing-library/react');
const {App}=await import('../src/App');
test('failed task exposes the small log without opening run details',async()=>{
 const {RunCard}=await import('../src/RunCard');
 try{
  render(<RunCard run={{id:'failed-run',status:'failed',intent:'generate_case',mode:'auto',stage:'failed',updated_at:'2026-09-09',artifact_ids:[],experience:'reliable',graph_version:7,error:'JSON 语法错误'}} onChanged={()=>{}} onTarget={()=>{}}/>);
  const link=screen.getByRole('link',{name:/下载失败步骤日志/});
  assert.equal(link.getAttribute('href'),'/api/runs/failed-run/failed-step');
  assert.equal(link.hasAttribute('download'),true);
  assert.equal(link.closest('details'),null);
 }finally{cleanup();}
});


test('template column definition can be edited without losing its header',async()=>{
 const {ProfileEditor}=await import('../src/ProfileEditor');let saved:any;
 try{
  render(<ProfileEditor value={JSON.stringify({excel_columns:[{field:'test_data',header:'测试数据',definition:'原定义'}]})} onChange={v=>{saved=JSON.parse(v);}}/>);
  fireEvent.change(screen.getByLabelText('第 1 列定义'),{target:{value:'字段=值'}});
  assert.deepEqual(saved.excel_columns,[{field:'test_data',header:'测试数据',definition:'字段=值'}]);
 }finally{cleanup();}
});