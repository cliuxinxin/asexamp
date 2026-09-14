import {useEffect,useRef,useState} from 'react';
import {api,errText} from './api';
import {TextValue} from './ui';
import type {Artifact,Json} from './types';

export type ScopeFilter='all'|'missing'|'unlinked'|'independent'|'pending'|'issues'|'changed'|'excluded';
export type ParentRow={artifact_id:string;revision:number;item:Json;evidence?:Json[]};
export type RowContext={scenario?:ParentRow;requirements:ParentRow[];missing:boolean;independent?:string;pending:Json[];stale:boolean;excluded?:string;assumption:boolean;issues:Json[];changed:boolean;needsReview:boolean;downstream:string[]};

// A card owns one request per immutable version. Row expansion and selection never fetch.
export function useArtifactLineage(artifact:Artifact|undefined,enabled:boolean){
 const [result,setResult]=useState<{key:string;workspace?:Json;error?:string}>();
 const requests=useRef(new Map<string,Promise<Json>>());
 const id=artifact?.id,revision=artifact?.revision;
 const key=id+':'+revision;
 useEffect(()=>{
  if(!enabled||!id||revision===undefined)return;
  let alive=true;let request=requests.current.get(key);
  if(!request){request=api<Json>('/artifacts/'+encodeURIComponent(id)+'/workspace?revision='+revision);requests.current.set(key,request);}
  request.then(workspace=>{if(alive)setResult({key,workspace});}).catch(error=>{if(alive)setResult({key,error:errText(error)});});
  return()=>{alive=false;};
 },[id,revision,key,enabled]);
 return {workspace:result?.key===key?result.workspace:undefined,error:result?.key===key?result.error:undefined};
}

function issueTargets(issue:Json,id:string){return issue.id===id||issue.case_id===id||issue.item_id===id||(Array.isArray(issue.case_ids)&&issue.case_ids.includes(id));}
function reports(report:Json|undefined):Json[]{return report?[report,...(Array.isArray(report.review_reports)?report.review_reports:[]).flatMap(reports),...(Array.isArray(report.segments)?report.segments:[]).flatMap(reports)]:[];}
function exactParent(parent:Json|undefined,id:string|undefined,revision:number|undefined,itemId:string):ParentRow|undefined{
 if(!parent||parent.id!==id||parent.revision!==revision)return;
 const item=parent.items?.find((row:Json)=>row.id===itemId);
 return item?{artifact_id:parent.id,revision:parent.revision,item,evidence:[]}:undefined;
}

export function rowContext(artifact:Artifact,item:Json,workspace?:Json):RowContext{
 const saved=workspace?.lineage_rows?.find((row:Json)=>row.item_id===item.id);
 const independent=saved?.status==='independent'?(saved.reason||item._independent_origin?.reason||'用户指定独立条目'):undefined;
 let scenario=saved?.scenario??undefined;
 let requirements:ParentRow[]=saved?.requirements??[];
 // Older workspace responses can only supply content when identity AND revision match.
 if(!saved&&workspace?.parents){
  const source=artifact.report?.lineage??{};
  scenario=artifact.type==='cases'?exactParent(workspace.parents.scenarios,source.scenario_artifact_id,source.scenario_revisions?.[item.scenario_id]??source.scenario_revision,item.scenario_id):undefined;
  const parentSource=artifact.type==='scenarios'?source:workspace.parents.scenarios?.report?.lineage??{};
  requirements=((artifact.type==='scenarios'?item:scenario?.item)?.requirement_ids??[]).flatMap((id:string)=>{
   const parent=exactParent(workspace.parents.analysis,parentSource.analysis_artifact_id,parentSource.analysis_revisions?.[id]??parentSource.analysis_revision,id);return parent?[parent]:[];
  });
 }
 const coverageRow=workspace?.coverage?.[artifact.type==='analysis'?'requirements':'scenarios']?.find((row:Json)=>row.id===item.id);
 const allReports=reports(artifact.report);
 const exclusion=allReports.flatMap(report=>Array.isArray(report.scenario_exclusions)?report.scenario_exclusions:[]).find((entry:Json)=>entry.scenario_id===(artifact.type==='cases'?item.scenario_id:item.id));
 const excluded=exclusion?.reason??(item.status==='excluded'||item.out_of_scope===true?item.exclusion_reason??item.reason??'已明确排除':undefined);
 const pending=(workspace?.upstream_discrepancies??[]).filter((entry:Json)=>entry.status==='pending'&&entry.item_ids?.includes(item.id));
 const stale=typeof saved?.stale==='boolean'?(saved.status==='missing_parent'?!!(saved.scenario?.stale||saved.requirements?.some((parent:Json)=>parent.stale)):saved.stale):artifact.type==='cases'?workspace?.stale?.changed_scenario_ids?.includes(item.scenario_id)||requirements.some(parent=>workspace?.stale?.changed_requirement_ids?.includes(parent.item.id)):artifact.type==='scenarios'?requirements.some(parent=>workspace?.stale?.changed_requirement_ids?.includes(parent.item.id)):false;
 const issues=allReports.flatMap(report=>Array.isArray(report.issues)?report.issues:[]).filter((issue:unknown):issue is Json=>!!issue&&typeof issue==='object'&&issueTargets(issue as Json,item.id));
 const diff=workspace?.revision_diff??{};
 const changed=[...(diff.added??[]),...(diff.updated??[])].includes(item.id);
 const needsReview=workspace?.review?.changed_item_ids?.includes(item.id)??false;
 return {scenario,requirements,independent,missing:!independent&&(saved?.status==='missing_parent'||(artifact.type==='cases'?!scenario:artifact.type==='scenarios'?requirements.length===0:!coverageRow?.scenario_ids?.length)),pending,stale:!!stale,excluded,assumption:item.assumption===true||item.status==='assumption',issues,changed,needsReview,downstream:coverageRow?.[artifact.type==='analysis'?'scenario_ids':'case_ids']??[]};
}

