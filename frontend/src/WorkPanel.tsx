import {useEffect,useRef,useState} from 'react';
import {Check,ListChecks} from 'lucide-react';
import {api,errText} from './api';
import {ArtifactCard} from './ArtifactCard';
import {EvidenceRefs} from './EvidenceRefs';
import {ErrorBox,Spinner} from './ui';
import type {Artifact,WorkItem,WorkPage,WorkProgress} from './types';

type ArtifactActions={onTarget?:(artifact:Artifact,ids:string[])=>void;onChanged?:()=>void};
const noop=()=>{};

function WorkPreview({ids,refreshKey,partial,onTarget,onChanged}:ArtifactActions & {ids:string[];refreshKey:string;partial:boolean}){
 const [open,setOpen]=useState(false);
 if(!ids.length)return null;
 return <details className="work-previews" onToggle={event=>setOpen(event.currentTarget.open)}>
  <summary>工作草稿 · {ids.length} 份</summary>
  {open&&<><p className="muted small-text">{partial?'已保存的阶段结果，尚未完成全部分析与评审；最终结果会继续更新。':'这些是处理过程中的只读草稿；完整结果请查看聊天中的最终成果。'}</p>{ids.map(id=><ArtifactCard key={id} id={id} refreshKey={refreshKey} onTarget={onTarget??noop} onChanged={onChanged??noop}/>)}</>}
 </details>;
}

export function WorkPanel({runId,runStatus,work,previewIds=[],onTarget,onChanged}:ArtifactActions & {runId:string;runStatus:string;work?:WorkProgress;previewIds?:string[]}){
 const [inventoryOpen,setInventoryOpen]=useState(false);
 const [items,setItems]=useState<WorkItem[]>([]);const [nextCursor,setNextCursor]=useState<number|null>(null);
 const [loading,setLoading]=useState(false);const [error,setError]=useState('');
 const requestScope=useRef(0);const inventoryLoaded=useRef(false);
 async function loadPage(cursor:number,scope:number){
  setLoading(true);setError('');
  try{
   const page=await api<WorkPage>(`/runs/${encodeURIComponent(runId)}/work?cursor=${cursor}&limit=20`);
   if(requestScope.current!==scope)return;
   setItems(previous=>cursor===0?page.items:[...new Map([...previous,...page.items].map(item=>[item.id,item])).values()]);
   setNextCursor(page.next_cursor);inventoryLoaded.current=true;
  }catch(e){if(requestScope.current===scope)setError(errText(e));}
  finally{if(requestScope.current===scope)setLoading(false);}
 }
 useEffect(()=>{
  const scope=++requestScope.current;
  if(inventoryOpen){inventoryLoaded.current=false;setItems([]);setNextCursor(null);void loadPage(0,scope);}
  return()=>{requestScope.current++;};
 },[runId,inventoryOpen]);
 const current=work?.current;const active=runStatus==='running'&&current?.status==='running';
 const failed=work?.items.filter(item=>item.status==='failed')??[];
 const liveItems=new Map(work?.items.map(item=>[item.id,item]));
 const visibleItems=items.map(item=>liveItems.get(item.id)??item);
 const partial=runStatus!=='completed';
 const refreshKey=`${runStatus}:${work?.completed}:${work?.total}:${current?.id}:${current?.attempt}`;
 const status=(item:WorkItem)=>item.status==='superseded'?'已被后续规则替代':item.status==='completed'?'已完成':item.status==='failed'?'处理失败':item.status==='running'?(runStatus==='running'?(item.attempt>1?`重试中 · 第 ${item.attempt} 次`:'处理中'):runStatus==='failed'?'已中断':'已暂停'):'待处理';
 return <div className="work-panel" aria-label="逐项工作进度">
  {work&&<>
   <div className="agent-section-label"><ListChecks size={16}/><strong>工作进度</strong><small>已完成 {work.completed} / {work.total} 项</small></div>
   <progress aria-label="工作完成进度" value={work.completed} max={Math.max(work.total,1)}/>
   {current&&<div className="work-current"><p className="inline">{active&&<Spinner/>}<strong>{active?'正在处理':'当前工作'}：{current.title}</strong></p>{active&&current.attempt>1&&<p className="muted small-text">重试中 · 第 {current.attempt} 次</p>}<EvidenceRefs refs={current.refs}/></div>}
   {failed.map(item=><div className="work-failure" key={item.id}><strong>处理失败：{item.title}</strong><p className="error-text">{item.error??'此项未完成，可重试当前工作。'}</p><EvidenceRefs refs={item.refs}/></div>)}
   {runStatus==='failed'&&<p className="small-text muted">已完成的工作已保留，重试会从未完成的工作继续。</p>}
   <details className="work-inventory" onToggle={event=>setInventoryOpen(event.currentTarget.open)}>
    <summary>查看全部工作</summary>
    {inventoryOpen&&<><ol>{visibleItems.map(item=><li key={item.id} className={'work-item '+item.status}><div className="work-item-heading"><strong>{item.title}</strong><span className="inline">{item.status==='completed'&&<Check size={13}/>}<small>{status(item)}</small></span></div>{item.error&&<p className="error-text">{item.error}</p>}<EvidenceRefs refs={item.refs}/>{item.artifact_id&&<WorkPreview ids={[item.artifact_id]} refreshKey={refreshKey} partial={partial} onTarget={onTarget} onChanged={onChanged}/>}</li>)}</ol><ErrorBox message={error}/>{loading?<p className="inline muted small-text"><Spinner/>正在读取工作记录…</p>:error?<button onClick={()=>loadPage(inventoryLoaded.current?(nextCursor??0):0,requestScope.current)}>重新读取工作</button>:nextCursor!==null?<button onClick={()=>loadPage(nextCursor,requestScope.current)}>加载更多工作</button>:!visibleItems.length?<p className="muted small-text">还没有工作记录。</p>:null}</>}
   </details>
  </>}
  <WorkPreview ids={[...new Set(previewIds)]} refreshKey={refreshKey} partial={partial} onTarget={onTarget} onChanged={onChanged}/>
 </div>;
}
