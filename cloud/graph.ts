import {StateGraph,Annotation,START,END,interrupt,Command,isGraphInterrupt} from '@langchain/langgraph';
import {D1Checkpointer} from './checkpoints';
import {Store,type Fence} from './store';
import {ModelCalls,type ModelInvoke} from './model';
import {canonical,INTENTS,DomainError,ValidationError,LeaseLost,publicValue,now,uid,copy,batches,excerpt,identified,validateItems,applyOperations,profileConfig,checkPage,type Json} from './domain';
import {parseText} from './documents';
const State=Annotation.Root({run_id:Annotation<string>(),intent:Annotation<string>(),analysis_batch:Annotation<number>(),analysis_ref:Annotation<string>(),scenario_page:Annotation<number>(),scenario_ref:Annotation<string>(),case_page:Annotation<number>(),cases_ref:Annotation<string>(),output_ref:Annotation<string>(),done:Annotation<boolean>()});
export const NODES=['route','analysis','clarify','scenarios','scenario_gate','cases','review','single','finish'] as const;
export class Engine {
 calls:ModelCalls;graph:any;
 constructor(public store:Store,public fence:Fence,public signal:AbortSignal,invoke:ModelInvoke){
  this.calls=new ModelCalls(store,fence,signal,invoke);
  const builder:any=new StateGraph(State);
  for(const node of NODES)builder.addNode(node,async(state:Json)=>{
   this.signal.throwIfAborted();this.calls.node=node;const start=Date.now();await this.calls.trace('node.start');
   try{const result=await (this as any)[node](state);this.signal.throwIfAborted();await this.calls.trace('node.complete',{elapsed_ms:Date.now()-start});return result;}
   catch(e){if(isGraphInterrupt(e)){await this.calls.trace('node.interrupted');throw e;}if(this.signal.aborted)throw e;await this.calls.trace('node.error',{error_type:e instanceof Error?e.name:'Error',...(e instanceof ValidationError?{validation_error:e.issue}:{})});throw e;}
  });
  builder.addEdge(START,'route');
  builder.addConditionalEdges('route',(s:Json)=>s.done?'finish':['review_requirement','generate_scenario','generate_case'].includes(s.intent)?'analysis':s.intent==='review_case'?s.cases_ref?'review':'cases':'single',['finish','analysis','review','cases','single']);
  builder.addConditionalEdges('analysis',(s:Json)=>s.analysis_ref?'clarify':'analysis',['clarify','analysis']);
  builder.addConditionalEdges('clarify',(s:Json)=>s.intent==='review_requirement'?'finish':'scenarios',['finish','scenarios']);
  builder.addConditionalEdges('scenarios',(s:Json)=>s.scenario_ref?'scenario_gate':'scenarios',['scenario_gate','scenarios']);
  builder.addConditionalEdges('scenario_gate',(s:Json)=>s.intent==='generate_scenario'?'finish':'cases',['finish','cases']);
  builder.addConditionalEdges('cases',(s:Json)=>s.cases_ref?'review':'cases',['review','cases']);
  builder.addEdge('review','finish');builder.addEdge('single','finish');builder.addEdge('finish',END);
  this.graph=builder.compile({checkpointer:new D1Checkpointer(store,fence)});
 }
 get id(){return this.fence.runId;}
 config(){return {configurable:{thread_id:this.id},recursionLimit:1_000_000,signal:this.signal};}
 async run(){return this.store.get('run',this.id);}
 async stage(stage:string){await this.store.updateRun(this.id,{stage},this.fence);}
 async context(extra:Json={}):Promise<Json>{const run=await this.run();return {request:{content:run._request.content,intent:run._request.intent,mode:run._request.mode},profile:run._profile,conversation:run._conversation,evidence:(await this.store.evidence(run._source_ids)).map(({id,text,location,source_id,role})=>({id,text,location,source_id,role})),artifact:run._artifact_snapshot?publicValue(run._artifact_snapshot):null,selected_ids:run._request.selected_ids,...extra};}
 async grounded(extra:Json={}){const c=await this.context(extra);const refs=new Set(['analysis','scenarios','cases'].flatMap(k=>(extra[k]??[]).flatMap((i:Json)=>i.refs??[])));if(refs.size){c.evidence_catalog=c.evidence.map(({id,location,role}:Json)=>({id,location,role}));c.evidence=c.evidence.filter((e:Json)=>refs.has(e.id)||['change','clarification'].includes(e.role));}return c;}
 async routingContext(){const run=await this.run(),request=run._request;const sources=await Promise.all(run._source_ids.map((id:string)=>this.store.get('source',id)));const roles:Json={};for(const s of sources)roles[s.role]=(roles[s.role]??0)+1;const artifact=run._artifact_snapshot;const history=run._conversation.filter((m:Json)=>m.metadata?.run_id!==this.id);const c={request:{content:excerpt(request.content,4000),intent:request.intent,mode:request.mode,as_requirement:request.as_requirement,original_characters:request.content.length},conversation:history.slice(-4).map((m:Json)=>({role:m.role,content:excerpt(m.content,800),original_characters:m.content.length})),history_messages_available:history.length,sources:{count:sources.length,roles,characters:sources.reduce((n:number,s:Json)=>n+s.characters,0),sample_files:sources.slice(0,3).map((s:Json)=>({name:excerpt(s.name,240),role:s.role}))},artifact:artifact?{id:artifact.id,type:artifact.type,title:excerpt(artifact.title,256),revision:artifact.revision,item_count:artifact.items.length}:null,selected_item_count:request.selected_ids?.length??0};if(JSON.stringify(c).length>12000)throw new DomainError('自动识别上下文超过预算，请明确选择任务类型');return c;}
 async artifact(key:string,kind:string,title:string,items:Json[],report?:Json){return this.store.transaction(tx=>tx.artifact(this.id,key,kind,title,items,report),this.fence);}
 async answer(key:string,text:string,refs:string[]=[]){return this.artifact(key,'answer','回答',[{id:uid('ans_'),title:'回答',description:text,refs}]);}
 async route(state:Json){
  await this.stage('routing');let run=await this.run(),intent=run.intent;
  if(intent==='auto'){intent=(await this.calls.call('route','route',await this.routingContext())).intent;if(!INTENTS.includes(intent))throw new DomainError('模型返回了不支持的任务类型');}
  const sources=['review_case','query','modify'].includes(intent)?[...new Set([...run._source_ids,...run._artifact_source_ids])]:run._source_ids;
  run=await this.store.updateRun(this.id,{intent,_source_ids:sources},this.fence);
  if(['review_requirement','generate_scenario','generate_case','review_case'].includes(intent)&&!(await this.store.evidence(sources)).some(e=>e.role!=='example'))return {intent,output_ref:(await this.answer('missing_source','请上传或粘贴有效需求后重试。参考样例不能作为业务需求证据。')).id,done:true};
  if(intent==='modify'&&!run._artifact_snapshot)return {intent,output_ref:(await this.answer('missing_artifact','请先选择需要修改的产物。')).id,done:true};
  if(intent==='review_case'&&run._artifact_snapshot){if(run._artifact_snapshot.type!=='cases')throw new DomainError('请选择用例产物或上传用例文件');return {intent,cases_ref:run._artifact_snapshot.id};}
  return {intent};
 }
 async analysis(s:Json){
  await this.stage('requirement_analysis');const c=await this.context(),groups=batches(c.evidence.filter((e:Json)=>e.role!=='example')),position=s.analysis_batch??0;
  const result=await this.calls.call(`analysis:${position}`,'analyze_requirement',{...c,evidence:groups[position],batch_index:position,batch_count:groups.length});
  const items=identified(result.items,'req_');validateItems('analysis',items,Object.fromEntries(groups[position].map(e=>[e.id,e])));
  const report=result.report??{};if(!report||typeof report!=='object'||Array.isArray(report))throw new DomainError('分析报告必须为对象');for(const k of ['questions','assumptions'])if(report[k]!==undefined&&(!Array.isArray(report[k])||report[k].some((v:any)=>typeof v!=='string')))throw new DomainError('分析报告问题和假设必须为字符串数组');
  await this.store.setCache(this.id,`analysis_valid:${position}`,{items,report},this.fence);
  if(position+1<groups.length)return {analysis_batch:position+1};
  const parts=await Promise.all(groups.map((_,i)=>this.store.cache(this.id,`analysis_valid:${i}`)));
  const artifact=await this.artifact('analysis_artifact','analysis','需求分析',parts.flatMap((p,i)=>p.items.map((item:Json)=>({...item,id:`B${i+1}-${item.id}`}))),{questions:[...new Set(parts.flatMap(p=>p.report.questions??[]))],assumptions:[...new Set(parts.flatMap(p=>p.report.assumptions??[]))],requirement_map:{segments:parts.map(p=>p.report.requirement_map??{})},diagrams:parts.flatMap(p=>p.report.diagrams??[])});
  return {analysis_ref:artifact.id,output_ref:artifact.id};
 }
 async clarify(s:Json){
  const run=await this.run(),analysis=await this.store.get('artifact',s.analysis_ref),questions=analysis.report?.questions??[];
  if(run.mode!=='hitp'||!questions.length)return {};
  const answer=interrupt({type:'clarification',questions}) as Json;if(typeof answer?.answer!=='string'||!answer.answer.trim())throw new DomainError('请填写澄清答案');await this.stage('applying_clarification');
  if(!await this.store.cache(this.id,'clarification_source')){const {text,chunks}=parseText(answer.answer);await this.store.transaction(async tx=>{if(await tx.maybe('cache',`cache:${this.id}:clarification_source`))return;const source=await tx.addSource(run.chat_id,'用户澄清','clarification',text,chunks);const latest=await tx.get('run',this.id);tx.saveRun({...latest,_source_ids:[...latest._source_ids,source.id]});tx.put('cache',{id:`cache:${this.id}:clarification_source`,value:{id:source.id}});},this.fence);}
  const result=await this.calls.call('clarified_analysis','analyze_requirement',await this.context({analysis:analysis.items,clarification:answer.answer}));
  const art=await this.artifact('clarified_artifact','analysis','需求分析（已澄清）',identified(result.items,'req_'),{...(result.report??{}),questions:[]});return {analysis_ref:art.id,output_ref:art.id};
 }
 async scenarios(s:Json){
  await this.stage('scenario_generation');const page=s.scenario_page??0,previous=page?await this.store.cache(this.id,`scenarios_valid:${page-1}`):null;const analysis=await this.store.get('artifact',s.analysis_ref);
  const context=await this.grounded({analysis:analysis.items,requirement_map:analysis.report?.requirement_map,previous_items:previous?.items??[],cursor:previous?.cursor??null});
  const result=await this.calls.call(`scenarios:${page}`,'generate_scenarios',context);const merged=checkPage(result,previous?.items??[],Object.fromEntries(context.evidence.map((e:Json)=>[e.id,e])),'scenarios',previous?.cursor,previous?.used??[]);
  await this.store.setCache(this.id,`scenarios_valid:${page}`,{items:merged,cursor:result.next_cursor??null,used:[...(previous?.used??[]),previous?.cursor??null]},this.fence);
  if(result.has_more)return {scenario_page:page+1};const art=await this.artifact('scenarios_artifact','scenarios','测试场景',merged,{coverage:merged.map(i=>({scenario_id:i.id,refs:i.refs}))});return {scenario_ref:art.id,output_ref:art.id};
 }
 async scenario_gate(s:Json){
  const run=await this.run();if(run.mode==='hitp'&&s.intent==='generate_case'){
   const art=await this.store.get('artifact',s.scenario_ref);await this.store.transaction(tx=>tx.publish(this.id,[art.id],'请检查并编辑测试场景，确认后继续生成用例。',true),this.fence);
   const response=interrupt({type:'scenario_review',artifact_id:art.id,items:art.items}) as Json;if(response?.approved!==true)throw new DomainError('请确认场景后继续');
  }return {};
 }
 async cases(s:Json){
  await this.stage(s.intent==='review_case'?'case_import':'case_generation');const page=s.case_page??0,previous=page?await this.store.cache(this.id,`cases_valid:${page-1}`):null;
  const scenarios=s.scenario_ref?(await this.store.get('artifact',s.scenario_ref)).items:[];const context=await this.grounded({scenarios,previous_items:previous?.items??[],cursor:previous?.cursor??null});
  const evidence=Object.fromEntries(context.evidence.map((e:Json)=>[e.id,e]));const ids=scenarios.length?new Set<string>(scenarios.map((i:Json)=>i.id)):undefined;const task=s.intent==='review_case'?'import_cases':'generate_cases';
  let result=await this.calls.call(`cases:${page}`,task,context);const original=copy(result);let merged:Json[]=[];
  for(let attempt=0;attempt<2;attempt++){
   try{merged=checkPage(result,previous?.items??[],evidence,'cases',previous?.cursor,previous?.used??[],ids);if(attempt)this.preserveRepair(original,result,evidence,ids,previous?.items??[],previous?.cursor,previous?.used??[]);break;}
   catch(e){if(!(e instanceof ValidationError))throw e;await this.calls.trace('cases.validation_failed',{validation_error:e.issue});if(attempt)throw new DomainError('格式修正后仍未通过校验，原有用例已保留。'+e.message);await this.calls.trace('cases.repair_started');result=await this.calls.call(`cases:${page}:repair`,task,{...context,validation_repair:{validation_error:e.issue,previous_response:result}});await this.calls.trace('cases.repair_complete');}
  }
  await this.store.setCache(this.id,`cases_valid:${page}`,{items:merged,cursor:result.next_cursor??null,used:[...(previous?.used??[]),previous?.cursor??null]},this.fence);
  if(result.has_more)return {case_page:page+1};const art=await this.artifact('cases_artifact','cases','测试用例',merged);return {cases_ref:art.id};
 }
 preserveRepair(old:Json,next:Json,evidence:Json,scenarioIds?:Set<string>,previous:Json[]=[],cursor?:string,used:string[]=[]){
  const changed=(path:string,value:any)=>{throw new ValidationError(path,'unchanged_valid_page_content',value,'repair_changed_page');};
  if(!Array.isArray(old.items)||old.items.length!==next.items.length)changed('items',next.items);
  if(typeof old.has_more==='boolean'&&old.has_more!==next.has_more)changed('has_more',next.has_more);
  if(old.has_more&&typeof old.next_cursor==='string'&&old.next_cursor&&old.next_cursor!==cursor&&!used.includes(old.next_cursor)&&old.next_cursor!==next.next_cursor)changed('next_cursor',next.next_cursor);
  const counts=new Map<string,number>();for(const item of old.items)if(typeof item?.id==='string')counts.set(item.id,(counts.get(item.id)??0)+1);
  old.items.forEach((item:Json,i:number)=>{
   if(!item||typeof item!=='object'||Array.isArray(item))return;
   const stable=typeof item.id==='string'&&item.id.length>0&&item.id.length<=200&&counts.get(item.id)===1&&!previous.some(p=>p.id===item.id);
   if(stable&&item.id!==next.items[i].id)changed(`items[${i}].id`,next.items[i].id);
   try{validateItems('cases',[{...item,id:'validation-placeholder'}],evidence,scenarioIds);}catch(e){if(e instanceof ValidationError)return;throw e;}
   const value=(o:Json)=>canonical(Object.fromEntries(Object.entries(o).filter(([k])=>k!=='id')));
   if(value(item)!==value(next.items[i]))changed(`items[${i}]`,next.items[i]);
  });
 }
 async review(s:Json){
  await this.stage('case_review');const run=await this.run(),art=await this.store.get('artifact',s.cases_ref),snapshot=s.intent==='review_case'?run._artifact_snapshot??art:art;
  let result=await this.store.cache(this.id,'case_review')??{},revised=art;
  if(!await this.store.cache(this.id,'review_applied')){
   const scenarios=s.scenario_ref?(await this.store.get('artifact',s.scenario_ref)).items:[];const context=await this.grounded({cases:snapshot.items,scenarios});const evidence=Object.fromEntries((await this.store.evidence([...new Set<string>([...snapshot._source_ids,...run._source_ids])])).map(e=>[e.id,e]));
   result=await this.calls.call('case_review','review_cases',context);
   for(let attempt=0;attempt<2;attempt++){
    try{
     const operations=this.addIds(result.operations,'tc_');const items=applyOperations(snapshot.items,operations,run._request.selected_ids);validateItems('cases',items,evidence,scenarios.length?new Set(scenarios.map((i:Json)=>i.id)):undefined);
     revised=await this.store.transaction(async tx=>{const value=await tx.revise(art.id,snapshot.revision,items,'ai_review',this.id,'review_applied');tx.put('cache',{id:`cache:${this.id}:case_review`,value:result});return value;},this.fence);if(attempt)await this.calls.trace('review.repair_complete');break;
    }catch(e){if(!(e instanceof ValidationError))throw e;await this.calls.trace('review.validation_failed',{validation_error:e.issue});if(attempt)throw new DomainError('评审修正后仍未通过校验，原始用例已保留。'+e.message);await this.calls.trace('review.repair_started');result=await this.calls.call('case_review_repair','review_cases',{...context,validation_repair:{validation_error:e.issue,previous_response:result}});}
   }
  }
  if(s.intent==='review_case'){const report=await this.artifact('review_report','review','用例审查报告',[],result.report??{});await this.store.setCache(this.id,'additional_outputs',{ids:[report.id]},this.fence);}
  return {output_ref:revised.id};
 }
 addIds(operations:any,prefix:string){if(Array.isArray(operations))return operations.map(op=>op?.op==='add'&&op.item&&typeof op.item==='object'?{...op,item:{...op.item,id:op.item.id||uid(prefix)}}:op);return operations;}
 async single(s:Json){
  await this.stage(s.intent);const run=await this.run(),context=await this.context();let art:Json;
  if(s.intent==='query'){
   if(!context.evidence.some((e:Json)=>e.role!=='example'))art=await this.answer('query_no_evidence','当前没有可引用的需求证据。请上传需求或选择已有产物后再提问。');
   else{const r=await this.calls.call('query','query',context);if(typeof r.answer!=='string'||!Array.isArray(r.refs)||!r.refs.length)throw new DomainError('回答必须带有效证据引用');art=await this.answer('answer_artifact',r.answer,r.refs);}
  }else if(s.intent==='learn_template'){
   if(!context.evidence.length&&!context.artifact)art=await this.answer('template_no_sample','请上传参考样例或选择已有用例，再学习格式。');
   else{const r=await this.calls.call('template','learn_template',context);art=await this.artifact('proposal_artifact','proposal','Profile 配置建议',[{id:uid('proposal_'),title:'模板学习建议',description:String(r.summary??''),refs:[]}],{config:profileConfig(r.config)});}
  }else if(s.intent==='modify'){
   if(await this.store.cache(this.id,'modify_applied'))art=await this.store.get('artifact',run._artifact_snapshot.id);
   else{const r=await this.calls.call('modify','modify',context);const items=applyOperations(run._artifact_snapshot.items,this.addIds(r.operations,'item_'),run._request.selected_ids);art=await this.store.transaction(tx=>tx.revise(run._artifact_snapshot.id,run._artifact_snapshot.revision,items,'ai_modify',this.id,'modify_applied'),this.fence);}
  }else throw new DomainError('不支持的任务类型');return {output_ref:art.id};
 }
 async finish(s:Json){if((await this.run()).status==='completed')return {};await this.stage('publishing');if(!s.output_ref)throw new DomainError('任务没有生成最终产物');const art=await this.store.get('artifact',s.output_ref),extra=await this.store.cache(this.id,'additional_outputs');const proposal=art.type==='proposal'?art.report?.config:undefined;await this.store.transaction(tx=>tx.publish(this.id,[art.id,...(extra?.ids??[])],proposal?'已生成配置建议，请查看并选择是否应用。':'已完成，请查看下方结果。',false,proposal),this.fence);return {};}
 async editWaiting(){
  this.calls.node='paused_edit';const run=await this.run(),edit=run._pending_edit;if(!edit)return;
  await this.calls.trace('node.start');const snapshot=edit.artifact;const context=await this.context({request:{content:edit.content,intent:'modify',mode:run.mode},artifact:publicValue(snapshot),selected_ids:edit.selected_ids});
  const result=await this.calls.call('paused_edit:'+edit.id,'modify',context);const items=applyOperations(snapshot.items,this.addIds(result.operations,'item_'),edit.selected_ids);
  await this.store.transaction(async tx=>{const latest=await tx.get('run',this.id);if(latest._pending_edit?.id!==edit.id)throw new LeaseLost();const art=await tx.revise(snapshot.id,snapshot.revision,items,'ai_paused_edit',this.id,'edit_applied:'+edit.id);tx.saveRun({...latest,status:'waiting',stage:'scenario_review',_pending_edit:null,error:null,interrupt:{...latest.interrupt,items:art.items}});},this.fence);await this.calls.trace('node.complete');
 }
 async execute(){
  try{
   let run=await this.run();if(run._pending_edit){await this.editWaiting();return;}
   if(!['queued','running'].includes(run.status))return;await this.store.updateRun(this.id,{status:'running',error:null},this.fence);
   const config=this.config();const state=await this.graph.getState(config);const interrupts=(state.tasks??[]).flatMap((t:Json)=>t.interrupts??[]);const existing=!!Object.keys(state.values??{}).length;await this.calls.trace('checkpoint.loaded',{has_state:existing,next_nodes:state.next??[]});
   let input:any=existing?null:{run_id:this.id,intent:run.intent,analysis_batch:0,scenario_page:0,case_page:0};
   if(run._resume&&interrupts.some((i:Json)=>i.id===run._resume.interrupt_id))input=new Command({resume:{[run._resume.interrupt_id]:run._resume.value}});
   if(existing&&!state.next?.length&&!interrupts.length){await this.finish(state.values);return;}
   await this.graph.invoke(input,config);this.signal.throwIfAborted();
   const after=await this.graph.getState(config),pending=(after.tasks??[]).flatMap((t:Json)=>t.interrupts??[]);
   if(pending.length){await this.store.updateRun(this.id,{status:'waiting',stage:pending[0].value.type,interrupt:pending[0].value,_interrupt_id:pending[0].id,_resume:null},this.fence);}
   else if((await this.run()).status==='completed')await this.calls.trace('run.completed');
  }catch(e){
   if(this.signal.aborted||e instanceof LeaseLost){const latest=await this.run();if(latest.status==='completed'||latest.status==='cancelled')return;try{await this.store.updateRun(this.id,{status:latest._pending_edit?'waiting':'queued',stage:'suspended'},this.fence);await this.calls.trace('run.suspended');}catch{}return;}
   const latest=await this.run();if(latest.status==='completed'||latest.status==='cancelled')return;
   const message=e instanceof DomainError?e.message:'云端任务执行失败，请查看诊断并重试当前阶段';
   if(latest._pending_edit){await this.store.updateRun(this.id,{status:'waiting',stage:'scenario_review',_pending_edit:null,error:message},this.fence);await this.calls.trace('node.error',{error_type:e instanceof Error?e.name:'Error'});}
   else{await this.store.updateRun(this.id,{status:'failed',error:message,_failed_cache_key:latest._active_call_key??null},this.fence);await this.calls.trace('run.failed',{error_type:e instanceof Error?e.name:'Error'});}
  }
 }
}
