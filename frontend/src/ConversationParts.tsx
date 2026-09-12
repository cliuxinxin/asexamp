import {useEffect,useState} from 'react';
import {ArtifactCard} from './ArtifactCard';
import {ChangePreview} from './ArtifactActions';
import {ChatEstimate} from './ChatEstimate';
import {WorkspaceCoverage} from './WorkspaceCoverage';
import {ClarificationDraftEditor} from './ClarificationDraftEditor';
import {useConversationCommand} from './conversation';
import {api,errText} from './api';
import {ErrorBox} from './ui';
import type {Artifact,Json,TurnResponse} from './types';

export function ConversationParts({response,refreshKey,onTarget,onOpen,onChanged,compact=false}:{response:TurnResponse;refreshKey?:string;onTarget:(artifact:Artifact,ids:string[])=>void;onOpen?:(artifact:Artifact)=>void;onChanged:()=>void;compact?:boolean}){
 return <div className="conversation-parts">{(response.parts??[]).map((part,index)=>{
  const key=response.id+':'+index;
  const value=part as Json;
  switch(value.type){
   case 'answer':return <section key={key} className="turn-answer"><p className="preserve">{value.text}</p>{!!value.refs?.length&&<p className="muted small-text">依据：{value.refs.join(' · ')}</p>}</section>;
   case 'estimate':return compact?<details key={key} className="receipt-details"><summary>查看用例数量估算</summary><ChatEstimate estimate={value.data as any}/></details>:<ChatEstimate key={key} estimate={value.data as any}/>;
   case 'artifact':return <ArtifactCard key={key} compact={compact} id={value.artifact_id} revision={value.revision} readOnly refreshKey={refreshKey} onTarget={onTarget} onOpen={onOpen} onChanged={onChanged}/>;
   case 'case_details':return <ArtifactCard key={key} compact={compact} id={value.artifact_id} snapshot={{id:value.artifact_id,type:'cases',title:value.title??'用例步骤与预期',revision:value.revision,items:value.items}} readOnly initialDetailsOpen onTarget={onTarget} onOpen={onOpen} onChanged={onChanged}/>;
   case 'diff':return compact?<details key={key} className="receipt-details"><summary>修改预览 · 在工作区应用或放弃</summary><ConversationDiff proposalId={value.proposal_id} changes={value.changes} onChanged={onChanged} readOnly/></details>:<ConversationDiff key={key} proposalId={value.proposal_id} changes={value.changes} onChanged={onChanged}/>;
   case 'coverage':return <WorkspaceCoverage key={key} artifactId="" revision={0} initialData={value.data} initialOpen={!compact}/>;
   case 'source_impact':return compact?<details key={key} className="receipt-details"><summary>资料影响 · {value.data.summary||'查看分析结果'}</summary><SourceImpact data={value.data}/></details>:<SourceImpact key={key} data={value.data}/>;
   case 'files':return <div key={key} className="turn-files" aria-label="导出文件">{value.files.map((file:Json,i:number)=>/^\/(?!\/)|^https?:\/\//.test(file.url)?<a key={i} className="text-accent" href={file.url} download={file.name}>{file.name}</a>:<span key={i}>{file.name}（下载地址不可用）</span>)}</div>;
   case 'clarification_draft':return <ClarificationDraftEditor key={key} runId={value.draft.run_id} initialDraft={value.draft} refreshKey={refreshKey} onChanged={onChanged} summaryOnly/>;
  }
 })}{response.status==='deferred'&&<p role="status" className="muted small-text">操作已登记，将在安全步骤边界处理。</p>}{(response.pending??[]).length>0&&<div className="turn-pending" aria-label="待确认事项">{response.pending.map((item,index)=><div key={item.id??index}><p>{item.message??item.question??item.title??'请明确本次操作的对象。'}</p>{Array.isArray(item.candidates)&&item.candidates.length>0&&<ol>{item.candidates.map((candidate:Json,candidateIndex:number)=><li key={candidate.id??candidateIndex}>{candidate.title??candidate.name??'未命名成果'} · {candidate.id}{candidate.revision!==undefined?` · v${candidate.revision}`:''}</li>)}</ol>}</div>)}</div>}</div>;
}

function SourceImpact({data}:{data:Json}){
 const requirements=Array.isArray(data.requirement_ids)?data.requirement_ids.map(String):[];
 const descendants=Array.isArray(data.downstream_candidates)?data.downstream_candidates:[];
 const coverage=data.coverage&&typeof data.coverage==='object'?data.coverage as Json:{};
 const affectedDescendants=descendants.reduce((total,row)=>total+(Array.isArray(row.candidate_item_ids)?row.candidate_item_ids.length:0),0);
 const broad=Boolean(data.global_impact),uncertain=Boolean(data.uncertain),partial=Boolean(coverage.partial);
 const additions=Array.isArray(data.new_requirements)?data.new_requirements:[];
 const status=uncertain?'影响范围尚不确定，需要人工确认':broad?'检测到全局影响，需要按全部需求处理':additions.length?'发现需要纳入的新需求':requirements.length?'已定位可能受影响的需求':'未定位到受影响的现有需求';
 const typeName=(value:unknown)=>value==='analysis'?'需求理解':value==='scenarios'?'测试场景':value==='cases'?'测试用例':'关联成果';
 return <section className={'source-impact '+(broad||uncertain||partial?'source-impact-attention':'')} aria-label="新增资料影响分析">
  <div className="source-impact-head"><div><strong>新增资料影响分析</strong><p className="muted small-text">只读检查 · 需求理解 v{data.artifact_revision??'—'} · 未修改成果</p></div><span>{status}</span></div>
  {data.summary&&<p className="preserve source-impact-summary">{String(data.summary)}</p>}
  <div className="source-impact-metrics">
   <span><strong>{requirements.length}</strong>受影响需求</span>
   {additions.length>0&&<span><strong>{additions.length}</strong>新增需求</span>}
   <span><strong>{affectedDescendants}</strong>候选下游条目</span>
   <span><strong>{coverage.checked_pairs??0} / {coverage.expected_pairs??0}</strong>需求与资料组合</span>
  </div>
  <div className="source-impact-requirements"><strong>受影响需求</strong>{requirements.length?<ul>{requirements.map(id=><li key={id}>{id}</li>)}</ul>:<p className="muted small-text">当前检查没有定位到现有需求。</p>}</div>
  {additions.length>0&&<div className="source-impact-requirements"><strong>待纳入的新需求</strong><ul>{additions.map((row:Json,index:number)=><li key={index}><strong>{row.title}</strong>{row.description&&<p>{row.description}</p>}</li>)}</ul></div>}
  {descendants.length>0&&<div className="table-scroll"><table aria-label="可能受影响的关联成果"><thead><tr><th>成果</th><th>当前版本</th><th>可能受影响的条目</th><th>判断</th></tr></thead><tbody>{descendants.map((row,index)=>{
   const ids=Array.isArray(row.candidate_item_ids)?row.candidate_item_ids.map(String):[];
   return <tr key={String(row.artifact_id??index)}><td>{typeName(row.type)}</td><td>v{row.revision??'—'}</td><td>{ids.join('、')||'暂无已定位条目'}</td><td>{row.status==='potential_impact'?'需要核对':'未标记影响'}{row.partial?' · 范围不完整':''}</td></tr>;
  })}</tbody></table></div>}
  <p className={partial?'coverage-gap':'muted small-text'}>{partial?'检查范围不完整，请缩小资料或需求后重试。':`检查范围完整：已覆盖 ${coverage.requirement_count??0} 条需求和 ${coverage.evidence_count??0} 个资料片段。`}</p>
  {(broad||uncertain)&&<p className="coverage-gap">保存更新前请明确采用全部需求范围；本次结果本身不会写入正文或修改成果。</p>}
 </section>;
}

function ConversationDiff({proposalId,changes,onChanged,readOnly=false}:{proposalId:string;changes:Json[];onChanged:()=>void;readOnly?:boolean}){
 const [originals,setOriginals]=useState<Record<string,Artifact>>({});const [error,setError]=useState('');const [working,setWorking]=useState(false);const [resolution,setResolution]=useState('');const command=useConversationCommand();
 useEffect(()=>{let alive=true;Promise.all(changes.filter(change=>!change.before_items).map(async change=>[change.artifact_id,await api<Artifact>('/artifacts/'+encodeURIComponent(change.artifact_id)+'/revisions/'+change.expected_revision)] as const)).then(entries=>{if(alive)setOriginals(Object.fromEntries(entries));}).catch(e=>{if(alive)setError(errText(e));});return()=>{alive=false;};},[changes]);
 async function resolve(name:string){setWorking(true);setError('');try{const result=await command({name,arguments:{proposal_id:proposalId,...(changes[0]?{artifact_id:changes[0].artifact_id,expected_revision:changes[0].expected_revision}:{})}},name==='artifact.apply'?'应用这项预览修改':'取消这项预览修改');if(result.status==='succeeded'){setResolution(result.message);onChanged();}else setError(result.message);}catch(e){setError(errText(e));}finally{setWorking(false);}}
 return <section className="action-preview" aria-label="变更预览">{changes.map((change,index)=><ChangePreview key={index} change={change} before={originals[change.artifact_id]}/>)}<ErrorBox message={error}/>{resolution?<p role="status">{resolution}</p>:!readOnly&&proposalId&&<div className="actions"><button className="primary" disabled={working} onClick={()=>void resolve('artifact.apply')}>应用此修改</button><button disabled={working} onClick={()=>void resolve('artifact.discard')}>取消此修改</button></div>}</section>;
}
