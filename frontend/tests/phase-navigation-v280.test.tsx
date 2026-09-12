import {JSDOM} from 'jsdom';
import {afterEach,test} from 'node:test';
import assert from 'node:assert/strict';
const dom=new JSDOM('<!doctype html><html><body></body></html>',{url:'http://localhost/'});
for(const key of ['window','document','HTMLElement','MutationObserver','Node','Event','MouseEvent'])Object.defineProperty(globalThis,key,{value:(dom.window as any)[key],configurable:true,writable:true});
Object.defineProperty(globalThis,'navigator',{value:dom.window.navigator,configurable:true});
(globalThis as any).IS_REACT_ACT_ENVIRONMENT=true;
const React=await import('react');
const {render,screen,fireEvent,cleanup,within}=await import('@testing-library/react');
const {ArtifactWorkspace}=await import('../src/ArtifactWorkspace');
const originalFetch=globalThis.fetch;
afterEach(()=>{cleanup();globalThis.fetch=originalFetch;});
test('phase selection opens the exact related artifact while the upstream update remains the next action',async()=>{
 const selected:string[]=[];
 const artifact={id:'scenarios',type:'scenarios',title:'登录场景',revision:3,items:[{id:'S-1',title:'登录锁定'}]};
 globalThis.fetch=(async(input:any)=>{
  const path=String(input);
  const value=path.includes('/workspace-state')?{artifact_id:'scenarios',stages:[{key:'analysis',label:'需求理解',artifact_id:'analysis',revision:2,count:1,status:'current'},{key:'scenarios',label:'测试场景',artifact_id:'scenarios',revision:3,count:1,status:'current'},{key:'cases',label:'测试用例',artifact_id:'cases',revision:1,count:2,status:'stale'}],impact:{status:'pending',summary:'2 条用例待更新',affected:[],source_ids:[]},next_action:{kind:'reconcile',label:'预览更新受影响成果',arguments:{artifact_id:'scenarios',expected_revision:3}}}:artifact;
  return new Response(JSON.stringify(value),{status:200,headers:{'Content-Type':'application/json'}});
 }) as typeof fetch;
 render(<ArtifactWorkspace chatId="chat" artifact={artifact} refreshKey="1" onSelectArtifact={(id:string)=>selected.push(id)} onTarget={()=>{}} onChanged={()=>{}} onUpload={()=>{}} onGenerate={()=>{}} sourceCount={1} running={false}/>);
 await screen.findByRole('navigation',{name:'生成流程'});
 fireEvent.click(await screen.findByRole('button',{name:/测试用例/}));
 assert.deepEqual(selected,['cases']);
 assert.equal(screen.getAllByRole('button',{name:'预览更新受影响成果'}).length,1);
 assert.equal(screen.queryByRole('button',{name:'继续生成用例'}),null);
});

test('a case draft confirmation belongs to the case phase instead of the completed review phase',async()=>{
 const artifact={id:'cases',type:'cases',title:'登录用例',revision:1,items:[{id:'C-1',title:'登录成功'}]};
 globalThis.fetch=(async(input:any)=>new Response(JSON.stringify(String(input).includes('/workspace-state')?{artifact_id:'cases',stages:[{key:'cases',label:'测试用例',artifact_id:'cases',revision:1,count:1,status:'current'},{key:'review',label:'评审',artifact_id:null,revision:null,count:0,status:'missing'}],impact:{status:'current',summary:'用例草稿已保存',affected:[],source_ids:[]},next_action:{kind:'confirm',label:'确认用例，开始评审',arguments:{run_id:'run'}}}:artifact),{status:200,headers:{'Content-Type':'application/json'}})) as typeof fetch;
 render(<ArtifactWorkspace chatId="chat" artifact={artifact} refreshKey="draft" currentRun={{id:'run',status:'waiting',intent:'generate_case',mode:'hitp',stage:'case_draft_review',updated_at:'now',artifact_ids:['cases'],interrupt:{type:'case_draft_review',artifact_id:'cases'}}} onTarget={()=>{}} onChanged={()=>{}} onUpload={()=>{}} onGenerate={()=>{}} sourceCount={1} running={false}/>);
 const phases=within(screen.getByRole('navigation',{name:'生成流程'}));
 await screen.findByText('用例草稿已保存');
 assert.match(phases.getByRole('button',{name:/测试用例/}).textContent??'',/待确认/);
 assert.match(phases.getByRole('button',{name:/评审/}).textContent??'',/未开始/);
});
