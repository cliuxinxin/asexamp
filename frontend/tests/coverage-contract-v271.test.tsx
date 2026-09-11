import {JSDOM} from 'jsdom';
import {test} from 'node:test';
import assert from 'node:assert/strict';

const dom=new JSDOM('<!doctype html><html><body></body></html>',{url:'http://localhost:8000/'});
for(const key of ['window','document','HTMLElement','HTMLDialogElement','MutationObserver','Node','Event','MouseEvent'])Object.defineProperty(globalThis,key,{value:(dom.window as any)[key],configurable:true,writable:true});
Object.defineProperty(globalThis,'navigator',{value:dom.window.navigator,configurable:true});
(globalThis as any).IS_REACT_ACT_ENVIRONMENT=true;
const React=await import('react');
const {render,fireEvent,screen,cleanup}=await import('@testing-library/react');
const {ConversationParts}=await import('../src/ConversationParts');
const {WorkspaceCoverage}=await import('../src/WorkspaceCoverage');

function workspaceSnapshot(){
 return {
  artifact_id:'scenario-art',revision:3,analysis_artifact_id:'analysis-art',scenario_artifact_id:'scenario-art',selected_case_artifact_id:'cases-current',
  related_artifacts:[{id:'analysis-art',type:'analysis',title:'登录需求',revision:2,linked:true},{id:'cases-old',type:'cases',title:'登录用例第一版',revision:1,linked:true},{id:'cases-current',type:'cases',title:'登录用例第二版',revision:2,linked:true}],
  sources:[],lineage:{analysis_artifact_id:'analysis-art',analysis_revision:1},
  stale:{analysis:true,scenarios:true,changed_requirement_ids:['R1'],changed_scenario_ids:['S1']},
  coverage:{
   totals:{requirements:3,requirements_with_scenarios:2,requirements_with_cases:1,scenarios:2,scenarios_with_cases:1,cases:2},
   scenarios:[{id:'S1',title:'成功登录',case_ids:['C1','C2'],case_count:2,requirement_ids:['R1'],status:'covered',artifact_id:'scenario-art'},{id:'S2',title:'失败登录',case_ids:[],case_count:0,requirement_ids:['R2'],status:'missing_cases',artifact_id:'scenario-art'}],
   requirements:[{id:'R1',title:'账号登录',scenario_ids:['S1'],case_ids:['C1','C2'],status:'covered'},{id:'R2',title:'错误提示',scenario_ids:['S2'],case_ids:[],status:'missing_cases'},{id:'R3',title:'账号锁定',scenario_ids:[],case_ids:[],status:'missing_scenarios'}],
   orphan_cases:[],notes:['存在多个用例成果分支；当前仅统计所选的一个分支，未累加重复生成的用例。']
  }
 };
}

test('chat coverage renders the full workspace payload as a read-only snapshot',()=>{
 const originalFetch=globalThis.fetch;
 const requests:string[]=[];
 globalThis.fetch=(async(url:any)=>{requests.push(String(url));throw new Error('A saved chat snapshot must not request live data');}) as typeof fetch;
 const data=workspaceSnapshot(),before=structuredClone(data);
 try{
  render(<ConversationParts response={{id:'coverage-turn',client_message_id:'message',status:'succeeded',message:'覆盖检查完成',parts:[{type:'coverage',data}],pending:[],actions:[]}} onTarget={()=>assert.fail('coverage changed the target')} onChanged={()=>assert.fail('coverage mutated the workspace')}/>);
  assert.ok(screen.getByText('2 / 3'));
  assert.ok(screen.getByText('1 / 3'));
  assert.ok(screen.getByText('1 / 2'));
  assert.ok(screen.getByRole('row',{name:'S1 · 成功登录 C1、C2 2 已有用例'}));
  assert.ok(screen.getByRole('row',{name:'S2 · 失败登录 — 0 待补用例'}));
  assert.ok(screen.getByText('R1 · 账号登录'));
  assert.ok(screen.getByText('R3 · 账号锁定'));
  assert.ok(screen.getByText(/场景已更新，关联用例仍基于较早版本.*变更场景：S1/));
  assert.ok(screen.getByText(/需求理解已更新，关联成果仍基于较早版本/));
  assert.ok(screen.getByText(/存在多个用例成果分支/));
  assert.equal(screen.queryByRole('combobox'),null);
  fireEvent.click(screen.getByRole('button',{name:'收起覆盖关系'}));
  fireEvent.click(screen.getByRole('button',{name:'查看需求 → 场景 → 用例覆盖'}));
  assert.ok(screen.getByText('2 / 3'));
  assert.deepEqual(requests,[]);
  assert.deepEqual(data,before);
 }finally{cleanup();globalThis.fetch=originalFetch;}
});

test('coverage snapshots keep flat compatibility and never expose a live branch selector',()=>{
 const originalFetch=globalThis.fetch;
 const requests:string[]=[];
 globalThis.fetch=(async(url:any)=>{requests.push(String(url));throw new Error('A supplied snapshot must not fetch');}) as typeof fetch;
 try{
  const data=workspaceSnapshot();
  const view=render(<WorkspaceCoverage artifactId="scenario-art" revision={3} artifactType="scenarios" initialData={data} initialOpen/>);
  assert.ok(screen.getByText(/场景已更新，关联用例仍基于较早版本/));
  assert.equal(screen.queryByRole('combobox'),null);
  view.rerender(<WorkspaceCoverage artifactId="scenario-art" revision={3} artifactType="scenarios" initialData={data.coverage} initialOpen/>);
  assert.ok(screen.getByText('2 / 3'));
  assert.ok(screen.getByRole('row',{name:'S1 · 成功登录 C1、C2 2 已有用例'}));
  assert.ok(screen.getByText('R1 · 账号登录'));
  assert.ok(screen.getByText(/存在多个用例成果分支/));
  assert.equal(screen.queryByText(/场景已更新，关联用例仍基于较早版本/),null);
  assert.equal(screen.queryByRole('combobox'),null);
  assert.deepEqual(requests,[]);
 }finally{cleanup();globalThis.fetch=originalFetch;}
});
