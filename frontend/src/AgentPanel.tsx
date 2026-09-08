import {Check,Circle,Lightbulb,ListChecks} from 'lucide-react';
import {EvidenceRefs} from './EvidenceRefs';
import {Spinner} from './ui';
import {depthNames} from './types';
import type {AgentState,Coverage} from './types';

export function CoveragePanel({coverage}:{coverage:Coverage}){
 const gaps=Array.isArray(coverage.gaps)?coverage.gaps:[];
 return <div className="coverage-panel"><div className="coverage-metrics"><span><strong>{coverage.requirements_covered} / {coverage.requirements_total}</strong> 需求已关联用例</span><span><strong>{coverage.branches_covered} / {coverage.branches_total}</strong> 业务分支已关联用例</span></div>{gaps.length>0&&<details open><summary>还有 {gaps.length} 个覆盖缺口</summary><ul>{gaps.map((gap,index)=><li key={gap.id??index}>{gap.title??gap.description??gap.label??gap.requirement_id??gap.branch_id??gap.id??'待补充的覆盖项'}{gap.refs&&<EvidenceRefs refs={gap.refs}/>}</li>)}</ul></details>}<p className="muted small-text">基于本次已确认需求和业务图的设计关联统计，尚未执行这些测试。</p></div>;
}

export function AgentPanel({agent}:{agent?:AgentState}){
 if(!agent)return <p className="agent-intro muted">正在阅读上下文，制定本次测试方案…</p>;
 const plan=agent.plan??[];
 const insights=agent.insights??[];
 return <div className="agent-panel" aria-label="测试设计进展">
  {(agent.depth||agent.rationale)&&<div className="strategy-caption"><span className="depth-badge">{agent.depth?depthNames[agent.depth]:'本次方案'}</span><p>{agent.rationale}</p></div>}
  {plan.length>0&&<div className="agent-plan"><div className="agent-section-label"><ListChecks size={16}/><strong>本次计划</strong><small>{plan.filter(step=>step.status==='completed').length} / {plan.length} 项完成</small></div><ol>{plan.map(step=><li key={step.id} className={'plan-step '+step.status}>{step.status==='completed'?<Check size={15}/>:step.status==='running'?<Spinner/>:<Circle size={13}/>}<span>{step.title}</span>{step.status==='blocked'&&<small>等待确认</small>}</li>)}</ol></div>}
  {insights.length>0&&<div className="agent-insights"><div className="agent-section-label"><Lightbulb size={16}/><strong>分析与依据</strong></div>{insights.map(item=><div className={'insight '+item.kind} key={item.id}><span className="insight-kind">{{finding:'发现',decision:'判断',question:'待确认',summary:'阶段小结'}[item.kind]??'分析'}</span><p>{item.summary}</p><EvidenceRefs refs={item.refs??[]}/></div>)}</div>}
  {!!agent.pending_instructions&&<p role="status" className="instruction-pending">已收到 {agent.pending_instructions} 条补充，将在当前步骤结束后应用。</p>}
  {agent.coverage&&<CoveragePanel coverage={agent.coverage}/>}
  {agent.summary&&<div className="agent-summary"><strong>本轮总结</strong><p className="preserve">{agent.summary}</p></div>}
 </div>;
}
