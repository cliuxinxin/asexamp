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
const {ArtifactCard}=await import('../src/ArtifactCard');
const {ConversationParts}=await import('../src/ConversationParts');
const originalFetch=globalThis.fetch;
afterEach(()=>{cleanup();globalThis.fetch=originalFetch;});
const caseItem={id:'C1',title:'登录',type:'Business',priority:'P1',preconditions:'已注册',steps:[{action:'登录',expected:'成功'}],refs:[]};
const cases={id:'cases',chat_id:'chat',project_id:'project',revision:2,type:'cases',title:'测试用例',items:[caseItem,{...caseItem,id:'C2'}],report:{}};
function fixture(artifact:any,missing:any[]=[],failure=false){
 const requests:{path:string;method:string}[]=[];
 globalThis.fetch=(async(input:any,init:any={})=>{
  const path=String(input);requests.push({path,method:init.method??'GET'});
  if(path.includes('/export?'))return failure?new Response(JSON.stringify({detail:'模板字段待补充'}),{status:422}):new Response('xlsx',{status:200,headers:{'Content-Disposition':"attachment; filename*=UTF-8''tcg.xlsx"}});
  const value=path.includes('/export-options')?{revision:artifact.revision,snapshot:{excel_layout:'case'},snapshot_check:{missing},profiles:[]}:path.includes('/workspace')?{lineage_rows:[]}:artifact;
  return new Response(JSON.stringify(value),{status:200});
 }) as typeof fetch;
 return requests;
}

test('chat-first current viewer exports selected rows directly with exact revision and layout',async()=>{
 const requests=fixture(cases);let downloaded='';const click=dom.window.HTMLAnchorElement.prototype.click;
 dom.window.HTMLAnchorElement.prototype.click=function(){downloaded=this.download;};
 try{
  render(<ArtifactCard id={cases.id} snapshot={cases} chatOnly simplified onTarget={()=>{}} onChanged={()=>assert.fail('export must not mutate')}/>);
  fireEvent.click(screen.getByRole('checkbox',{name:'选择 C2',exact:true}));
  fireEvent.click(screen.getByRole('button',{name:'导出 Excel'}));
  await waitFor(()=>assert.equal((screen.getByRole('button',{name:'下载 XLSX'}) as HTMLButtonElement).disabled,false));
  fireEvent.change(screen.getByLabelText('Excel 布局'),{target:{value:'step'}});
  fireEvent.click(screen.getByRole('button',{name:'下载 XLSX'}));
  await waitFor(()=>assert.equal(downloaded,'tcg.xlsx'));
  const exported=requests.find(item=>item.path.includes('/export?'))!;const url=new URL(exported.path,'http://localhost');
  assert.equal(url.searchParams.get('ids'),'C2');assert.equal(url.searchParams.get('revision'),'2');assert.equal(url.searchParams.get('layout'),'step');
  assert.ok(requests.some(item=>item.path.endsWith('/export-options?revision=2')));
  assert.ok(requests.every(item=>item.method==='GET'));assert.equal(screen.queryByRole('dialog'),null);
 }finally{dom.window.HTMLAnchorElement.prototype.click=click;}
});

test('historical chat viewer offers read-only export and no hidden field mutation controls',async()=>{
 const historical={...cases,revision:1};const requests=fixture(historical,[{id:'C1',field:'purpose',header:'验证目的'}]);
 render(<ArtifactCard id={historical.id} snapshot={historical} readOnly chatOnly simplified onTarget={()=>{}} onChanged={()=>{}}/>);
 fireEvent.click(screen.getByRole('button',{name:'导出 Excel'}));
 await screen.findByText(/C1 · 验证目的/);
 assert.ok(requests.some(item=>item.path.endsWith('/export-options?revision=1')));
 assert.equal(screen.queryByRole('button',{name:'补全模板字段'}),null);assert.equal(screen.queryByRole('button',{name:'填写缺失字段'}),null);
 assert.equal((screen.getByRole('button',{name:'下载 XLSX'}) as HTMLButtonElement).disabled,true);
 assert.ok(screen.getByText(/打开最新版本.*补全/));
});

test('scenario historical export remains available and errors stay in the dialog',async()=>{
 const scenarios={...cases,id:'scenarios',type:'scenarios',items:[{id:'S1',title:'场景',refs:[]}]};
 fixture(scenarios,[],true);
 render(<ArtifactCard id={scenarios.id} snapshot={scenarios} readOnly chatOnly simplified onTarget={()=>{}} onChanged={()=>{}}/>);
 fireEvent.click(screen.getByRole('button',{name:'导出 Excel'}));
 await waitFor(()=>assert.equal((screen.getByRole('button',{name:'下载 XLSX'}) as HTMLButtonElement).disabled,false));
 assert.equal(screen.queryByLabelText('Excel 布局'),null);
 fireEvent.click(screen.getByRole('button',{name:'下载 XLSX'}));
 await screen.findAllByText('模板字段待补充');
 assert.ok(screen.getByRole('dialog'));assert.ok(screen.getByRole('button',{name:'下载 XLSX'}));
});

test('partial case-details export includes only displayed rows and ignores unrelated field gaps',async()=>{
 const requests=fixture(cases,[{id:'C2',field:'purpose',header:'验证目的'}]);let opened:any;let downloaded='';
 const click=dom.window.HTMLAnchorElement.prototype.click;
 dom.window.HTMLAnchorElement.prototype.click=function(){downloaded=this.download;};
 try{
  render(<ConversationParts chatOnly compact response={{id:'turn',status:'succeeded',message:'显示第一条',parts:[{type:'case_details',artifact_id:'cases',revision:2,items:[caseItem]}]}} onTarget={()=>{}} onOpen={value=>{opened=value;}} onChanged={()=>{}}/>);
  fireEvent.click(screen.getByRole('button',{name:/打开成果/}));
  assert.deepEqual(opened.view_item_ids,['C1']);
  cleanup();
  render(<ArtifactCard id={opened.id} snapshot={opened} readOnly chatOnly simplified onTarget={()=>{}} onChanged={()=>assert.fail('read-only export')}/>);
  fireEvent.click(screen.getByRole('button',{name:'导出 Excel'}));
  await waitFor(()=>assert.equal((screen.getByRole('button',{name:'下载 XLSX'}) as HTMLButtonElement).disabled,false));
  assert.equal(screen.queryByText(/C2 · 验证目的/),null);
  fireEvent.click(screen.getByRole('button',{name:'下载 XLSX'}));
  await waitFor(()=>assert.equal(downloaded,'tcg.xlsx'));
  const url=new URL(requests.find(item=>item.path.includes('/export?'))!.path,'http://localhost');
  assert.equal(url.searchParams.get('ids'),'C1');assert.equal(url.searchParams.get('revision'),'2');
 }finally{dom.window.HTMLAnchorElement.prototype.click=click;}
});
