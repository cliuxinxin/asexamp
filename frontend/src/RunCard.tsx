import {useEffect,useState} from 'react';
import {Check,RotateCcw,Square,Plus} from 'lucide-react';
import {api,errText} from './api';
import {ArtifactCard} from './ArtifactCard';
import {RunTimeline} from './RunTimeline';
import {ProjectSupplement} from './ProjectSupplement';
import {ErrorBox,Spinner} from './ui';
import {ClarificationDraftEditor} from './ClarificationDraftEditor';
import {useConversationCommand} from './conversation';
import type {Artifact,Run,Source} from './types';

const stages:Record<string,string>={input_check:'检查资料用途',source_review:'确认本轮需求资料',summarizing:'整理 AI 总结',understand:'理解需求与业务图',understanding_gate:'确认理解与方案',apply_answer:'应用澄清',dispatch:'识别任务',publish:'保存结果',planning:'制定测试计划',agent_plan:'调整测试计划',agent_analyze:'理解业务与建立流程图',strategy_review:'确认测试方向',coverage:'检查覆盖缺口',agent_summary:'整理本轮结论',routing:'理解你的请求',requirement_analysis:'分析需求',applying_clarification:'应用澄清信息',scenario_generation:'生成测试场景',case_generation:'设计测试用例',case_import:'读取已有用例',case_review:'评审与优化',publishing:'保存最终结果',resuming:'恢复任务',retrying:'重试失败阶段',route:'理解你的请求',analyze:'分析需求',analyze_requirement:'分析需求',analysis:'分析需求',clarify:'等待需求澄清',clarification:'等待需求澄清',scenarios:'生成测试场景',generate_scenarios:'生成测试场景',scenario_review:'等待场景确认',cases:'设计测试用例',generate_cases:'设计测试用例',review:'评审与优化',review_cases:'评审与优化',finalize:'保存最终结果',complete:'已完成',done:'已完成',query:'检索需求证据',learn_template:'学习模板',modify:'修改产物',queued:'等待执行'};
const reviewGates:Record<string,{title:string;message:string;confirm:string;next:string;stop:string;allow:string}>={
 strategy_review:{title:'确认需求理解与测试方案',message:'请检查需求理解、业务图与测试范围。可以直接编辑，或通过聊天预览修订。',confirm:'确认理解，生成场景',next:'scenarios',stop:'analysis',allow:'确认理解，允许生成场景'},
 scenario_review:{title:'确认测试场景',message:'请检查场景与需求覆盖。确认后将使用最新保存版本生成用例。',confirm:'确认场景，继续生成用例',next:'cases',stop:'scenarios',allow:'确认场景，允许生成用例'},
 case_draft_review:{title:'确认用例草稿',message:'请检查用例目的、前置条件、步骤和预期结果，再决定是否进入评审。',confirm:'确认用例，开始评审',next:'review',stop:'cases',allow:'确认用例，允许评审'},
 case_result_review:{title:'确认用例评审结果',message:'请检查评审意见和当前用例，完成必要修改后确认本轮结果。',confirm:'确认评审，完成任务',next:'complete',stop:'review',allow:'确认评审，完成任务'},
};
export function RunCard({run,onChanged,onTarget,onPrompt,onSettings,interactionBusy=false,sources=[],onSupplementBusy,saveToProject:sharedPreference,onSaveToProjectChange}:{run:Run;saveToProject?:boolean;onSaveToProjectChange?:(value:boolean)=>void;sources?:Source[];onSupplementBusy?:(busy:boolean)=>void;onChanged:()=>void;onTarget:(artifact:Artifact,ids:string[])=>void;onPrompt?:(text:string,artifactId?:string)=>void;onSettings?:()=>void;interactionBusy?:boolean}){
 const [clock,setClock]=useState(Date.now());
 const [confirmedDepth,setConfirmedDepth]=useState('');
 const [chosenSources,setChosenSources]=useState<string[]>([]);
 const [localShare,setLocalShare]=useState(true),[supplementOpen,setSupplementOpen]=useState(false),[supplementBusy,setSupplementBusy]=useState(false);
 const saveToProject=sharedPreference??localShare;
 const setSaveToProject=(value:boolean)=>{setLocalShare(value);onSaveToProjectChange?.(value);};
 const [answer,setAnswer]=useState('');const [error,setError]=useState('');const [notice,setNotice]=useState('');const [working,setWorking]=useState(false);
 const command=useConversationCommand();
 // Polling timestamps and refreshed suggestion wording must not replace the user's draft.
 const answerScope=JSON.stringify([run.id,run.interrupt?.type,run.interrupt?.artifact_id,run.interrupt?.questions,run.interrupt?.sources?.map(source=>source.id)]);
 async function action(name:string,body:object={},label?:string){
  setError('');setNotice('');setWorking(true);
  try{
   const revision=run.artifact_revision??run.interrupt?.artifact_revision;
   const result=await command({name:name==='resume'?'workflow.continue':'workflow.'+name,arguments:{run_id:run.id,...(name==='resume'&&run.interrupt_id?{interrupt_id:run.interrupt_id}:{}),...(name==='resume'&&run.control_version!==undefined?{expected_control_version:run.control_version}:{}),...(name==='resume'&&revision!==undefined?{expected_revision:revision}:{}),...body}},label??({resume:'确认当前阶段，继续任务',cancel:'停止整个任务',retry:'重试未完成步骤',pause:'暂停当前任务'} as Record<string,string>)[name]??name,undefined,run.chat_id);
   if(result.status==='succeeded'){setAnswer('');setNotice(result.message);}
   else if(result.status==='deferred')setNotice(result.message||'操作已登记，完成当前步骤后处理。');
   else setError(result.message||'当前操作需要补充信息，请查看后重试。');
   onChanged();
  }catch(e){setError(errText(e));}finally{setWorking(false);}
 }
 useEffect(()=>{setAnswer(run.interrupt?.type==='source_review'?run.interrupt.suggested_text??'':'');setLocalShare(true);},[answerScope]);
 const agentMode=run.experience==='agent'||run.graph_version===2;
 const pending=run.status==='queued'||run.status==='running';const pause=run.status==='waiting';
 const gate=run.interrupt?reviewGates[run.interrupt.type]:undefined;
 const goalReached=!!gate&&(run.stop_after??run.interrupt?.stop_after)===gate.stop;
 const confirmLabel=gate?(goalReached?gate.allow:run.interrupt?.confirm_label??gate.confirm):'';
 const legacyCasesStop=run.interrupt?.type==='workflow_paused'&&run.interrupt.reason==='stop_after'&&run.stop_after==='cases';
 const confirmationBusy=working||interactionBusy||supplementBusy||!!run.edit_in_progress;
 const confirmationMessage=interactionBusy||supplementBusy?'正在处理当前请求，完成后可继续确认。':'正在保存当前修改，保存完成后可继续确认。';
 const canSupplement=pause&&['generate_case','generate_scenario','review_requirement'].includes(run.intent);
 const supplementWorking=(busy:boolean)=>{setSupplementBusy(busy);onSupplementBusy?.(busy);};
 useEffect(()=>{if(!pending)return;const timer=setInterval(()=>setClock(Date.now()),1000);return()=>clearInterval(timer);},[pending]);
 const detail=run.diagnostic;const waiting=!!detail&&['model.start','model.waiting','model.transport_start'].includes(detail.event);
 const seconds=(value:number)=>Math.max(0,Math.floor(value/1000));
 const elapsed=run.created_at?seconds(clock-Date.parse(run.created_at)):undefined;
 const modelElapsed=detail?seconds((detail.elapsed_ms??0)+Math.max(0,clock-Date.parse(detail.at))):0;
 if(run.status==='completed')return null;
 return <section className={'run-card '+(run.status==='failed'?'failed':'')} aria-live="polite"><div className="run-heading"><span className="inline">{pending?<Spinner/>:<span className={'status-dot '+run.status}/>}<strong>{pending?(stages[run.stage]??'正在处理'):pause?'需要你确认':run.status==='failed'?'任务暂停：处理失败':'任务已停止'}</strong></span><div className="actions">{run.status==='failed'&&<button disabled={working} onClick={()=>action('retry')}><RotateCcw size={15}/>重试未完成步骤</button>}{canSupplement&&<button disabled={confirmationBusy} onClick={()=>setSupplementOpen(true)}><Plus size={14}/>补充资料</button>}{pending&&<button disabled={working} onClick={()=>action('pause')}>暂停</button>}{(pending||pause)&&<button disabled={working||supplementBusy} onClick={()=>action('cancel')}><Square size={13}/>停止</button>}</div></div>
 <p className="muted small-text">本轮模式：{run.mode==='hitp'?'Human · 逐步确认':'Auto · 自动完成'}{run.mode==='hitp'&&run.pause_contract!==2?' · 此任务沿用创建时的确认节点':''}</p>
 {notice&&<p role="status" className="small-text">{notice}</p>}
 {run.status==='failed'&&<a className="text-accent run-log-link" href={'/api/runs/'+encodeURIComponent(run.id)+'/failed-step'} download>下载失败步骤日志（精简，≤64 KB）</a>}
 {run.generation_plan&&<p className="muted small-text">{run.generation_plan.reason}{run.generation_plan.input_groups>1?` · ${run.generation_plan.input_groups} 个分组`:''}</p>}
 {run.progress&&<div className="count-progress"><div><strong>{run.progress.label}</strong><span>{run.progress.total>1?`已完成 ${run.progress.completed} / ${run.progress.total}`:run.status==='waiting'?'等待你确认':run.status==='failed'?'已暂停':'处理中'}</span></div><progress max={Math.max(1,run.progress.total)} value={run.progress.completed}/>{run.status==='failed'&&run.error&&<p className="error-text">失败：{run.error}</p>}</div>}
 {pending&&<div className="run-progress"><p className="muted small-text">{elapsed!==undefined?`本轮已运行 ${elapsed} 秒。`:''}任务在本机后台执行，可以关闭此页面后再回来。</p>{detail?.batch_count&&<p className="small-text">第 {detail.batch_index}/{detail.batch_count} 批需求</p>}{waiting&&<p className="small-text">等待模型响应 · 本次请求 {modelElapsed} 秒{detail.timeout_seconds?` · 超时设置 ${detail.timeout_seconds} 秒`:''}{detail.attempt?` · 第 ${detail.attempt}/${detail.max_attempts} 次尝试`:''}</p>}{detail?.event==='model.retry'&&<p className="small-text">上次模型请求失败，准备第 {detail.next_attempt} 次尝试。</p>}</div>}
 {run.recovery&&<div className="recovery-guide"><strong>{run.recovery.title}</strong><p>{run.recovery.detail}</p><ul>{run.recovery.suggestions.map((suggestion,index)=><li key={index}>{suggestion}</li>)}</ul>{run.recovery.preserved.length>0&&<p className="preserved-progress">已保留：{run.recovery.preserved.join('、')}</p>}{onSettings&&['authentication','configuration','connection','model','transport','network'].includes(run.recovery.category)&&<button onClick={onSettings}>打开模型设置</button>}</div>}
 {run.error&&!run.recovery&&!run.progress&&<p className="preserve error-text">{run.error}</p>}
 <RunTimeline runId={run.id} restartKey={run.status} onChanged={onChanged} agentMode={agentMode} initialAgent={run.agent} initialOpen={true}/>
 {pause&&run.interrupt?.type==='source_review'&&<div className="clarification"><p>{run.interrupt.message}</p>{run.interrupt.sources?.map(source=><label className="check-label" key={source.id}><input type="checkbox" checked={chosenSources.includes(source.id)} onChange={e=>setChosenSources(old=>e.target.checked?[...old,source.id]:old.filter(id=>id!==source.id))}/>{source.name} · 本轮作为需求使用</label>)}<textarea aria-label="补充需求正文" value={answer} onChange={e=>setAnswer(e.target.value)} placeholder="也可以直接粘贴需求正文"/><button className="primary" disabled={confirmationBusy||(!chosenSources.length&&!answer.trim())} onClick={()=>action('resume',{source_ids:chosenSources,answer})}>确认资料，继续当前任务</button></div>}
 {pause&&run.interrupt?.type==='clarification'&&<ClarificationDraftEditor runId={run.id} controlVersion={run.control_version} interruptId={run.interrupt_id} refreshKey={run.updated_at} onChanged={onChanged} disabled={confirmationBusy} saveToProject={saveToProject} onSaveToProjectChange={setSaveToProject}/>}
 {pause&&run.interrupt?.type==='workflow_paused'&&<div className="clarification"><p>{run.interrupt.message??'任务已暂停，已保存成果会保留。'}</p><button className="primary" disabled={confirmationBusy} onClick={()=>action('resume',legacyCasesStop?{stop_after:'review'}:{},legacyCasesStop?'允许评审，继续当前任务':undefined)}>{legacyCasesStop?'允许评审，继续当前任务':'继续当前任务'}</button></div>}
 {pause&&gate&&run.interrupt&&<section className="scenario-review" aria-label={run.interrupt.title??gate.title}>
  <h3>{run.interrupt.title??gate.title}</h3><p>{run.interrupt.message??gate.message}</p>
  <p className="small-text">下一步：{stages[run.interrupt.next_stage??gate.next]??run.interrupt.next_stage??gate.next}。{goalReached?'当前任务已达到原定停止位置，点击下方按钮才允许进入下一阶段。':'确认后继续下一阶段。'}</p>
  <p className="muted small-text">查看、提问、估算和保存修改都会保留当前确认节点；应用预览后仍需明确继续。</p>
  {onPrompt&&<div className="actions">
   <button disabled={confirmationBusy} onClick={()=>onPrompt('请解释当前成果的依据和待确认事项，只读查看，不修改或继续任务。',run.interrupt?.artifact_id)}>在聊天中提问</button>
   <button disabled={confirmationBusy} onClick={()=>onPrompt('只查看当前成果的需求、场景与用例覆盖关系，不修改或继续任务。',run.interrupt?.artifact_id)}>在聊天中查看覆盖</button>
   <button disabled={confirmationBusy} onClick={()=>onPrompt('请先预览当前成果的修改，不应用，也不继续任务。',run.interrupt?.artifact_id)}>在聊天中预览修改</button>
  </div>}
  {run.interrupt.artifact_id&&<ArtifactCard key={run.interrupt.artifact_id} id={run.interrupt.artifact_id} initialDetailsOpen={run.interrupt.type.startsWith('case_')} refreshKey={run.updated_at} onTarget={onTarget} onChanged={onChanged}/>}
  {run.interrupt.type==='strategy_review'&&<label>本轮测试深度<select aria-label="确认测试深度" value={confirmedDepth} disabled={confirmationBusy} onChange={e=>setConfirmedDepth(e.target.value)}><option value="">沿用本轮 Profile</option><option value="quick">Quick · 核心路径</option><option value="standard">Standard · 主要规则</option><option value="deep">Deep · 深入组合</option></select></label>}
  {confirmationBusy&&<p className="small-text">{confirmationMessage}</p>}
  <button className="primary" disabled={confirmationBusy} onClick={()=>action('resume',{approved:true,...(run.interrupt?.type==='strategy_review'&&confirmedDepth?{depth:confirmedDepth}:{}),...(goalReached?{stop_after:gate.next}:{})},confirmLabel)}><Check size={16}/>{confirmLabel}</button>
 </section>}
 {supplementOpen&&<ProjectSupplement run={run} sources={sources} onClose={()=>setSupplementOpen(false)} onChanged={onChanged} onBusy={supplementWorking} disabled={interactionBusy||!!run.edit_in_progress}/>}
 <details className="run-diagnostics"><summary>运行详情</summary><p className="small-text">任务：{run.id}<br/>阶段：{run.stage}{detail?.node?` · 节点：${detail.node}`:''}</p>{detail&&<><p className="small-text">最近事件：{detail.event} · {new Date(detail.at).toLocaleTimeString()}</p><pre>{JSON.stringify(detail,null,2)}</pre></>}<a className="text-accent" href={'/api/runs/'+encodeURIComponent(run.id)+'/debug-bundle'} download>下载完整诊断包</a><p className="muted small-text">诊断包包含本轮发送内容、原始回答、校验错误和检查点，不含模型认证请求头。</p></details><ErrorBox message={error}/></section>;
}
