import {useEffect,useRef,useState} from 'react';
import type {ReactNode} from 'react';
import {Check,FileText,RefreshCw,Sparkles,Upload} from 'lucide-react';
import {ArtifactCard} from './ArtifactCard';
import {ChangePreview} from './ArtifactActions';
import {api,errText} from './api';
import {useConversationCommand} from './conversation';
import {ErrorBox,Spinner} from './ui';
import type {Artifact,Json,Run} from './types';

type Phase='analysis'|'scenarios'|'cases'|'review';
type Stage={key:Phase;label:string;artifact_id:string|null;revision:number|null;count:number;status:string};
type Proposal={id:string;artifact_id:string;summary?:string;changes:Json[]};
type WorkspaceState={current_gate?:{run_id:string;artifact_id?:string;type?:string;revision?:number}|null;artifact_id:string|null;stages:Stage[];impact:{status:string;summary:string;affected:{artifact_id:string;type:string;item_ids:string[];count:number;reason:string}[];source_ids:string[]};next_action:{kind:string;label:string;arguments:Json};pending_proposal?:Proposal|null};
const phaseNames:Record<Phase,string>={analysis:'需求理解',scenarios:'测试场景',cases:'测试用例',review:'评审'};
const statusNames:Record<string,string>={current:'已保存',stale:'待更新',missing:'未开始',running:'处理中',waiting:'待确认',completed:'已完成',needs_review:'需核对'};
const typeNames:Record<string,string>={analysis:'需求',scenarios:'场景',cases:'用例'};

