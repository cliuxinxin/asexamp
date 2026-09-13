import {FileText} from 'lucide-react';
import type {ConversationPrompt,Run} from './types';

const stages:Record<string,string>={intake:'理解任务',understand:'理解需求',analysis:'分析需求',requirement_review:'确认需求',strategy_review:'确认需求理解',scenario_generation:'生成场景',scenarios:'生成场景',scenario_review:'确认场景',case_generation:'生成用例',cases:'生成用例',case_draft_review:'确认用例',case_result_review:'确认评审结果',case_review:'评审用例',review:'评审用例',clarification:'补充信息',completed:'已完成'};

export function WorkflowSummary({run,prompt,hasResult,onOpen}:{run?:Run;prompt?:ConversationPrompt|null;hasResult:boolean;onOpen:()=>void}){
 if(!run&&!prompt&&!hasResult)return null;
 const status=run?.status??(prompt?'waiting':'completed');
 const label=stages[prompt?.kind??'']??(prompt?.kind==='workflow_gate'?prompt.title:undefined)??stages[run?.stage??'']??prompt?.title??(hasResult?'已完成':'当前任务');
 const active=status==='queued'||status==='running'||prompt?.busy||prompt?.kind==='busy';
 const detail=active
   ?'AI 正在处理，请稍候。你仍可在对话中补充说明。'
  :status==='waiting'||prompt
   ?'等待你的确认。你可以先提问、修改或补充资料，再在对话中回复。'
   :status==='failed'||status==='cancelled'
    ?`${run?.error||'本轮未完成。'} 请在对话中说明调整或要求重试。`
    :status==='completed'?'本轮已完成，可以继续提问、修改或查看成果。':'可以继续在对话中说明下一步。';
 return <section className={'workflow-summary '+status} aria-label="当前工作流">
  <div className="workflow-summary-copy"><span className={'status-dot '+status}/><div><small>当前阶段</small><strong>{label}</strong><p>{detail}</p></div></div>
  {hasResult&&<button onClick={onOpen}><FileText size={16}/>查看当前成果</button>}
 </section>;
}
