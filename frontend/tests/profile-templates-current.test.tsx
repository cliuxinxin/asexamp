// Retained current contracts extracted from guided-v258.test.tsx; archived legacy controls remain in legacy-tests/frontend.
import {JSDOM} from 'jsdom';
import {test} from 'node:test';
import assert from 'node:assert/strict';

const dom=new JSDOM('<!doctype html><html><body></body></html>',{url:'http://localhost:8000/'});
for(const key of ['window','document','HTMLElement','HTMLDialogElement','MutationObserver','Node','Event','MouseEvent'])Object.defineProperty(globalThis,key,{value:(dom.window as any)[key],configurable:true,writable:true});
Object.defineProperty(globalThis,'navigator',{value:dom.window.navigator,configurable:true});
(globalThis as any).IS_REACT_ACT_ENVIRONMENT=true;
(globalThis as any).EventSource=class{addEventListener(){}close(){}};
dom.window.HTMLElement.prototype.scrollIntoView=function(){};
dom.window.HTMLDialogElement.prototype.showModal=function(){this.open=true;};
dom.window.HTMLDialogElement.prototype.close=function(){this.open=false;};

const React=await import('react');
const {render,fireEvent,screen,waitFor,cleanup}=await import('@testing-library/react');
const {nextTaskDraft}=await import('../src/taskPrompts');

test('task prompts fill defaults and append once while retaining the custom draft',()=>{
 assert.match(nextTaskDraft('', 'auto', 'learn_template'),/场景模板.*用例模板/);
 const previous=nextTaskDraft('', 'auto', 'generate_case');
 assert.match(nextTaskDraft(previous,'generate_case','learn_template'),/字段定义/);
 const custom=nextTaskDraft('我的自定义要求','generate_case','learn_template');
 assert.ok(custom.startsWith('我的自定义要求\n\n'));
 assert.match(custom,/场景模板.*用例模板/);
 assert.equal(nextTaskDraft(custom,'learn_template','learn_template'),custom);
});

test('scenario card exposes deterministic Excel export without case completion UI',async()=>{
 const {ArtifactCard}=await import('../src/ArtifactCard');const originalFetch=globalThis.fetch;
 const result={id:'s1',type:'scenarios',title:'测试场景',revision:1,items:[{id:'S-1',title:'登录',description:'验证登录',priority:'P1',refs:['src#P1'],requirement_ids:['R-1']}]};
 globalThis.fetch=(async(url:any)=>{
  const path=String(url);let value:any;
  if(path==='/api/artifacts/s1')value=result;
  else if(path.endsWith('/export-options'))value={kind:'scenarios',snapshot:{scenario_sheet_name:'Test Scenarios'},profiles:[]};
  else throw new Error(path);
  return new Response(JSON.stringify(value),{status:200,headers:{'Content-Type':'application/json'}});
 }) as typeof fetch;
 try{
  render(<ArtifactCard id="s1" onTarget={()=>{}} onChanged={()=>{}}/>);
  fireEvent.click(await screen.findByRole('button',{name:'导出 Excel'}));
  await screen.findByRole('button',{name:'下载 XLSX'});
  assert.equal(screen.queryByText('补全模板字段'),null);
  assert.equal(screen.queryByLabelText('Excel 布局'),null);
 }finally{cleanup();globalThis.fetch=originalFetch;}
});

