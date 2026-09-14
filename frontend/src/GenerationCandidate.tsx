import {useEffect,useState} from 'react';
import {FileText} from 'lucide-react';
import {api,errText} from './api';
import {Dialog,ErrorBox,TextValue} from './ui';
import {workflowStageLabel} from './WorkflowSummary';
import type {GenerationCandidateData,GenerationCandidatePart} from './types';

const pageSize=25;
const record=(value:unknown):Record<string,unknown>|null=>value!==null&&typeof value==='object'&&!Array.isArray(value)?value as Record<string,unknown>:null;
const count=(value:unknown,fallback=0)=>typeof value==='number'&&Number.isInteger(value)&&value>=0?value:fallback;

// Candidates are failed model outputs. Never pass them through normal artifact renderers.
export function GenerationCandidate({part}:{part:GenerationCandidatePart}){
 const [open,setOpen]=useState(false);
 const attempts=count(part.attempts);
 return <section className="generation-candidate" aria-label="已保留的生成内容">
  <div><strong>{typeof part.title==='string'?part.title:workflowStageLabel(part.stage)+'生成内容'}</strong><p className="generation-candidate-state">未通过校验 · 已自动修复 {attempts} 次</p></div>
  <button onClick={()=>setOpen(true)}><FileText size={16}/>查看已生成内容{typeof part.item_count==='number'?` · ${part.item_count} 条`:''}</button>
  {open&&<CandidateDialog part={part} onClose={()=>setOpen(false)}/>}
 </section>;
}

function CandidateDialog({part,onClose}:{part:GenerationCandidatePart;onClose:()=>void}){
 const [data,setData]=useState<GenerationCandidateData>();const [error,setError]=useState('');const [reload,setReload]=useState(0);
 const path='/runs/'+encodeURIComponent(part.run_id)+'/candidates/'+encodeURIComponent(part.candidate_id);
 useEffect(()=>{let alive=true;setError('');setData(undefined);api<GenerationCandidateData>(path).then(value=>{if(alive)setData(value);}).catch(e=>{if(alive)setError(errText(e));});return()=>{alive=false;};},[path,reload]);
 const items=Array.isArray(data?.items)?data.items:[];
 const issues=Array.isArray(data?.issues)?data.issues:Array.isArray(part.issues)?part.issues:[];
 const attempts=count(data?.attempts,count(part.attempts));
 const requests=count(data?.request_count,count(part.request_count,attempts+1));
 const history=Array.isArray(data?.history)?data.history:[];
 const completed=Array.isArray(data?.completed_batches)?data.completed_batches:[];
 const isCases=data?.kind==='cases'||['cases','generate_cases','case_generation'].includes(data?.stage??part.stage);
 const isScenarios=data?.kind==='scenarios'||['scenarios','generate_scenarios','scenario_generation'].includes(data?.stage??part.stage);
 const columns=isCases?[['id','编号'],['title','用例标题'],['scenario_id','场景'],['preconditions','前置条件'],['steps','步骤 / 预期结果'],['refs','依据']]:isScenarios?[['id','编号'],['title','场景标题'],['description','说明'],['requirement_ids','需求'],['refs','依据']]:[['id','编号'],['title','需求标题'],['description','说明'],['refs','依据']];
 const title=typeof part.title==='string'?part.title:'已保留的生成内容';
 return <Dialog title={title} wide onClose={onClose}>
  <div className="generation-candidate-view">
   <div className="generation-candidate-notice"><strong>未通过校验 · 已自动修复 {attempts} 次</strong><p>首次请求 + {attempts} 次自动修复，共 {requests} 次模型请求。</p><p>本步骤仍有校验问题，未自动推进下一阶段。已通过的分批结果与待修复内容分开展示，可以查看后在对话中提出修改。</p></div>
   <ErrorBox message={error}/>{error&&<button onClick={()=>setReload(value=>value+1)}>重新读取</button>}
   {!data&&!error&&<p role="status">正在读取已保留的内容…</p>}
   {data&&<>
    {typeof data.selected_attempt==='number'&&<p className="muted small-text">当前展示{data.selected_attempt<=1?'首次生成':`第 ${data.selected_attempt-1} 次自动修复`}保留的内容；其余轮次可在生成记录中查看。</p>}
    {issues.length>0&&<section className="generation-candidate-issues" aria-label="未通过的校验"><h3>仍需处理的问题</h3><ul>{issues.map((issue,index)=>{const detail=record(issue);const missing=Array.isArray(detail?.missing_input_ids)?detail.missing_input_ids:[];return <li key={index}><TextValue value={detail?.message??issue}/>{missing.length>0&&<p className="small-text">缺失输入：{missing.map(id=>typeof id==='string'?id:JSON.stringify(id)).join('、')}</p>}{detail?.category!==undefined&&<details><summary>校验类型</summary><TextValue value={detail.category}/></details>}</li>;})}</ul></section>}
    <h3>当前待修复内容</h3>
    <CandidateItems items={items} columns={columns} label="未通过校验的生成内容"/>
    {data.report!==undefined&&<RawValue summary="查看生成报告" value={data.report}/>}
    <RawValue summary="查看对应原始返回" value={data.raw_result??{items:data.items,report:data.report}}/>
    {completed.length>0&&<section className="generation-candidate-completed" aria-label="已通过校验的分批结果"><h3>已通过校验的分批结果 · {completed.length} 批</h3><p className="muted small-text">这些批次已保留，本步骤尚未全部完成。以下内容只读展示，不会作为完整成果继续流转。</p>{completed.map((batch,index)=>{const value=record(batch);const rows=Array.isArray(value?.items)?value.items:[];return <section key={index} aria-label={`已通过的第 ${index+1} 批`}><h4>第 {index+1} 批 · 已通过校验</h4><CandidateItems items={rows} columns={columns} label={`已通过校验的第 ${index+1} 批内容`}/>{value?.report!==undefined&&<RawValue summary="查看本批报告" value={value.report}/>}<RawValue summary="查看本批完整返回" value={batch}/></section>;})}</section>}
    {history.length>0&&<details className="generation-candidate-history"><summary>查看各轮生成记录 · {history.length} 次</summary>{history.map((entry,index)=>{const value=record(entry);const attempt=count(value?.attempt,index+1);return <details key={index}><summary>{attempt<=1?'首次生成':`自动修复 ${attempt-1}`} · {typeof value?.call_id==='string'?value.call_id:'调用编号未记录'}</summary>{value?.validation_error!==undefined&&<p><TextValue value={value.validation_error}/></p>}<RawValue summary="查看该轮完整返回" value={value?.result??entry}/></details>;})}</details>}
    <a className="text-accent generation-candidate-download" href={'/api'+path+'/download'} download>下载生成记录 JSON</a>
   </>}
  </div>
 </Dialog>;
}

