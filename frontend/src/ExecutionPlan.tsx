import {useEffect,useState} from 'react';
import {api} from './api';

export type ExecutionPlanStep={id:string;title:string;status:string;message?:string};
export type ExecutionPlanData={id:string;chat_id?:string;title:string;status:string;steps:ExecutionPlanStep[];waiting_prompt_id?:string};
type PlanProps={chatId:string;planId:string;snapshot?:ExecutionPlanData;summary?:string;refreshKey?:string};
type StatusView={label:string;tone:string};
const STATUS:Record<string,StatusView>={
 pending:{label:'待执行',tone:'pending'},queued:{label:'待执行',tone:'pending'},
 running:{label:'处理中',tone:'running'},waiting_pipeline:{label:'等待后台处理',tone:'running'},
 waiting_confirmation:{label:'等待确认',tone:'waiting'},needs_confirmation:{label:'等待确认',tone:'waiting'},
 completed:{label:'已完成',tone:'completed'},succeeded:{label:'已完成',tone:'completed'},
 cancelled:{label:'已取消',tone:'cancelled'},canceled:{label:'已取消',tone:'cancelled'},
 failed:{label:'未完成',tone:'failed'},blocked:{label:'需要补充信息',tone:'waiting'},
 interrupted:{label:'已暂停',tone:'waiting'}
};
const statusView=(status:string):StatusView=>STATUS[status]??{label:'未完成',tone:'failed'};

function validPlan(value:unknown,id:string,chatId:string):value is ExecutionPlanData{
 if(!value||typeof value!=='object')return false;
 const plan=value as ExecutionPlanData;
 return plan.id===id&&(!plan.chat_id||plan.chat_id===chatId)&&typeof plan.title==='string'&&typeof plan.status==='string'&&Array.isArray(plan.steps)&&plan.steps.every(step=>step&&typeof step.id==='string'&&typeof step.title==='string'&&typeof step.status==='string');
}

export function ExecutionPlan({chatId,planId,snapshot,summary,refreshKey}:PlanProps){
 const identity=chatId+':'+planId;
 const [record,setRecord]=useState<{identity:string;plan:ExecutionPlanData}>();
 const [error,setError]=useState<string>();
 const [attempt,setAttempt]=useState(0);
 const [loading,setLoading]=useState(false);
 const plan=record?.identity===identity?record.plan:validPlan(snapshot,planId,chatId)?snapshot:undefined;
 const refreshFailed=error===identity;
 useEffect(()=>{
  let active=true;
  setLoading(true);setError(undefined);
  api<ExecutionPlanData>(`/chats/${encodeURIComponent(chatId)}/plans/${encodeURIComponent(planId)}`).then(value=>{
   if(!active)return;
   if(!validPlan(value,planId,chatId))throw new Error('Invalid plan response');
   setRecord({identity,plan:value});
  }).catch(()=>{if(active)setError(identity);}).finally(()=>{if(active)setLoading(false);});
  return()=>{active=false;};
 },[chatId,planId,identity,refreshKey,attempt]);

 const waiting=plan?.steps.find(step=>step.status==='waiting_confirmation'||step.status==='needs_confirmation');
 const blocked=plan?.steps.find(step=>step.status==='blocked'||step.status==='failed'||step.status==='interrupted');
 const current=waiting??blocked;
 const currentMessage=typeof current?.message==='string'&&current.message.trim()?current.message:waiting?'请按当前对话提示确认，之后继续后续安排。':undefined;
 const notes=plan?.steps.filter(step=>step!==current&&typeof step.message==='string'&&step.message.trim())??[];
 const overall=plan?statusView(plan.status):undefined;
 return <section className="execution-plan" aria-label="本次安排" aria-busy={loading}>
  <header><strong>本次安排</strong>{overall&&<span className={'execution-plan-status is-'+overall.tone}>{overall.label}</span>}</header>
  {(plan?.title||summary)&&<p className="execution-plan-title">{plan?.title||summary}</p>}
  {plan?<ol aria-label="执行步骤">{plan.steps.map((step,index)=>{
   const view=statusView(step.status);
   return <li key={step.id} className={'is-'+view.tone}><span className="execution-plan-number" aria-hidden="true">{index+1}</span><span className="execution-plan-step-title">{step.title}</span><span className="execution-plan-step-status">{view.label}</span></li>;
  })}</ol>:!refreshFailed&&<p className="execution-plan-note" role="status">正在读取安排…</p>}
  {currentMessage&&<p className="execution-plan-current" role="status">{currentMessage}</p>}
  {notes.length>0&&<details className="execution-plan-details"><summary>查看步骤说明</summary>{notes.map(step=><p key={step.id}><strong>{step.title}：</strong>{step.message}</p>)}</details>}
  {refreshFailed&&<div className="execution-plan-error"><p role="alert">{plan?'暂时无法更新安排，仍显示上次记录。':'暂时无法读取安排，请刷新重试。'}</p><button type="button" disabled={loading} onClick={()=>setAttempt(value=>value+1)}>刷新安排</button></div>}
 </section>;
}
