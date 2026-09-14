// Current Profile column editing contract.
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
test('template column definition can be edited without losing its header',async()=>{
 const {ProfileEditor}=await import('../src/ProfileEditor');let saved:any;
 try{
  render(<ProfileEditor value={JSON.stringify({excel_columns:[{field:'test_data',header:'测试数据',definition:'原定义'}]})} onChange={v=>{saved=JSON.parse(v);}}/>);
  fireEvent.change(screen.getByLabelText('第 1 列定义'),{target:{value:'字段=值'}});
  assert.deepEqual(saved.excel_columns,[{field:'test_data',header:'测试数据',definition:'字段=值'}]);
 }finally{cleanup();}
});
