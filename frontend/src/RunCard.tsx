import {useEffect,useState} from 'react';
import {Check,RotateCcw,Square} from 'lucide-react';
import {api,errText} from './api';
import {ArtifactCard} from './ArtifactCard';
import {RunTimeline} from './RunTimeline';
import {ErrorBox,Spinner} from './ui';
import type {Artifact,Run} from './types';

const stages:Record<string,string>={routing:'理解你的请求',requirement_analysis:'分析需求',applying_clarification:'应用澄清信息',scenario_generation:'生成测试场景',case_generation:'设计测试用例',case_import:'读取已有用例',case_review:'评审与优化',publishing:'保存最终结果',resuming:'恢复任务',retrying:'重试失败阶段',route:'理解你的请求',analyze:'分析需求',analyze_requirement:'分析需求',analysis:'分析需求',clarify:'等待需求澄清',clarification:'等待需求澄清',scenarios:'生成测试场景',generate_scenarios:'生成测试场景',scenario_review:'等待场景确认',cases:'设计测试用例',generate_cases:'设计测试用例',review:'评审与优化',review_cases:'评审与优化',finalize:'保存最终结果',complete:'已完成',done:'已完成',query:'检索需求证据',learn_template:'学习模板',modify:'修改产物',queued:'等待执行'};
export function RunCard({run,onChanged,onTarget}:{run:Run;onChanged:()=>void;onTarget:(artifact:Artifact,ids:string[])=>void}){
 const [clock,setClock]=useState(Date.now());
 const [answer,setAnswer]=useState('');const [error,setError]=useState('');const [working,setWorking]=useState(false);
 async function action(name:string,body:object={}){setError('');setWorking(true);try{await api('/runs/'+run.id+'/'+name,body);setAnswer('');onChanged();}catch(e){setError(errText(e));}finally{setWorking(false);}}
 const pending=run.status==='queued'||run.status==='running';const pause=run.status==='waiting';
 useEffect(()=>{if(!pending)return;const timer=setInterval(()=>setClock(Date.now()),1000);return()=>clearInterval(timer);},[pending]);
 const detail=run.diagnostic;const waiting=!!detail&&['model.start','model.waiting','model.transport_start'].includes(detail.event);
 const seconds=(value:number)=>Math.max(0,Math.floor(value/1000));
 const elapsed=run.created_at?seconds(clock-Date.parse(run.created_at)):undefined;
 const modelElapsed=detail?seconds((detail.elapsed_ms??0)+Math.max(0,clock-Date.parse(detail.at))):0;
 if(run.status==='completed')return null;
 return <section className={'run-card '+(run.status==='failed'?'failed':'')} aria-live="polite"><div className="run-heading"><span className="inline">{pending?<Spinner/>:<span className={'status-dot '+run.status}/>}<strong>{pending?(stages[run.stage]??'正在处理'):pause?'需要你确认':run.status==='failed'?'任务暂停：处理失败':'任务已停止'}</strong></span><div className="actions">{run.status==='failed'&&<button disabled={working} onClick={()=>action('retry')}><RotateCcw size={15}/>重试此阶段</button>}{(pending||pause)&&<button disabled={working} onClick={()=>action('cancel')}><Square size={13}/>停止</button>}</div></div>
 {pending&&<div className="run-progress"><p className="muted small-text">{elapsed!==undefined?`本轮已运行 ${elapsed} 秒。`:''}任务在本机后台执行，可以关闭此页面后再回来。</p>{detail?.batch_count&&<p className="small-text">第 {detail.batch_index}/{detail.batch_count} 批需求</p>}{waiting&&<p className="small-text">等待模型响应 · 本次请求 {modelElapsed} 秒{detail.timeout_seconds?` · 超时设置 ${detail.timeout_seconds} 秒`:''}{detail.attempt?` · 第 ${detail.attempt}/${detail.max_attempts} 次尝试`:''}</p>}{detail?.event==='model.retry'&&<p className="small-text">上次模型请求失败，准备第 {detail.next_attempt} 次尝试。</p>}</div>}
 {run.error&&<p className="preserve error-text">{run.error}</p>}
 <RunTimeline runId={run.id} restartKey={run.status} onChanged={onChanged}/>
 {pause&&run.interrupt?.type==='clarification'&&<div className="clarification"><p>以下信息会影响测试设计，请补充后继续。</p><ol>{run.interrupt.questions?.map((question,index)=><li key={index}>{typeof question==='string'?question:JSON.stringify(question)}</li>)}</ol><textarea aria-label="回答澄清问题" value={answer} onChange={e=>setAnswer(e.target.value)} placeholder="输入你的确认或补充说明…" rows={4}/><button className="primary" disabled={working||!answer.trim()} onClick={()=>action('resume',{answer})}><Check size={16}/>提交并继续</button></div>}
 {pause&&run.interrupt?.type==='scenario_review'&&<div className="scenario-review"><p>请检查场景。可以直接编辑，确认后将使用最新版本生成用例。</p>{run.interrupt.artifact_id&&<ArtifactCard id={run.interrupt.artifact_id} refreshKey={run.updated_at} onTarget={onTarget} onChanged={onChanged}/>}<button className="primary" disabled={working} onClick={()=>action('resume',{approved:true})}><Check size={16}/>确认场景，继续生成用例</button></div>}
 <details className="run-diagnostics"><summary>运行详情</summary><p className="small-text">任务：{run.id}<br/>阶段：{run.stage}{detail?.node?` · 节点：${detail.node}`:''}</p>{detail&&<><p className="small-text">最近事件：{detail.event} · {new Date(detail.at).toLocaleTimeString()}</p><pre>{JSON.stringify(detail,null,2)}</pre></>}<a className="text-accent" href={'/api/runs/'+encodeURIComponent(run.id)+'/diagnostics?download=true'} download>下载诊断日志</a><p className="muted small-text">日志包含执行元数据，不包含完整需求、提示词、模型回答或 API Key。</p></details><ErrorBox message={error}/></section>;
}