export function matchesScope(context:RowContext,filter:ScopeFilter,kind:string){
 if(filter==='missing')return !context.excluded&&(kind==='scenarios'?context.downstream.length===0:context.missing);
 if(filter==='unlinked')return !context.excluded&&context.missing;
 if(filter==='independent')return !!context.independent;
 if(filter==='pending')return !!context.pending.length||context.stale;
 if(filter==='issues')return !!context.issues.length||context.needsReview;
 if(filter==='changed')return context.changed;
 if(filter==='excluded')return !!context.excluded;
 return true;
}

export function ArtifactScope({artifact,workspace,filter,onFilter,contexts}:{artifact:Artifact;workspace:Json;filter:ScopeFilter;onFilter:(filter:ScopeFilter)=>void;contexts:Map<string,RowContext>}){
 const rows=[...contexts.values()];const excluded=rows.filter(row=>row.excluded).length;
 const missing=rows.filter(row=>matchesScope(row,'missing',artifact.type)).length;
 const unlinked=rows.filter(row=>matchesScope(row,'unlinked',artifact.type)).length;
 const pending=rows.filter(row=>matchesScope(row,'pending',artifact.type)).length;
 const issues=rows.filter(row=>matchesScope(row,'issues',artifact.type)).length;
 const changed=rows.filter(row=>row.changed).length;
 const independent=rows.filter(row=>row.independent).length;
 const scopeName=artifact.type==='analysis'?'需求':artifact.type==='scenarios'?'场景':'用例';
 const missingName=artifact.type==='analysis'?'待补场景':artifact.type==='scenarios'?'待补用例':'未关联';
 const caseBranch=artifact.type==='scenarios'?workspace.related_artifacts?.find((item:Json)=>item.id===workspace.selected_case_artifact_id):undefined;
 return <div className="artifact-inline-scope" aria-label="当前成果范围">
  <span className="muted small-text">本轮范围内 {rows.length-excluded} 条{scopeName}</span>
  <div className="artifact-scope-filters" role="group" aria-label="关联与评审筛选">
   {([['all',`全部 ${rows.length}`],['missing',`${missingName} ${missing}`],...(artifact.type==='scenarios'&&unlinked?[['unlinked',`未关联需求 ${unlinked}`]]:[]),...(independent?[['independent',`N/A ${independent}`]]:[]),...(pending?[['pending',`待同步 ${pending}`]]:[]),...(artifact.type==='cases'?[['issues',`有问题 ${issues}`],['changed',`有变化 ${changed}`]]:[]),...(excluded?[['excluded',`已排除 ${excluded}`]]:[])] as [ScopeFilter,string][]).map(([value,label])=><button key={value} className={filter===value?'selected':''} aria-pressed={filter===value} onClick={()=>onFilter(value)}>{label}</button>)}
  </div>
  {caseBranch&&<small className="muted">关联用例分支：{caseBranch.title} · v{caseBranch.revision}</small>}
 </div>;
}