function CandidateItems({items,columns,label}:{items:unknown[];columns:string[][];label:string}){
 const [page,setPage]=useState(0);
 if(!items.length)return <p>未能整理为表格条目，可以查看下方原始返回。</p>;
 return <>
  <p className="muted small-text">已保留 {items.length} 条 · 当前显示 {page*pageSize+1}–{Math.min((page+1)*pageSize,items.length)} 条</p>
  <div className="table-scroll"><table className="generation-candidate-table" aria-label={label}><thead><tr>{columns.map(([field,title])=><th scope="col" key={field}>{title}</th>)}<th scope="col">完整条目</th></tr></thead><tbody>{items.slice(page*pageSize,(page+1)*pageSize).map((item,index)=>{const row=record(item);return <tr key={page*pageSize+index}>{row?<>{columns.map(([field])=><td key={field}>{field==='steps'?<CandidateSteps value={row[field]}/>:<TextValue value={row[field]}/>}</td>)}<td><RawValue summary="查看全部字段" value={item}/></td></>:<td colSpan={columns.length+1}><span className="generation-candidate-invalid">格式待修复</span><TextValue value={item}/></td>}</tr>;})}</tbody></table></div>
  {items.length>pageSize&&<nav className="generation-candidate-pages" aria-label={label+'分页'}><button disabled={page===0} onClick={()=>setPage(value=>value-1)}>上一页</button><span>{page+1} / {Math.ceil(items.length/pageSize)}</span><button disabled={(page+1)*pageSize>=items.length} onClick={()=>setPage(value=>value+1)}>下一页</button></nav>}
 </>;
}

function CandidateSteps({value}:{value:unknown}){
 if(!Array.isArray(value))return <TextValue value={value}/>;
 if(!value.length)return <span className="muted">未生成步骤</span>;
 return <ol className="generation-candidate-steps">{value.map((step,index)=>{const row=record(step);return <li key={index}>{row?<><div><strong>步骤</strong><TextValue value={row.action}/></div><div><strong>预期</strong><TextValue value={row.expected}/></div></>:<TextValue value={step}/>}</li>;})}</ol>;
}

function RawValue({summary,value}:{summary:string;value:unknown}){
 const [open,setOpen]=useState(false);
 return <details className="generation-candidate-raw" onToggle={event=>setOpen(event.currentTarget.open)}><summary>{summary}</summary>{open&&<pre><TextValue value={value}/></pre>}</details>;
}
