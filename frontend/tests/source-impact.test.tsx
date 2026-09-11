import {JSDOM} from 'jsdom';
import {afterEach,test} from 'node:test';
import assert from 'node:assert/strict';

const dom=new JSDOM('<!doctype html><html><body></body></html>',{url:'http://localhost:8000/'});
for(const key of ['window','document','HTMLElement','MutationObserver','Node','Event','MouseEvent'])Object.defineProperty(globalThis,key,{value:(dom.window as any)[key],configurable:true,writable:true});
Object.defineProperty(globalThis,'navigator',{value:dom.window.navigator,configurable:true});
(globalThis as any).IS_REACT_ACT_ENVIRONMENT=true;

const React=await import('react');
const {cleanup,render,screen}=await import('@testing-library/react');
const {ConversationParts}=await import('../src/ConversationParts');
afterEach(()=>cleanup());

const callbacks={onTarget:()=>{},onOpen:()=>{},onChanged:()=>{}};

test('source impact renders affected scope and descendants without internal snapshot data',()=>{
 render(<ConversationParts {...callbacks} response={{id:'impact-turn',status:'succeeded',message:'已检查',pending:[],actions:[],parts:[{type:'source_impact',data:{
  artifact_revision:3,requirement_ids:['REQ-1','REQ-3'],summary:'新增会话注销规则影响登录与账号安全。',global_impact:false,uncertain:false,
  downstream_candidates:[
   {artifact_id:'scenes-secret-id',revision:4,type:'scenarios',candidate_item_ids:['SC-1','SC-4'],status:'potential_impact',partial:false},
   {artifact_id:'cases-secret-id',revision:6,type:'cases',candidate_item_ids:['TC-2'],status:'potential_impact',partial:false},
  ],
  coverage:{requirement_count:4,evidence_count:2,checked_pairs:8,expected_pairs:8,partial:false},
  input_versions:{internal:'DO-NOT-SHOW'},transient_content_digest:'SECRET-DIGEST',refs:['source#1'],
 }}]}}/>);
 assert.ok(screen.getByRole('region',{name:'新增资料影响分析'}));
 assert.ok(screen.getByText('新增会话注销规则影响登录与账号安全。'));
 assert.ok(screen.getByText('REQ-1'));assert.ok(screen.getByText('REQ-3'));
 assert.ok(screen.getByText('2',{selector:'.source-impact-metrics strong'}));
 assert.ok(screen.getByText('3',{selector:'.source-impact-metrics strong'}));
 assert.ok(screen.getByText('8 / 8'));
 const table=screen.getByRole('table',{name:'可能受影响的关联成果'});
 assert.match(table.textContent??'',/测试场景.*SC-1、SC-4.*需要核对/);
 assert.match(table.textContent??'',/测试用例.*TC-2.*需要核对/);
 assert.ok(screen.getByText('检查范围完整：已覆盖 4 条需求和 2 个资料片段。'));
 assert.doesNotMatch(document.body.textContent??'',/DO-NOT-SHOW|SECRET-DIGEST|scenes-secret-id|cases-secret-id|source#1/);
});

test('source impact makes global uncertainty and incomplete scope explicit',()=>{
 render(<ConversationParts {...callbacks} response={{id:'uncertain-turn',status:'succeeded',message:'需确认',pending:[],actions:[],parts:[{type:'source_impact',data:{
  artifact_revision:2,requirement_ids:[],summary:'规则可能影响所有登录方式。',global_impact:true,uncertain:true,
  downstream_candidates:[],coverage:{requirement_count:5,evidence_count:1,checked_pairs:3,expected_pairs:5,partial:true},
 }}]}}/>);
 assert.ok(screen.getByText('影响范围尚不确定，需要人工确认'));
 assert.ok(screen.getByText('当前检查没有定位到现有需求。'));
 assert.ok(screen.getByText('检查范围不完整，请缩小资料或需求后重试。'));
 assert.ok(screen.getByText(/保存更新前请明确采用全部需求范围/));
});