test('scenario Profile columns, sheet and filename can change independently of case settings',async()=>{
 const {ProfileEditor}=await import('../src/ProfileEditor');
 const initial={excel_columns:[{field:'title',header:'用例标题'}],sheet_name:'Cases',filename_pattern:'cases.xlsx',scenario_excel_columns:[{field:'title',header:'场景标题'},{field:'refs',header:'证据'}],scenario_sheet_name:'Scenarios',scenario_filename_pattern:'scenarios.xlsx'};let saved:any=initial;
 function Editor(){const [value,setValue]=React.useState(JSON.stringify(initial));return <ProfileEditor value={value} onChange={next=>{saved=JSON.parse(next);setValue(next);}}/>;}
 try{
  render(<Editor/>);
  assert.equal(screen.queryByLabelText('场景第 1 列填写方式'),null);assert.ok(screen.getByLabelText('第 1 列填写方式'));
  fireEvent.change(screen.getByLabelText('场景 Sheet 名称'),{target:{value:'登录场景'}});
  fireEvent.change(screen.getByLabelText('场景文件名格式'),{target:{value:'{project}_场景.xlsx'}});
  fireEvent.change(screen.getByLabelText('场景第 1 列定义'),{target:{value:'核心业务路径'}});
  fireEvent.click(screen.getByRole('button',{name:'上移场景第 2 列'}));
  assert.deepEqual(saved.excel_columns,initial.excel_columns);assert.equal(saved.sheet_name,'Cases');assert.equal(saved.filename_pattern,'cases.xlsx');
  assert.equal(saved.scenario_sheet_name,'登录场景');assert.equal(saved.scenario_filename_pattern,'{project}_场景.xlsx');assert.equal(saved.scenario_excel_columns[0].field,'refs');assert.equal(saved.scenario_excel_columns[1].definition,'核心业务路径');
  const scenarios=JSON.stringify(saved.scenario_excel_columns);fireEvent.change(screen.getByLabelText('第 1 列标题'),{target:{value:'新用例标题'}});assert.equal(JSON.stringify(saved.scenario_excel_columns),scenarios);
 }finally{cleanup();}
});

const proposalProfiles=[
 {id:'target-a',project_id:'proposal-project',name:'Profile A',version:2,config:{language:'A',scope:'A scope',additional_rules:'A focus',template_rules:'A writing',case_level:'quick',case_types:['Business'],excel_columns:[{field:'title',header:'A case'}],excel_layout:'step',sheet_name:'A Cases',filename_pattern:'A-cases.xlsx',scenario_excel_columns:[{field:'title',header:'A scenario'}],scenario_sheet_name:'A Scenarios',scenario_filename_pattern:'A-scenarios.xlsx'}},
 {id:'target-b',project_id:'proposal-project',name:'Profile B',version:7,config:{language:'B',scope:'B scope',additional_rules:'B focus',template_rules:'B writing',case_level:'standard',case_types:['Negative'],excel_columns:[{field:'description',header:'B case'}],excel_layout:'case',sheet_name:'B Cases',filename_pattern:'B-cases.xlsx',scenario_excel_columns:[{field:'description',header:'B scenario'}],scenario_sheet_name:'B Scenarios',scenario_filename_pattern:'B-scenarios.xlsx'}},
];
function scopedProposal(kind:'scenarios'|'cases'){
 const patch=kind==='scenarios'?{scenario_excel_columns:[{field:'title',header:'Learned scenario'}],scenario_sheet_name:'Learned Scenarios',scenario_filename_pattern:'learned-scenarios.xlsx'}:{excel_columns:[{field:'title',header:'Learned case'}],excel_layout:'case',sheet_name:'Learned Cases',filename_pattern:'learned-cases.xlsx',template_rules:'Learned writing',case_level:'deep',case_types:['Business','Boundary'],additional_rules:'Learned focus'};
 return {proposal:{template_kinds:[kind],config:{...proposalProfiles[0].config,...patch}},patch};
}
function proposalRequests(){
 const original=globalThis.fetch;const calls:{path:string;body:any;method:string}[]=[];
 globalThis.fetch=(async(url:any,init:any={})=>{
  const path=String(url);let value:any;
  if(path==='/api/settings')value={provider:'openai',model:'mock'};
  else{const body=JSON.parse(init.body);calls.push({path,body,method:init.method});value={id:'new-profile',project_id:'proposal-project',version:1,...body};}
  return new Response(JSON.stringify(value),{status:200,headers:{'Content-Type':'application/json'}});
 }) as typeof fetch;
 return {calls,restore:()=>{globalThis.fetch=original;}};
}