function ParentContent({parent,label}:{parent:ParentRow;label:string}){
 const item=parent.item;
 return <details className="artifact-parent"><summary><span className="artifact-parent-id">{item.id}</span><span>{item.title??'未命名'+label}</span></summary><div className="artifact-parent-content">
  <small>{label} · v{parent.revision} · {parent.artifact_id}</small>
  {item.description&&<p><TextValue value={item.description}/></p>}{item.content&&<p><TextValue value={item.content}/></p>}{item.rule&&<p><TextValue value={item.rule}/></p>}
  {(parent.evidence??[]).map((evidence,index)=><blockquote key={evidence.id??index}><small>{evidence.id??evidence.ref}{evidence.evidence_basis==='current_unversioned'?' · 当前来源，历史来源版本未记录':''}</small><p><TextValue value={evidence.text??evidence.content}/></p></blockquote>)}
  {!(parent.evidence?.length)&&item.refs?.length>0&&<small>依据：{item.refs.join(' · ')}</small>}
  <details><summary>{label}全部字段</summary><pre>{JSON.stringify(item,null,2)}</pre></details>
 </div></details>;
}

export function ArtifactLineage({context,kind,loading,error}:{context:RowContext;kind:string;loading:boolean;error?:string}){
 if(loading)return <span className="muted small-text">{error?'关联暂不可读取':'读取关联…'}</span>;
 return <div className="artifact-row-lineage">
  {kind==='cases'&&context.scenario&&<ParentContent parent={context.scenario} label="主场景"/>}
  {context.requirements.map(parent=><ParentContent key={parent.artifact_id+':'+parent.revision+':'+parent.item.id} parent={parent} label="需求"/>)}
  {context.missing&&<span className="artifact-row-status">{context.scenario||context.requirements.length?'关联不完整':'尚未关联'}</span>}
  {context.independent&&<details className="artifact-row-status independent"><summary>{kind==='cases'&&!context.scenario?'场景 / 需求：N/A':'需求：N/A'}</summary><p>{context.independent}</p></details>}
  {!context.missing&&!context.independent&&kind==='cases'&&!context.requirements.length&&<span className="artifact-row-status">需求尚未关联</span>}
  <RowStatus context={context}/>
  {kind==='scenarios'&&<small className="muted">{context.downstream.length?`关联用例：${context.downstream.join('、')}`:context.excluded?'本轮已排除':'尚无下游用例'}</small>}
 </div>;
}

export function RowStatus({context}:{context:RowContext}){return <>
 {context.excluded&&<details className="artifact-row-status"><summary>明确排除</summary><p>{context.excluded}</p></details>}
 {context.assumption&&<span className="artifact-row-status">基于假设</span>}
 {!!context.pending.length&&<details className="artifact-row-status pending"><summary>上游待同步</summary>{context.pending.map((entry,index)=><div key={index}><p>{entry.reason}</p>{entry.refs?.length>0&&<small>补充依据：{entry.refs.join(' · ')}</small>}</div>)}</details>}
 {context.stale&&<span className="artifact-row-status pending">父级已更新 · 待同步</span>}
 </>;}

export function CaseReview({context,hasReview}:{context:RowContext;hasReview:boolean}){
 return <div className="artifact-row-review">{context.needsReview&&<span className="artifact-row-status pending">修改后待复查</span>}{context.changed&&<span className="artifact-row-status">本版有变化</span>}{context.issues.length?context.issues.map((issue,index)=><div key={index}><p><TextValue value={issue.description??issue.message??issue.title??issue.summary??issue}/></p>{issue.reason&&<small><TextValue value={issue.reason}/></small>}{(issue.fields??issue.changed_fields)?.length>0&&<small>修改范围：<TextValue value={Array.isArray(issue.fields??issue.changed_fields)?(issue.fields??issue.changed_fields).join('、'):issue.fields??issue.changed_fields}/></small>}{issue.refs?.length>0&&<small>依据：<TextValue value={Array.isArray(issue.refs)?issue.refs.join(' · '):issue.refs}/></small>}</div>):<span className="muted small-text">{hasReview?'未记录逐条问题':'尚未评审'}</span>}</div>;
}
