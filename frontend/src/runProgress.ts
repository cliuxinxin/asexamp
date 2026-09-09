import type {Json} from './types';

export const stageNames:Record<string,string>={agent_repair:'修复出错字段',agent_repair_batch:'集中修复引用',paused_edit:'修改待确认场景',queued:'等待执行',routing:'理解请求',route:'理解请求',requirement_analysis:'分析需求',analysis:'分析需求',applying_clarification:'应用澄清',clarify:'需求澄清',scenario_generation:'生成场景',scenarios:'生成场景',scenario_gate:'场景确认',scenario_review:'等待场景确认',case_generation:'生成用例',case_import:'导入用例',cases:'生成用例',case_review:'评审用例',review:'评审用例',finish:'保存结果',publishing:'保存结果',single:'处理请求',query:'证据问答',modify:'修改结果',learn_template:'学习模板'};
export type ModelOutput={id:string;node:string;task:string;label:string;text:string;status:string;at:string;elapsed?:number;attempt?:number;streaming?:boolean;requestAvailable?:boolean};
export type ProgressEntry={id:number;at:string;text:string;tone:string;callId?:string};
export type Feed={lastId:number;entries:ProgressEntry[];calls:Record<string,ModelOutput>;latest:Record<string,string>};
export const emptyFeed=():Feed=>({lastId:0,entries:[],calls:{},latest:{}});

export function reduceEvent(feed:Feed,kind:string,id:number,data:Json):Feed{
 if(!Number.isSafeInteger(id)||id<=feed.lastId)return feed;
 const next={...feed,lastId:id,calls:{...feed.calls},latest:{...feed.latest}};
 if(kind==='model_delta'){
  const call=next.calls[data.call_id];
  if(call&&typeof data.text==='string')next.calls[call.id]={...call,text:call.text+data.text};
  return next;
 }
 if(kind!=='progress')return next;
 const event=data.event as string;const node=data.node??'';const label=stageNames[data.task]??(node==='paused_edit'?stageNames[node]:(stageNames[data.stage]??stageNames[node]??data.task??'处理任务'));
 const callId=data.call_id??next.latest[node];const call=next.calls[callId];
 const finalize=(status:string)=>{for(const output of Object.values(next.calls))if(['running','returned'].includes(output.status))next.calls[output.id]={...output,status};};
 const add=(text:string,tone='neutral',outputId?:string)=>{next.entries=[...next.entries,{id,at:data.at,text,tone,callId:outputId}];};
 if(event==='model.start'){
  const title=label+(data.batch_count?` · 第 ${data.batch_index}/${data.batch_count} 批`:'')+(data.previous_items?` · 已有 ${data.previous_items} 条`:'');
  next.calls[data.call_id]={id:data.call_id,node,task:data.task,label:title,text:'',status:'running',at:data.at,attempt:data.attempt,streaming:data.streaming};
  next.latest[node]=data.call_id;add(title,'active',data.call_id);
 }else if(event==='model.request_saved'&&call){next.calls[callId]={...call,requestAvailable:true};
 }else if(event==='model.waiting'&&call){next.calls[callId]={...call,elapsed:data.elapsed_ms};
 }else if(['model.complete','model.error','model.cancelled'].includes(event)&&call){
  if(['running','returned'].includes(call.status))next.calls[callId]={...call,status:event==='model.complete'?'returned':event==='model.error'?'failed':'cancelled',elapsed:data.elapsed_ms};
 }else if(event==='node.start'){add(`开始${stageNames[node]??label}`,'active');
 }else if(event==='node.complete'){
  if(call&&call.status==='returned')next.calls[callId]={...call,status:'accepted'};
  add(`${stageNames[node]??label}完成${typeof data.elapsed_ms==='number'?` · ${(data.elapsed_ms/1000).toFixed(1)} 秒`:''}`,'success');
 }else if(event==='node.cancelled'){
  if(call&&['running','returned'].includes(call.status))next.calls[callId]={...call,status:'cancelled'};
  add(`${stageNames[node]??label}已停止`,'waiting');
 }else if(event==='node.interrupted'){add('等待你的补充或确认','waiting');
 }else if(event==='agent.repair_applied'){
  if(call)next.calls[callId]={...call,status:'applied'};
  add(`已应用 ${data.field_count??1} 个字段的修正，正在校验当前工作项`,'active');
 }else if(event==='agent.repair_rejected'){
  if(call)next.calls[callId]={...call,status:'invalid'};
  add('修复回复未符合指定字段要求，未应用；继续调整修复方式','waiting');
 }else if(event.endsWith('.validation_failed')){
  if(call&&call.status!=='applied')next.calls[callId]={...call,status:'invalid'};
  const issue=data.validation_error;const reference=issue&&String(issue.expected).includes('evidence_id');add(reference?`引用校验未通过：${issue.path} 不属于当前可用的业务证据，正在核对引用与原文`:`校验未通过${issue?`：${issue.path}，期望 ${issue.expected}，实际 ${issue.actual}`:''}`,'error');
 }else if(event.endsWith('.repair_started')){add('正在根据校验反馈修正格式','waiting');
 }else if(event.endsWith('.repair_complete')){add('当前工作项修复后已通过完整校验，继续后续流程','success');
 }else if(event==='node.error'){
  if(call&&call.status==='returned')next.calls[callId]={...call,status:'failed'};
  add(`${stageNames[node]??label}失败${data.validation_error?`：${data.validation_error.path}`:''}`,'error');
 }else if(event==='model.retry'){add(`模型请求失败，开始第 ${data.next_attempt} 次尝试`,'waiting');
 }else if(event==='model.cache_hit'){add('读取已保存的模型结果','neutral');
 }else if(event==='checkpoint.loaded'){add(data.has_state?'已加载检查点，继续保存的进度':'开始新的执行流程');
 }else if(event==='run.resumed'){add('已收到确认，继续执行','active');
 }else if(event==='run.retried'){add('从失败阶段重新执行','active');
 }else if(event==='run.recovered'){finalize('cancelled');add('服务重启，正在恢复任务','waiting');
 }else if(event==='edit.recovered'){finalize('cancelled');add('服务重启，上次未完成的场景修改可以重新提交','waiting');
 }else if(event==='run.completed'){add('全部步骤已完成','success');
 }else if(event==='run.cancelled'){finalize('cancelled');add('任务已停止','waiting');
 }else if(event==='run.failed'){finalize('failed');add('任务失败，可重试当前阶段','error');
 }else if(event==='run.suspended'){finalize('cancelled');add('服务已暂停，重启后可恢复任务','waiting');}
 return next;
}