export function ArtifactWorkspace({artifact,chatId='',refreshKey,onTarget,onChanged,onUpload,onGenerate,onSelectArtifact,sourceCount,running,currentRun,renderRun}:{artifact?:Artifact;chatId?:string;refreshKey:string;onTarget:(artifact:Artifact,ids:string[])=>void;onChanged:()=>void;onUpload:()=>void;onGenerate:(content?:string,intent?:string,args?:Json)=>void;onSelectArtifact?:(id:string,manual?:boolean)=>void;sourceCount:number;running:boolean;currentRun?:Run;renderRun?:(hideActions:boolean)=>ReactNode}){
 const command=useConversationCommand();
 const [state,setState]=useState<WorkspaceState>();
 const [activePhase,setActivePhase]=useState<Phase>((artifact?.type as Phase)||'analysis');
 const [loading,setLoading]=useState(false),[working,setWorking]=useState(false),[error,setError]=useState(''),[notice,setNotice]=useState(''),[epoch,setEpoch]=useState(0);
 const [localProposal,setLocalProposal]=useState<Proposal>();
 const [originals,setOriginals]=useState<Record<string,Artifact>>({});
 const previousChat=useRef(chatId),autoSelected=useRef('');const requestedPhase=useRef<{key:Phase;artifactId:string}|undefined>(undefined);
 const selectRef=useRef(onSelectArtifact);selectRef.current=onSelectArtifact;
 useEffect(()=>{const requested=requestedPhase.current;setActivePhase(requested&&requested.artifactId===artifact?.id?requested.key:(artifact?.type as Phase)||'analysis');if(requested?.artifactId===artifact?.id)requestedPhase.current=undefined;},[artifact?.id]);
 useEffect(()=>{
  if(previousChat.current!==chatId){setState(undefined);setLocalProposal(undefined);setNotice('');autoSelected.current='';previousChat.current=chatId;}
  if(!chatId){setState(undefined);return;}
  let alive=true;setLoading(true);setError('');
  api<WorkspaceState>('/chats/'+encodeURIComponent(chatId)+'/workspace-state'+(artifact?.id?'?artifact_id='+encodeURIComponent(artifact.id):'')).then(value=>{
   if(!alive)return;
   if(!Array.isArray(value.stages))return;
   setState(value);setLocalProposal(undefined);
   if(!artifact?.id&&value.artifact_id&&autoSelected.current!==value.artifact_id){autoSelected.current=value.artifact_id;selectRef.current?.(value.artifact_id,false);}
  }).catch(e=>{if(alive)setError(errText(e));}).finally(()=>{if(alive)setLoading(false);});
  return()=>{alive=false;};
 },[chatId,artifact?.id,refreshKey,epoch]);
 const proposal=localProposal??state?.pending_proposal??undefined;
 useEffect(()=>{
  if(!proposal){setOriginals({});return;}
  let alive=true;
  Promise.all(proposal.changes.filter(change=>!change.before_items).map(async change=>[change.artifact_id,await api<Artifact>('/artifacts/'+encodeURIComponent(change.artifact_id)+'/revisions/'+change.expected_revision)] as const)).then(rows=>{if(alive)setOriginals(Object.fromEntries(rows));}).catch(e=>{if(alive)setError(errText(e));});
  return()=>{alive=false;};
 },[proposal?.id]);
 const hasImpact=state?.impact?.status==='pending';
 const action=state?.next_action;
 const processing=working||!!currentRun&&['queued','running'].includes(currentRun.status);
 const branchHasGate=!!state&&(Object.hasOwn(state,'current_gate')?!!state.current_gate:!currentRun?.interrupt?.artifact_id||state.stages.some(stage=>stage.artifact_id===currentRun.interrupt?.artifact_id));
 const runVisible=!!renderRun&&(currentRun?.status!=='waiting'||branchHasGate);
 const needsInput=branchHasGate&&currentRun?.status==='waiting'&&['clarification','source_review','source_roles','input_required'].includes(currentRun.interrupt?.type??'');
 const blocked=running||loading||working||!!error||!!currentRun?.edit_in_progress||action?.kind==='generate'&&!artifact&&!sourceCount;
 const phase=currentRun?.interrupt?.type??currentRun?.stage??'';
 const runPhase:Phase=/case_draft/.test(phase)?'cases':/case_result|review_result|^review$|review_cases|case_review|complete/.test(phase)?'review':/case/.test(phase)?'cases':/scenario/.test(phase)?'scenarios':'analysis';
 const confirmationArtifact=currentRun?.status==='waiting'?currentRun.interrupt?.artifact_id:undefined;
 const stages:Stage[]=(Object.keys(phaseNames) as Phase[]).map(key=>{
  const saved=state?.stages.find(stage=>stage.key===key);
  let result:Stage=saved??{key,label:phaseNames[key],artifact_id:artifact?.type===key?artifact.id:key==='review'&&artifact?.type==='cases'?artifact.id:null,revision:artifact?.type===key?artifact.revision:null,count:artifact?.type===key?artifact.items.length:0,status:artifact?.type===key?'current':'missing'};
  if(currentRun&&(currentRun.status!=='waiting'||branchHasGate)&&runPhase===key&&result.status!=='stale'&&['queued','running','waiting'].includes(currentRun.status))result={...result,status:currentRun.status==='waiting'?'waiting':'running'};
  return result;
 });
 async function perform(name:string,args:Json,label:string){
  setWorking(true);setError('');setNotice('');
  try{
   const result=await command({name,arguments:args},label,undefined,chatId);
   if(!['succeeded','deferred','needs_confirmation'].includes(result.status))throw new Error(result.message||'请补充本次更新的范围。');
   const diff=result.parts.find(part=>part.type==='diff');
   if(diff?.type==='diff')setLocalProposal({id:diff.proposal_id,artifact_id:args.artifact_id??artifact?.id??'',summary:result.message,changes:diff.changes});
   else {setLocalProposal(undefined);setState(previous=>previous?{...previous,pending_proposal:null}:previous);}
   setNotice(result.message);setEpoch(value=>value+1);onChanged();
  }catch(e){setError(errText(e));}finally{setWorking(false);}
 }
 function next(){
  if(!action)return;
  if(action.kind==='reconcile')void perform('workspace.reconcile',{...action.arguments,scope:'all',preview:true},action.label);
  else if(action.kind==='generate')onGenerate(action.arguments.content,action.arguments.intent,action.arguments);
  else if(action.kind==='confirm')void perform('workflow.continue',action.arguments,action.label);
 }
 const hideRunActions=!!proposal||hasImpact||loading||working;
 const showAction=!proposal&&!needsInput&&!processing&&action&&['reconcile','generate'].includes(action.kind);
 return <section className="results-workspace unified-workspace" aria-label="用例工作区">
  <header className="results-header"><div><p className="eyebrow">TEST DESIGN WORKSPACE</p><h1>测试设计工作区</h1><p className="muted">理解需求，完善场景，持续更新用例。</p></div><span className="version-pill">2.8.0</span></header>
  <nav className="phase-navigator" aria-label="生成流程">{stages.map((stage,index)=><button key={stage.key} aria-current={activePhase===stage.key?'step':undefined} disabled={!stage.artifact_id} onClick={()=>{setActivePhase(stage.key);requestedPhase.current=stage.artifact_id&&stage.artifact_id!==artifact?.id?{key:stage.key,artifactId:stage.artifact_id}:undefined;if(stage.artifact_id&&stage.artifact_id!==artifact?.id)onSelectArtifact?.(stage.artifact_id);}} className={'phase-step '+(activePhase===stage.key?'active ':'')+stage.status}><span className="phase-number">{index+1}</span><span><strong>{stage.label}</strong><small>{statusNames[stage.status]??stage.status}{stage.artifact_id&&stage.key!=='review'?` · ${stage.count} 条`:''}</small></span></button>)}</nav>
  <div className="workspace-scroll">
   <section className={'workspace-next-action '+(hasImpact?'has-impact':'')} aria-label="下一步">
    <div className="workspace-impact-heading"><div><p className="eyebrow">{proposal?'修改待应用':hasImpact?'变更影响':'当前进度'}</p><strong>{proposal?proposal.summary||'修改预览已准备好。':state?.impact?.summary||(artifact?'当前成果已保存。':'从一份需求开始。')}</strong>{hasImpact&&<p className="muted small-text">{currentRun?.status==='waiting'?'预览本分支全部受影响内容；应用后仍保留当前确认节点。':'预览本分支全部受影响内容，再应用到关联成果。'}</p>}</div>{showAction&&<button className="primary" disabled={blocked} onClick={next}>{working?<Spinner/>:<RefreshCw size={16}/>} {action.label}</button>}</div>
    {!!state?.impact?.affected?.length&&<details className="impact-details"><summary>查看受影响范围</summary><ul>{state.impact.affected.map((item,index)=><li key={item.artifact_id+':'+index}><strong>{item.count} 条{typeNames[item.type]??'成果'}</strong> · {item.reason}{item.item_ids?.length?` · ${item.item_ids.join('、')}`:''}</li>)}</ul></details>}
    {proposal&&<div className="workspace-proposal" aria-label="待应用修改"><details open><summary>查看修改明细</summary>{proposal.changes.map((change,index)=><ChangePreview key={change.artifact_id+':'+index} change={change} before={originals[change.artifact_id]}/>)}</details><div className="actions"><button className="primary" disabled={blocked} onClick={()=>void perform('artifact.apply',{proposal_id:proposal.id},'应用更新')}>{working?<Spinner/>:<Check size={16}/>}应用更新</button><button disabled={working} onClick={()=>void perform('artifact.discard',{proposal_id:proposal.id},'取消此次更新')}>取消此次更新</button></div></div>}
    {working&&<p className="inline small-text" role="status"><Spinner/>正在处理本次修改…</p>}
    {notice&&!proposal&&<p className="small-text" role="status">{notice}</p>}
    <ErrorBox message={error}/>{error&&<button onClick={()=>{setError('');setEpoch(value=>value+1);}}>重新读取工作区</button>}
    {confirmationArtifact&&(confirmationArtifact!==artifact?.id||activePhase!==runPhase)&&<div className="workspace-gate-context"><span>{branchHasGate?'当前确认对象：':'另一个分支等待确认：'}{phaseNames[runPhase]}{currentRun?.artifact_revision??currentRun?.interrupt?.artifact_revision?` · v${currentRun?.artifact_revision??currentRun?.interrupt?.artifact_revision}`:''}</span><button onClick={()=>{setActivePhase(runPhase);onSelectArtifact?.(confirmationArtifact);}}>查看待确认成果</button></div>}
    {runVisible&&renderRun?.(hideRunActions)}
    {!runVisible&&!proposal&&!needsInput&&action?.kind==='confirm'&&<button className="primary" disabled={blocked} onClick={next}>{action.label}</button>}
   </section>
   <div className="results-body">{artifact?<ArtifactCard key={artifact.id} id={artifact.id} refreshKey={refreshKey} onTarget={onTarget} onChanged={onChanged} simplified reviewMode={activePhase==='review'}/>:<div className="intake-workspace">
    <span className="intake-icon"><FileText size={28}/></span><h2>把需求变成测试设计</h2><p>上传需求文档，或在右侧描述业务。<br/>需求理解、场景和用例会保留在这里。</p>
    <button className="upload-zone" disabled={running} onClick={onUpload}><Upload size={25}/><strong>{sourceCount?`已添加 ${sourceCount} 份资料，继续添加`:'添加需求文档'}</strong><span>DOCX · PDF · Markdown · TXT · Excel</span></button>
    {!action&&<button className="primary" disabled={running||!sourceCount} onClick={()=>onGenerate('请分析当前需求资料。','review_requirement')}><Sparkles size={16}/>开始理解需求</button>}
   </div>}</div>
  </div>
 </section>;
}
