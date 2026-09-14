import {useContext,useEffect,useRef,useState} from 'react';
import {MessageSquare,Sparkles,RefreshCw,Calculator} from 'lucide-react';
import {api,errText} from './api';
import {useConversationCommand} from './conversation';
import {Dialog,ErrorBox,Spinner,TextValue} from './ui';
import {ArtifactWorkspaceContext} from './workspaceContext';
import type {Artifact,Json} from './types';

type Action='estimate'|'explain'|'sync';
const names:Record<Action,string>={estimate:'估算用例数量',explain:'解释或总结用例',sync:'同步关联用例'};

export function ArtifactActions({artifact,selected,onChanged,readOnly=false}:{readOnly?:boolean;artifact:Artifact;selected:string[];onChanged:()=>void}){
 const command=useConversationCommand();
 const artifactWorkspace=useContext(ArtifactWorkspaceContext);
 const [action,setAction]=useState<Action>();const [instruction,setInstruction]=useState('');const [scope,setScope]=useState<string[]>([]);
 const [workspace,setWorkspace]=useState<Json>();const [showSources,setShowSources]=useState(false);const [sourceIds,setSourceIds]=useState<string[]>([]);const [targets,setTargets]=useState<string[]>([]);const [proposal,setProposal]=useState<Json>();
 const [busy,setBusy]=useState(false);const [error,setError]=useState('');const seq=useRef(0);
 useEffect(()=>()=>{seq.current++;},[]);
 useEffect(()=>{setProposal(undefined);},[artifact.id,artifact.revision]);
 function close(){seq.current++;setAction(undefined);setBusy(false);}
 async function begin(next:Action){const token=++seq.current;setAction(next);setScope([...selected]);setInstruction(next==='estimate'?'仅根据当前场景估算用例数量区间，说明拆分理由和待确认假设，不生成用例。':next==='explain'?(selected.length?'解释选中用例的测试目的、前置条件、操作和预期结果。':'总结这些用例的业务内容、验证重点和已有覆盖，不修改用例。'):next==='sync'?'根据当前场景的最新版本，更新所选场景对应的用例，保留其他用例及已有人工填写内容。':'');setProposal(undefined);setError('');setWorkspace(undefined);setTargets([]);setShowSources(false);setSourceIds([]);setBusy(next==='sync');if(next!=='sync')return;try{const value=await api<Json>('/artifacts/'+artifact.id+'/workspace');if(token===seq.current){setWorkspace(value);const linked=(value.related_artifacts??[]).filter((item:Json)=>item.type==='cases'&&item.linked);setTargets(linked.length===1?[linked[0].id]:[]);}}catch(e){if(token===seq.current)setError(errText(e));}finally{if(token===seq.current)setBusy(false);}}
 async function loadSources(){setShowSources(true);if(workspace)return;const token=++seq.current;setBusy(true);setError('');try{const value=await api<Json>('/artifacts/'+artifact.id+'/workspace');if(token===seq.current)setWorkspace(value);}catch(e){if(token===seq.current)setError(errText(e));}finally{if(token===seq.current)setBusy(false);}}
 function changeInstruction(value:string){setInstruction(value);setProposal(undefined);}
 async function preview(){
  if(!action)return;const token=++seq.current;setBusy(true);setError('');setProposal(undefined);
  try{
   const name=action==='estimate'?'artifact.estimate':action==='explain'?'artifact.analyze':'artifact.sync_related';
   const result=await command({name,arguments:{instruction,...(scope.length?{selected_ids:scope}:{}),...(action!=='estimate'&&sourceIds.length?{source_ids:sourceIds}:{}),...(action==='sync'?{related_artifact_ids:targets,preview:true}:{})}},instruction,artifact);
   const difference=result.parts?.find(part=>part.type==='artifact_proposal');const answer=result.parts?.find(part=>part.type==='answer');const estimate=result.parts?.find(part=>part.type==='estimate');
   if(difference?.type==='artifact_proposal'){if(token===seq.current){setAction(undefined);onChanged();artifactWorkspace.open?.({artifactId:difference.artifact_id,proposalId:difference.proposal_id});}return;}
   if(token===seq.current)setProposal({summary:result.message,answer:answer?.type==='answer'?answer.text:undefined,refs:answer?.type==='answer'?answer.refs:[],estimate:estimate?.type==='estimate'?estimate.data:undefined});
  }catch(e){if(token===seq.current)setError(errText(e));}finally{if(token===seq.current)setBusy(false);}
 }
 return <><div className="artifact-action-buttons">{!readOnly&&<button onClick={()=>artifactWorkspace.open?.({artifactId:artifact.id})}><Sparkles size={15}/>{selected.length?`AI 微调选中 ${selected.length} 条`:'AI 微调'}</button>}{artifact.type==='cases'&&<button onClick={()=>void begin('explain')}><MessageSquare size={15}/>{selected.length?'解释选中用例':'总结用例'}</button>}{artifact.type==='scenarios'&&<><button onClick={()=>void begin('estimate')}><Calculator size={15}/>估算用例数量（不生成）</button>{!readOnly&&<button onClick={()=>void begin('sync')}><RefreshCw size={15}/>同步关联用例</button>}</>}</div>
 {action&&<Dialog title={names[action]+' · '+artifact.title} onClose={()=>{if(!busy)close();}} closeDisabled={busy} wide><p className="action-scope">基于 v{artifact.revision} · {scope.length?`选中 ${scope.length} 条：${scope.join('、')}`:`全部 ${artifact.items.length} 条`}</p>
 <label>本次要求<textarea aria-label="本次微调或分析要求" rows={3} value={instruction} disabled={busy} onChange={e=>changeInstruction(e.target.value)}/></label>
 {action==='sync'&&workspace&&<fieldset className="action-targets"><legend>要同步的用例成果</legend>{(workspace.related_artifacts??[]).filter((item:Json)=>item.type==='cases'&&item.linked).length>1&&<p className="muted small-text">此场景有多套关联用例，请选择本次要同步的成果。</p>}{(workspace.related_artifacts??[]).filter((item:Json)=>item.type==='cases').map((item:Json)=><label key={item.id}><input type="checkbox" checked={targets.includes(item.id)} disabled={busy} onChange={e=>{setTargets(old=>e.target.checked?[...old,item.id]:old.filter(id=>id!==item.id));setProposal(undefined);}}/>{item.title} · v{item.revision} · {item.linked?'已关联':'待选择关联'}</label>)}{!(workspace.related_artifacts??[]).some((item:Json)=>item.type==='cases')&&<p className="muted">当前没有可同步的用例成果。可先生成或导入用例，再选择关联目标。</p>}</fieldset>}
 {action!=='estimate'&&<div className="action-source-picker"><button disabled={busy} onClick={()=>void loadSources()}>添加本次补充资料</button>{showSources&&workspace&&<fieldset className="action-targets"><legend>本次额外参考的资料</legend><p className="muted small-text">勾选需要纳入本次操作的新资料。原成果的需求依据会继续使用。</p>{(workspace.sources??[]).filter((source:Json)=>source.role!=='example').map((source:Json)=><label key={source.id}><input type="checkbox" disabled={busy} checked={sourceIds.includes(source.id)} onChange={e=>{setSourceIds(old=>e.target.checked?[...old,source.id]:old.filter(id=>id!==source.id));setProposal(undefined);}}/>{source.name}</label>)}{!(workspace.sources??[]).some((source:Json)=>source.role!=='example')&&<p className="muted">暂无可添加资料。可先在会话中上传补充需求，再打开此操作。</p>}</fieldset>}</div>}
 <ErrorBox message={error}/>
 {proposal&&<section className="action-preview" aria-label="操作预览"><h3>{action==='estimate'?'估算结果':action==='explain'?'用例说明':'变更预览'}</h3><p className="preserve">{proposal.summary}</p>{proposal.answer&&<p className="preserve"><TextValue value={proposal.answer}/></p>}{proposal.refs?.length>0&&<p className="muted small-text">需求依据：{proposal.refs.join('、')}</p>}{proposal.estimate&&<><p><strong>预计 {proposal.estimate.min_count}–{proposal.estimate.max_count} 条用例</strong> · 仅估算，未生成用例</p><div className="table-scroll"><table><thead><tr><th>场景</th><th>数量区间</th><th>拆分理由</th><th>待确认假设</th></tr></thead><tbody>{(proposal.estimate.scenarios??[]).map((item:Json)=><tr key={item.scenario_id}><td>{item.scenario_id}</td><td>{item.min_count}–{item.max_count}</td><td><TextValue value={item.rationale}/></td><td>{(item.assumptions??[]).join('；')||'无额外假设'}</td></tr>)}</tbody></table></div></>}</section>}
 <div className="dialog-actions"><button disabled={busy||!instruction.trim()||(action==='sync'&&(!workspace||!targets.length))} onClick={()=>void preview()}>{busy?<Spinner/>:<Sparkles size={16}/>} {action==='estimate'?'开始估算':action==='explain'?'生成说明':proposal?'重新预览':'预览修改'}</button><button onClick={close} disabled={busy}>关闭</button></div></Dialog>}
 </>;
}
