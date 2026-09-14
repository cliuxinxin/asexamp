import {useEffect,useMemo,useRef,useState} from 'react';
import {ChevronDown,ChevronRight,RefreshCw,X} from 'lucide-react';
import {api,errText} from './api';
import {ErrorBox,Spinner} from './ui';

export type TraceNode={key:string;kind:'analysis'|'scenarios'|'cases';item_id:string;title:string;artifact_id:string;artifact_title:string;revision:number;chat_id:string;parent_keys:string[];statuses:string[];missing:boolean;stale:boolean;independent:boolean;direct:boolean;reason?:string;basis:{artifact_id:string|null;item_id:string;revision:number|null;current_revision:number|null;stale:boolean;missing:boolean}[]};
type TraceData={project_id:string;chat_id:string|null;summary:{requirements:number;scenarios:number;cases:number;missing:number;stale:number;independent:number;direct_cases:number};chats:{id:string;title:string;nodes:TraceNode[]}[];notes:string[]};
type Filter='all'|'missing'|'stale'|'independent';
const kindLabels={analysis:'需求',scenarios:'场景',cases:'用例'};
const statusLabels:Record<string,string>={linked:'已关联',independent:'独立 N/A',skipped_scenarios:'场景已跳过',missing_parent:'缺失上游',missing_scenarios:'待补场景',missing_cases:'待补用例',stale:'待同步'};
const PAGE_SIZE=200;

type DisplayRow={node:TraceNode;depth:number;path:string;hasChildren:boolean;chatTitle:string;firstInChat:boolean};
export function traceRows(data:TraceData|undefined,filter:Filter,collapsed:Set<string>,limit:number){
 const rows:DisplayRow[]=[];
 if(!data)return {rows,hasMore:false};
 for(const chat of data.chats){
  const index=new Map(chat.nodes.map(node=>[node.key,node]));
  const children=new Map<string,TraceNode[]>();
  for(const node of chat.nodes)for(const key of node.parent_keys){const list=children.get(key)??[];list.push(node);children.set(key,list);}
  const retained=new Set<string>();
  function retain(node:TraceNode){if(retained.has(node.key))return;retained.add(node.key);for(const parent of node.parent_keys){const value=index.get(parent);if(value)retain(value);}}
  for(const node of chat.nodes)if(filter==='all'||node[filter])retain(node);
  let firstInChat=true;
  function visit(node:TraceNode,depth:number,path:string){
   if(rows.length>limit||!retained.has(node.key))return;
   const nested=(children.get(node.key)??[]).filter(child=>retained.has(child.key));
   rows.push({node,depth,path,hasChildren:!!nested.length,chatTitle:chat.title,firstInChat});firstInChat=false;
   if(!collapsed.has(node.key))for(const child of nested)visit(child,depth+1,path+'/'+child.key);
  }
  for(const node of chat.nodes)if(!node.parent_keys.some(key=>index.has(key)))visit(node,0,chat.id+'/'+node.key);
 }
 return {rows:rows.slice(0,limit),hasMore:rows.length>limit};
}