for(const kind of ['scenarios','cases'] as const){
 test(`${kind} scoped proposal starts on target B and saves only its learned template kind`,async()=>{
  const {SettingsDialog}=await import('../src/SettingsDialog');const requests=proposalRequests();const {proposal,patch}=scopedProposal(kind);
  try{
   render(<SettingsDialog projectId="proposal-project" profiles={proposalProfiles} activeProfileId="target-b" proposal={proposal} onClose={()=>{}} onSaved={()=>{}}/>);
   assert.equal((screen.getByLabelText('选择 Profile') as HTMLSelectElement).value,'target-b');
   const expected={...proposalProfiles[1].config,...patch};
   assert.deepEqual(JSON.parse((screen.getByLabelText('Profile 配置 JSON') as HTMLTextAreaElement).value),expected);
   fireEvent.click(screen.getByRole('button',{name:'更新当前 Profile'}));await waitFor(()=>assert.equal(requests.calls.length,1));
   assert.deepEqual(requests.calls[0],{path:'/api/profiles/target-b',method:'PUT',body:{name:'Profile B',config:expected,expected_version:7}});
  }finally{cleanup();requests.restore();}
 });
 test(`${kind} scoped proposal rebases from A to B while retaining learned-field manual edits`,async()=>{
  const {SettingsDialog}=await import('../src/SettingsDialog');const requests=proposalRequests();const {proposal,patch}=scopedProposal(kind);
  try{
   render(<SettingsDialog projectId="proposal-project" profiles={proposalProfiles} activeProfileId="target-a" proposal={proposal} onClose={()=>{}} onSaved={()=>{}}/>);
   const sheetKey=kind==='scenarios'?'scenario_sheet_name':'sheet_name';
   fireEvent.change(screen.getByLabelText(kind==='scenarios'?'场景 Sheet 名称':'Sheet 名称',{exact:true}),{target:{value:'Manual learned sheet'}});
   fireEvent.change(screen.getByLabelText('选择 Profile'),{target:{value:'target-b'}});
   const expected={...proposalProfiles[1].config,...patch,[sheetKey]:'Manual learned sheet'};
   assert.deepEqual(JSON.parse((screen.getByLabelText('Profile 配置 JSON') as HTMLTextAreaElement).value),expected);
   fireEvent.click(screen.getByRole('button',{name:'更新当前 Profile'}));await waitFor(()=>assert.equal(requests.calls.length,1));
   assert.equal(requests.calls[0].path,'/api/profiles/target-b');assert.equal(requests.calls[0].body.expected_version,7);assert.deepEqual(requests.calls[0].body.config,expected);
  }finally{cleanup();requests.restore();}
 });
}

test('scenario scoped proposal creates a new Profile from the selected destination defaults',async()=>{
 const {SettingsDialog}=await import('../src/SettingsDialog');const requests=proposalRequests();const {proposal,patch}=scopedProposal('scenarios');
 try{
  render(<SettingsDialog projectId="proposal-project" profiles={proposalProfiles} activeProfileId="target-a" proposal={proposal} onClose={()=>{}} onSaved={()=>{}}/>);
  fireEvent.change(screen.getByLabelText('选择 Profile'),{target:{value:'target-b'}});fireEvent.change(screen.getByLabelText('Profile 名称'),{target:{value:'Learned copy'}});
  fireEvent.click(screen.getByRole('button',{name:'保存为新 Profile'}));await waitFor(()=>assert.equal(requests.calls.length,1));
  assert.deepEqual(requests.calls[0],{path:'/api/projects/proposal-project/profiles',method:'POST',body:{name:'Learned copy',config:{...proposalProfiles[1].config,...patch}}});
 }finally{cleanup();requests.restore();}
});

test('case scoped proposal use-once keeps target scenario settings and edited case settings',async()=>{
 const {SettingsDialog}=await import('../src/SettingsDialog');const requests=proposalRequests();const {proposal,patch}=scopedProposal('cases');let used:any;let closed=false;
 try{
  render(<SettingsDialog projectId="proposal-project" profiles={proposalProfiles} activeProfileId="target-a" proposal={proposal} onClose={()=>{closed=true;}} onSaved={()=>{}} onUseOnce={value=>{used=value;}}/>);
  fireEvent.change(screen.getByLabelText('Sheet 名称',{exact:true}),{target:{value:'Manual Cases'}});fireEvent.change(screen.getByLabelText('选择 Profile'),{target:{value:'target-b'}});
  fireEvent.click(screen.getByRole('button',{name:'仅下一次运行使用'}));
  assert.deepEqual(used,{...proposalProfiles[1].config,...patch,sheet_name:'Manual Cases'});assert.equal(closed,true);assert.equal(requests.calls.length,0);
 }finally{cleanup();requests.restore();}
});
