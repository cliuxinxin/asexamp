import type {Json} from './types';

export const stageNames:Record<string,string>={dispatch:'识别本次任务',inputs:'检查资料用途',understand:'理解需求',clarification_gate:'收集你的补充',apply_answer:'保存澄清，沿用理解',understanding_gate:'确认理解与方案',conversation:'回答问题',summarize:'整理总结',summarizing:'整理总结',publish:'保存结果',complete_case_fields:'补全模板字段',complete_case_descriptions:'补全用例描述',link_scenarios:'补全场景关联',paused_edit:'修改待确认场景',queued:'等待执行',routing:'理解请求',route:'理解请求',requirement_analysis:'分析需求',analysis:'分析需求',applying_clarification:'应用澄清',clarify:'需求澄清',scenario_generation:'生成场景',scenarios:'生成场景',scenario_gate:'场景确认',scenario_review:'等待场景确认',case_generation:'生成用例',case_import:'导入用例',cases:'生成用例',case_review:'评审用例',review:'评审用例',finish:'保存结果',publishing:'保存结果',single:'处理请求',query:'证据问答',modify:'修改结果',learn_template:'学习模板'};
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
 const event=data.event as string;const node=data.node??'';const label=node==='paused_edit'?stageNames[node]:(stageNames[data.stage]??stageNames[node]??data.task??'处理任务');
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
 }else if(event==='case_fields.completion_started'){add(`正在补全 ${data.missing_count} 条用例的模板字段，保留已有内容`,'active');
 }else if(event==='case_fields.completion_complete'){add(`已补全 ${data.completed_count} 项模板字段${data.unresolved_count?`；${data.unresolved_count} 项缺少依据，待补充`:''}`,data.unresolved_count?'waiting':'success');
 }else if(event==='analysis.reused'){add(`沿用已保存的需求理解 · ${data.requirement_count} 条 · v${data.revision}`,'success');
 }else if(event==='clarification.applied'){add('澄清已保存；需求 ID 和已有理解保持不变','success');
 }else if(event==='scenarios.linking_started'){add(`正在补全 ${data.scenario_count} 个场景的需求关联`,'active');
 }else if(event==='scenarios.linking_complete'){add('场景关联已补全，继续生成用例','success');
 }else if(event==='workflow.step_saved'){
  const count=Math.max(0,...Object.values(data.item_counts??{}).map(v=>Number(v)));
  if(count)add(`${stageNames[data.step]??'当前阶段'}已保存 · ${count} 条`,'success');
 }else if(event==='model.json_local_repair'){add('已补齐 JSON 结束括号，继续校验；无需重新请求模型','success');
 }else if(event==='json.repair_started'){add(`返回格式需要修正，正在自动修复 · 第 ${data.repair_attempt} 次`,'waiting');
 }else if(event==='node.start'){add(`开始${stageNames[node]??label}`,'active');
 }else if(event==='node.complete'){
  if(call&&call.status==='returned')next.calls[callId]={...call,status:'accepted'};
  add(`${stageNames[node]??label}完成${typeof data.elapsed_ms==='number'?` · ${(data.elapsed_ms/1000).toFixed(1)} 秒`:''}`,'success');
 }else if(event==='node.cancelled'){
  if(call&&['running','returned'].includes(call.status))next.calls[callId]={...call,status:'cancelled'};
  add(`${stageNames[node]??label}已停止`,'waiting');
 }else if(event==='node.interrupted'){add('等待你的补充或确认','waiting');
 }else if(event.endsWith('.validation_failed')){
  if(call)next.calls[callId]={...call,status:'invalid'};
  const issue=data.validation_error??data.errors?.[0]??data.validation_errors?.[0];add(`校验未通过${issue?`：${issue.path} · ${issue.code??'字段错误'}，${typeof issue.expected==='string'?issue.expected:JSON.stringify(issue.expected)}${issue.actual?'，实际 '+issue.actual:''}`:''}`,'error');
 }else if(event.endsWith('.repair_started')){add('正在根据校验反馈修正格式','waiting');
 }else if(event.endsWith('.repair_complete')){add('格式修正已通过校验','success');
 }else if(event==='node.error'){
  if(call&&call.status==='returned')next.calls[callId]={...call,status:'failed'};
  add(`${stageNames[node]??label}失败${data.validation_error?`：${data.validation_error.path}`:''}`,'error');
 }else if(event==='model.retry'){add(`模型请求失败，开始第 ${data.next_attempt} 次尝试`,'waiting');
 }else if(event==='model.cache_hit'){add('读取已保存的模型结果','neutral');
 }else if(event==='checkpoint.loaded'){add(data.has_state?'已加载检查点，继续保存的进度':'开始新的执行流程');
 }else if(event==='edit.started'){add('AI 正在修改当前确认结果','active');
 }else if(event==='edit.saved'){add(`AI 修改已保存${data.artifact_revision?` · v${data.artifact_revision}`:''}，仍等待你确认`,'success');
 }else if(event==='resume.blocked_by_edit'){add('修改尚未保存，已保留当前确认节点','waiting');
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