export function TraceabilityPanel({projectId,chatId,onClose,onOpen}:{projectId:string;chatId?:string;onClose:()=>void;onOpen:(artifactId:string,itemId:string,chatId:string)=>void}){
 const ref=useRef<HTMLDialogElement>(null);
 const [scope,setScope]=useState<'chat'|'project'>(chatId?'chat':'project');
 const [data,setData]=useState<TraceData>();const [error,setError]=useState('');const [reload,setReload]=useState(0);
 const [filter,setFilter]=useState<Filter>('all');const [collapsed,setCollapsed]=useState<Set<string>>(new Set());const [limit,setLimit]=useState(PAGE_SIZE);
 useEffect(()=>{ref.current?.showModal();return()=>ref.current?.close();},[]);
 useEffect(()=>{let active=true;setData(undefined);setError('');setCollapsed(new Set());setLimit(PAGE_SIZE);
  const query=scope==='chat'&&chatId?'?chat_id='+encodeURIComponent(chatId):'';
  api<TraceData>('/projects/'+encodeURIComponent(projectId)+'/traceability'+query).then(value=>{if(active)setData(value);}).catch(e=>{if(active)setError(errText(e));});
  return()=>{active=false;};
 },[projectId,chatId,scope,reload]);
 const view=useMemo(()=>traceRows(data,filter,collapsed,limit),[data,filter,collapsed,limit]);
 function changeFilter(next:Filter){setFilter(next);setCollapsed(new Set());setLimit(PAGE_SIZE);}
 function toggle(key:string){setCollapsed(old=>{const next=new Set(old);if(next.has(key))next.delete(key);else next.add(key);return next;});}
 return <dialog ref={ref} className="traceability-panel" aria-label="追溯矩阵" onCancel={e=>{e.preventDefault();onClose();}}>
  <header className="traceability-head"><div><h2>追溯矩阵</h2><p>需求、场景与用例的关联全貌</p></div><button className="icon-button" aria-label="关闭追溯矩阵" onClick={onClose}><X size={20}/></button></header>
  <div className="traceability-controls"><label>查看范围<select aria-label="追溯矩阵范围" value={scope} onChange={e=>setScope(e.target.value as 'chat'|'project')}>{chatId&&<option value="chat">当前对话</option>}<option value="project">整个项目</option></select></label><button onClick={()=>setReload(value=>value+1)} className="inline"><RefreshCw size={15}/>刷新</button></div>
  <ErrorBox message={error}/>
  {!data&&!error?<p className="muted inline traceability-loading"><Spinner/>正在读取成果关联…</p>:null}
  {data?<><div className="traceability-metrics" aria-label="追溯汇总"><span><strong>{data.summary.requirements}</strong>需求</span><span><strong>{data.summary.scenarios}</strong>场景</span><span><strong>{data.summary.cases}</strong>用例</span><span className="traceability-gap"><strong>{data.summary.missing}</strong>有缺口条目</span><span className="traceability-stale"><strong>{data.summary.stale}</strong>待同步条目</span></div>
   <div className="traceability-filterbar"><label>筛选<select aria-label="追溯矩阵筛选" value={filter} onChange={e=>changeFilter(e.target.value as Filter)}><option value="all">全部关联</option><option value="missing">只看缺口</option><option value="stale">只看待同步</option><option value="independent">独立 N/A</option></select></label><button onClick={()=>setCollapsed(new Set())}>展开全部</button><button onClick={()=>setCollapsed(new Set(data.chats.flatMap(chat=>chat.nodes.map(node=>node.key))))}>收起全部</button></div>
   <p className="traceability-caption">同一条目可出现在多个上游路径下，汇总只计一次；筛选时保留上游路径。点击标题查看并编辑对应成果。</p>
   <div className="traceability-table-scroll"><table aria-label="需求场景用例层级表"><thead><tr><th>需求 → 场景 → 用例</th><th>成果版本</th><th>关联状态</th><th>使用的上游版本</th></tr></thead><tbody>{view.rows.map(({node,depth,path,hasChildren,chatTitle,firstInChat})=><tr key={path} className={node.stale?'traceability-row-stale':node.missing?'traceability-row-gap':''}><td>{firstInChat&&scope==='project'?<p className="traceability-chat-title">{chatTitle}</p>:null}<div className="traceability-treecell" style={{paddingLeft:depth*24}}>{hasChildren?<button className="traceability-expand" aria-label={(collapsed.has(node.key)?'展开 ':'收起 ')+node.item_id} aria-expanded={!collapsed.has(node.key)} onClick={()=>toggle(node.key)}>{collapsed.has(node.key)?<ChevronRight size={16}/>:<ChevronDown size={16}/>}</button>:<span className="traceability-leaf" aria-hidden="true"/>}<span className={'traceability-kind kind-'+node.kind}>{kindLabels[node.kind]}</span><button className="traceability-title" aria-label={node.item_id+' '+node.title} onClick={()=>onOpen(node.artifact_id,node.item_id,node.chat_id)}><strong>{node.item_id}</strong><span>{node.title}</span></button></div></td><td><span>{node.artifact_title}</span><small>v{node.revision} · {node.artifact_id}</small></td><td><div className="traceability-badges">{node.statuses.map(status=><span key={status} className={'traceability-badge status-'+status}>{statusLabels[status]??status}</span>)}</div>{node.reason?<small>{node.reason}</small>:null}</td><td>{node.basis.length?node.basis.map((basis,index)=><small className={basis.stale?'traceability-stale':''} key={index}>{basis.item_id} · {basis.revision?'v'+basis.revision:'版本未记录'}{basis.missing?' · 关联缺失':basis.stale?' → 当前 v'+basis.current_revision:''}</small>):<small>{node.independent?'N/A':node.kind==='analysis'?'原始需求':'未找到上游'}</small>}</td></tr>)}</tbody></table>{!view.rows.length?<p className="traceability-empty">{data.chats.some(chat=>chat.nodes.length)?'当前筛选没有匹配的条目。':'还没有已保存的需求、场景或用例。生成后即可在这里查看关联。'}</p>:null}</div>
   {view.hasMore?<button className="traceability-more" onClick={()=>setLimit(value=>value+PAGE_SIZE)}>继续显示 {PAGE_SIZE} 行</button>:null}
   <footer className="traceability-notes">{data.notes.map(note=><p key={note}>{note}</p>)}</footer>
  </>:null}
 </dialog>;
}
