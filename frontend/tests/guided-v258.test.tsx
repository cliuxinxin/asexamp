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

test('task prompt fills an empty/default draft but preserves user text',()=>{
 assert.match(nextTaskDraft('', 'auto', 'learn_template'),/自动判断是场景模板/);
 const previous=nextTaskDraft('', 'auto', 'generate_case');
 assert.match(nextTaskDraft(previous,'generate_case','learn_template'),/列顺序/);
 assert.equal(nextTaskDraft('我的自定义要求','generate_case','learn_template'),'我的自定义要求');
});

test('clarification suggestions fill drafts and never auto submit',async()=>{
 const {RunCard}=await import('../src/RunCard');let requested=false;const originalFetch=globalThis.fetch;
 globalThis.fetch=(async()=>{requested=true;return new Response('{}',{status:200,headers:{'Content-Type':'application/json'}});}) as typeof fetch;
 try{
  render(<RunCard run={{id:'r1',status:'waiting',intent:'generate_case',mode:'hitp',stage:'clarification',updated_at:'2026-09-10',artifact_ids:[],experience:'reliable',graph_version:7,interrupt:{type:'clarification',questions:['是否锁定账号？'],question_suggestions:[{question:'是否锁定账号？',answer:'连续失败 5 次后锁定。',basis:'需求明确给出阈值。',refs:['src#P1'],confidence:'supported'}]}}} onChanged={()=>{}} onTarget={()=>{}}/>);
  fireEvent.click(screen.getByRole('button',{name:'采用此答案'}));
  assert.match((screen.getByLabelText('回答澄清问题') as HTMLTextAreaElement).value,/连续失败 5 次/);
  assert.equal(requested,false);
  fireEvent.click(screen.getByRole('button',{name:'提交并继续'}));
  await waitFor(()=>assert.equal(requested,true));
 }finally{cleanup();globalThis.fetch=originalFetch;}
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

test('task selection fills the composer and preserves edited drafts across every task selector',async()=>{
 const {App}=await import('../src/App');const originalFetch=globalThis.fetch;
 globalThis.fetch=(async(url:any)=>{
  const path=String(url);let value:any;
  if(path==='/api/projects')value=[{id:'p-guided',name:'Guided project'}];
  else if(path==='/api/settings')value={provider:'openai',model:'mock'};
  else if(path.endsWith('/profiles'))value=[{id:'profile',name:'Default',config:{}}];
  else if(path.endsWith('/chats'))value=[{id:'c-guided',project_id:'p-guided',title:'Guided'}];
  else if(path==='/api/chats/c-guided')value={chat:{id:'c-guided'},sources:[],runs:[],messages:[]};
  else throw new Error(path);
  return new Response(JSON.stringify(value),{status:200,headers:{'Content-Type':'application/json'}});
 }) as typeof fetch;
 try{
  render(<App/>);await waitFor(()=>assert.equal(screen.getByRole('button',{name:'新建生成会话'}).hasAttribute('disabled'),false));
  const composer=screen.getByLabelText('聊天输入') as HTMLTextAreaElement;
  fireEvent.click(screen.getByRole('button',{name:'学习模板',exact:true}));assert.match(composer.value,/自动判断是场景模板.*列顺序/);
  assert.equal((screen.getByLabelText('附件用途') as HTMLSelectElement).value,'example');
  assert.equal(screen.getByRole('button',{name:'发送消息',exact:true}).hasAttribute('disabled'),false);
  fireEvent.click(screen.getByRole('button',{name:'生成测试用例',exact:true}));assert.match(composer.value,/可追溯的测试用例/);
  fireEvent.change(composer,{target:{value:'  自定义要求，保留这些空格  '}});
  fireEvent.click(screen.getByRole('button',{name:'分析需求',exact:true}));assert.equal(composer.value,'  自定义要求，保留这些空格  ');
  fireEvent.click(screen.getByRole('button',{name:/本次方案/}));fireEvent.click(screen.getByText('指定任务与资料用途'));
  fireEvent.change(screen.getByLabelText('任务目标'),{target:{value:'learn_template'}});assert.equal(composer.value,'  自定义要求，保留这些空格  ');
  fireEvent.change(composer,{target:{value:''}});fireEvent.change(screen.getByLabelText('任务目标'),{target:{value:'generate_scenario'}});assert.match(composer.value,/生成测试场景/);
 }finally{cleanup();globalThis.fetch=originalFetch;}
});

test('adopt all retains an edited answer and unrelated manual notes without duplicate answers',async()=>{
 const {RunCard}=await import('../src/RunCard');const originalFetch=globalThis.fetch;let submitted:any;
 globalThis.fetch=(async(url:any,init:any)=>{assert.equal(String(url),'/api/runs/r-guided/resume');submitted=JSON.parse(init.body);return new Response('{}',{status:200,headers:{'Content-Type':'application/json'}});}) as typeof fetch;
 try{
  render(<RunCard run={{id:'r-guided',status:'waiting',intent:'generate_case',mode:'hitp',stage:'clarification',updated_at:'2026-09-10',artifact_ids:[],interrupt:{type:'clarification',questions:['失败几次锁定？','是否允许人工解锁？'],question_suggestions:[{question:'失败几次锁定？',answer:'5 次。',basis:'需求第 1 段。',refs:['src#P1'],confidence:'supported'},{question:'是否允许人工解锁？',answer:'建议允许管理员解锁。',basis:'建议待业务确认。',refs:[],confidence:'assumption'}]}}} onChanged={()=>{}} onTarget={()=>{}}/>);
  assert.ok(screen.getByText('需求中已有答案'));assert.ok(screen.getByText('建议假设'));assert.ok(screen.getByText('引用：src#P1'));
  const draft=screen.getByLabelText('回答澄清问题') as HTMLTextAreaElement;
  fireEvent.change(draft,{target:{value:'  我的补充备注  '}});fireEvent.click(screen.getAllByRole('button',{name:'采用此答案'})[0]);
  fireEvent.change(draft,{target:{value:draft.value.replace('5 次。','3 次，以人工确认为准。')}});
  const edited=draft.value;fireEvent.click(screen.getByRole('button',{name:'采用全部建议'}));
  assert.ok(draft.value.startsWith(edited));assert.match(draft.value,/建议允许管理员解锁/);assert.equal(draft.value.includes('5 次。'),false);
  assert.equal(draft.value.split('问题：失败几次锁定？').length,2);const once=draft.value;
  fireEvent.click(screen.getByRole('button',{name:'采用全部建议'}));assert.equal(draft.value,once);assert.equal(submitted,undefined);
  fireEvent.click(screen.getByRole('button',{name:'提交并继续'}));await waitFor(()=>assert.equal(submitted.answer,once));
 }finally{cleanup();globalThis.fetch=originalFetch;}
});

test('legacy clarification still accepts manual answers when suggestions are absent',async()=>{
 const {RunCard}=await import('../src/RunCard');
 try{
  render(<RunCard run={{id:'r-old',status:'waiting',intent:'generate_case',mode:'hitp',stage:'clarification',updated_at:'2026-09-10',artifact_ids:[],interrupt:{type:'clarification',questions:['请确认范围。']}}} onChanged={()=>{}} onTarget={()=>{}}/>);
  assert.ok(screen.getByText('请确认范围。'));assert.equal(screen.queryByRole('button',{name:'采用全部建议'}),null);
  fireEvent.change(screen.getByLabelText('回答澄清问题'),{target:{value:'仅 Web 登录。'}});
  assert.equal(screen.getByRole('button',{name:'提交并继续'}).hasAttribute('disabled'),false);
 }finally{cleanup();}
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

test('selected scenario export sends its profile and IDs without case layout or completion requests',async()=>{
 const {ArtifactCard}=await import('../src/ArtifactCard');const originalFetch=globalThis.fetch;
 const originalCreate=URL.createObjectURL,originalRevoke=URL.revokeObjectURL,originalClick=dom.window.HTMLAnchorElement.prototype.click;
 let downloaded:URL|undefined;let filename='';
 URL.createObjectURL=()=> 'blob:scenario-export';URL.revokeObjectURL=()=>{};dom.window.HTMLAnchorElement.prototype.click=function(){filename=this.download;};
 globalThis.fetch=(async(url:any)=>{
  const path=String(url);let value:any;
  if(path==='/api/artifacts/s-export')value={id:'s-export',type:'scenarios',title:'场景',revision:1,items:[{id:'S-1',title:'登录'},{id:'S-2',title:'锁定'}]};
  else if(path.endsWith('/export-options'))value={kind:'scenarios',snapshot:{scenario_sheet_name:'Test Scenarios'},profiles:[{id:'scenario-profile',name:'场景定制',config:{scenario_sheet_name:'场景'}}]};
  else if(path.startsWith('/api/artifacts/s-export/export?')){downloaded=new URL(path,'http://localhost');return new Response('xlsx',{status:200,headers:{'Content-Disposition':"attachment; filename*=UTF-8''scenarios.xlsx"}});}
  else throw new Error(path);
  return new Response(JSON.stringify(value),{status:200,headers:{'Content-Type':'application/json'}});
 }) as typeof fetch;
 try{
  render(<ArtifactCard id="s-export" onTarget={()=>{}} onChanged={()=>{}}/>);
  fireEvent.click(await screen.findByLabelText('选择 S-2'));fireEvent.click(screen.getByRole('button',{name:'导出 Excel'}));
  await screen.findByRole('option',{name:'场景定制 · 当前版本'});assert.equal((screen.getByLabelText('导出范围') as HTMLSelectElement).value,'selected');
  fireEvent.change(screen.getByLabelText('导出格式 Profile'),{target:{value:'scenario-profile'}});
  assert.equal(screen.getByRole('button',{name:'下载 XLSX'}).hasAttribute('disabled'),false);
  fireEvent.click(screen.getByRole('button',{name:'下载 XLSX'}));await waitFor(()=>assert.equal(filename,'scenarios.xlsx'));
  assert.equal(downloaded?.searchParams.get('ids'),'S-2');assert.equal(downloaded?.searchParams.get('profile_id'),'scenario-profile');assert.equal(downloaded?.searchParams.has('layout'),false);
 }finally{cleanup();globalThis.fetch=originalFetch;URL.createObjectURL=originalCreate;URL.revokeObjectURL=originalRevoke;dom.window.HTMLAnchorElement.prototype.click=originalClick;}
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

test('App carries learned template metadata into the proposal editor for the active target Profile',async()=>{
 const {App}=await import('../src/App');const originalFetch=globalThis.fetch;const {proposal,patch}=scopedProposal('scenarios');
 globalThis.fetch=(async(url:any)=>{
  const path=String(url);let value:any;
  if(path==='/api/projects')value=[{id:'proposal-project',name:'Proposal project'}];
  else if(path==='/api/settings')value={provider:'openai',model:'mock'};
  else if(path.endsWith('/profiles'))value=proposalProfiles;
  else if(path.endsWith('/chats'))value=[{id:'proposal-chat',project_id:'proposal-project',title:'Learned template'}];
  else if(path==='/api/chats/proposal-chat')value={chat:{id:'proposal-chat'},sources:[],runs:[],messages:[{id:'proposal-message',role:'assistant',content:'模板学习完成',metadata:{proposal}}]};
  else throw new Error(path);
  return new Response(JSON.stringify(value),{status:200,headers:{'Content-Type':'application/json'}});
 }) as typeof fetch;
 try{
  render(<App/>);await screen.findByText('已识别模板：场景模板');
  fireEvent.change(screen.getByLabelText('运行 Profile'),{target:{value:'target-b'}});
  fireEvent.click(screen.getByRole('button',{name:'查看并应用 Profile 建议'}));
  assert.deepEqual(JSON.parse((screen.getByLabelText('Profile 配置 JSON') as HTMLTextAreaElement).value),{...proposalProfiles[1].config,...patch});
 }finally{cleanup();globalThis.fetch=originalFetch;}
});
