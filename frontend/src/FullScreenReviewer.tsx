import {memo,useCallback,useEffect,useMemo,useRef,useState} from 'react';
import {api,errText} from './api';
import {diffText,equalConfig} from './profile-diff';
import type {Json} from './types';
import type {TableReviewRequest} from './tableReviewContext';
import {applyDecisions,canonicalField,cellChanges,cloneItems,globalIssues,issuesForCell,issueText,reviewChanges,rowKey} from './table-review-model';
import type {ReviewChange,ReviewColumn,ReviewDecision,ReviewRow,TableReviewData} from './table-review-model';

type SendBinding={artifactRevision:number;proposalId?:string;promptId?:string};
type Props={request:TableReviewRequest;chatId:string;messages:Json[];refreshKey?:string|number;onClose:()=>void;onSaved:(result:Json)=>void|Promise<void>;onSend:(text:string,selectedIds:string[],binding?:SendBinding)=>Promise<void>};
type Filter='all'|'changes'|'comments'|'issues';
type Editor={itemId:string;field:string;header:string;value:string;steps?:Json[];error?:string};
const editable=(column:ReviewColumn)=>column.editable!==false&&canonicalField(column)!=='id'&&(column.value_source!=='default')&&(column.value_source!=='derived'||['steps','expected'].includes(canonicalField(column)));
const signature=(data:TableReviewData)=>JSON.stringify([data.artifact_revision,data.proposal_id,data.profile_revision,data.proposed_items,data.read_only,data.prompt_id]);
const emptyRows:ReviewRow[]=[];

export const CellDiffViewer=memo(function CellDiffViewer({before,after,changed}:{before:string;after:string;changed:boolean}){
 const parts=useMemo(()=>changed?diffText(before,after):[{kind:'same',text:after}],[before,after,changed]);
 return <span className="review-cell-text">{parts.map((part,index)=>part.kind==='remove'?<del key={index}>{part.text}</del>:part.kind==='add'?<ins key={index}>{part.text}</ins>:<span key={index}>{part.text}</span>)}</span>;
});

export function FullScreenReviewer({request,chatId,messages,refreshKey,onClose,onSaved,onSend}:Props){
 const [data,setData]=useState<TableReviewData|null>(null),[draft,setDraft]=useState<Json[]>([]),[rows,setRows]=useState<ReviewRow[]>(emptyRows);
 const [decisions,setDecisions]=useState<Record<string,ReviewDecision>>({}),[manual,setManual]=useState(false);
 const [layout,setLayout]=useState<'case'|'step'>('case'),[filter,setFilter]=useState<Filter>('all'),[selected,setSelected]=useState<string[]>([]);
 const [sidebar,setSidebar]=useState(true),[chatDraft,setChatDraft]=useState(''),[chatBusy,setChatBusy]=useState(false);
 const [loading,setLoading]=useState(true),[projecting,setProjecting]=useState(false),[projectionError,setProjectionError]=useState(false),[busy,setBusy]=useState(false),[error,setError]=useState(''),[notice,setNotice]=useState('');
 const [incoming,setIncoming]=useState<TableReviewData|null>(null),[editor,setEditor]=useState<Editor|null>(null),[activeCell,setActiveCell]=useState<string|null>(null),[exitWarning,setExitWarning]=useState(false),[visible,setVisible]=useState(100);
 const container=useRef<HTMLDivElement>(null),loadSerial=useRef(0),projectionSerial=useRef(0),latestData=useRef<TableReviewData|null>(null),latestDirty=useRef(false),saving=useRef(false),chatSending=useRef(false),saveReceipt=useRef<{fingerprint:string;id:string}|null>(null),chatLog=useRef<HTMLDivElement>(null);
 const dirty=manual||Object.keys(decisions).length>0;latestDirty.current=dirty||!!editor;latestData.current=data;
 const changes=useMemo(()=>data?reviewChanges(data.original_items,data.proposed_items):[],[data]);
 const unresolved=useMemo(()=>changes.filter(change=>!decisions[change.key]),[changes,decisions]);
 const baseItems=data?.original_items??[],proposedItems=data?.proposed_items??[];
 const locked=!!request.readOnly||!!data?.read_only||!!incoming||busy||loading||chatBusy;
 const endpoint=`/artifacts/${encodeURIComponent(request.artifactId)}/table-review`;
 const body=useCallback((items:Json[],mode=layout)=>({items,layout:mode,expected_revision:data?.artifact_revision,profile_id:data?.profile_id,profile_revision:data?.profile_revision,run_id:data?.run_id,proposal_id:data?.proposal_id,prompt_id:data?.prompt_id,revision:(request.readOnly||data?.read_only)?data?.artifact_revision:undefined}),[data,layout,request.readOnly]);
 const install=useCallback((next:TableReviewData)=>{setData(next);setDraft(cloneItems(next.proposed_items));setRows(next.proposed_rows);setLayout(next.layout);setDecisions({});setManual(false);setIncoming(null);setEditor(null);setActiveCell(null);setError('');setProjectionError(false);setNotice('');setVisible(100);
  latestData.current=next;latestDirty.current=false;
 },[]);
 const load=useCallback(async(discard=false,discover=false)=>{
  const sequence=++loadSerial.current;
  if(!latestData.current)setLoading(true);
  const params=new URLSearchParams();if(latestData.current)params.set('layout',latestData.current.layout);
  if(request.runId)params.set('run_id',request.runId);
  if(request.proposalId&&!discover&&(!latestData.current||request.readOnly))params.set('proposal_id',request.proposalId);
  if(request.readOnly&&request.revision!==undefined)params.set('revision',String(request.revision));
  try{
   const next=await api<TableReviewData>(endpoint+'?'+params);
   if(sequence!==loadSerial.current)return;
   const current=latestData.current;
   if(current&&signature(current)===signature(next)){setError('');return;}
   if(current&&latestDirty.current&&!discard){setIncoming(next);setNotice('有新的成果或评审建议。本地审阅内容已保留，请核对后重新载入。');return;}install(next);
  }catch(cause){if(sequence===loadSerial.current)setError(errText(cause));}
  finally{if(sequence===loadSerial.current)setLoading(false);}
 },[endpoint,request.runId,request.proposalId,request.readOnly,request.revision,install]);
 useEffect(()=>{void load();return()=>{loadSerial.current++;};},[load,refreshKey]);
 useEffect(()=>{if(chatLog.current)chatLog.current.scrollTop=chatLog.current.scrollHeight;},[messages,chatBusy]);
 useEffect(()=>{if(editor?.steps)container.current?.querySelector<HTMLTextAreaElement>('.review-step-editor textarea')?.focus();},[!!editor?.steps]);
 useEffect(()=>{
  const previous=document.activeElement as HTMLElement|null;const overflow=document.body.style.overflow;document.body.style.overflow='hidden';container.current?.focus();
  return()=>{document.body.style.overflow=overflow;previous?.focus();};
 },[]);
 const close=()=>{if(busy||chatBusy)return;if(editor){setEditor(null);return;}if(dirty)setExitWarning(true);else onClose();};
 useEffect(()=>{
  if(!data)return;
  const sequence=++projectionSerial.current;
  if(equalConfig(draft,data.proposed_items)&&layout===data.layout){setRows(data.proposed_rows);setProjecting(false);setProjectionError(false);return;}
  setProjecting(true);setProjectionError(false);
  const timer=setTimeout(()=>{
   void api<{columns:ReviewColumn[];rows:ReviewRow[]}>(endpoint+'/project',body(draft)).then(next=>{
    if(sequence!==projectionSerial.current)return;
    setRows(next.rows);setData(old=>old?{...old,columns:next.columns}:old);setProjecting(false);
   }).catch(cause=>{if(sequence===projectionSerial.current){setError(errText(cause));setProjecting(false);setProjectionError(true);}});
  },120);
  return()=>{clearTimeout(timer);projectionSerial.current++;};
 },[draft,layout,data?.artifact_revision,data?.proposal_id,endpoint]);
 // Original and proposed projections change with layout too; those snapshots are immutable.
 useEffect(()=>{
  if(!data||layout===data.layout)return;
  let live=true;
  void Promise.all([api<{rows:ReviewRow[]}>(endpoint+'/project',body(data.original_items)),api<{rows:ReviewRow[]}>(endpoint+'/project',body(data.proposed_items))]).then(([original,proposed])=>{
   if(live)setData(old=>old?{...old,layout,original_rows:original.rows,proposed_rows:proposed.rows}:old);
  }).catch(cause=>{if(live)setError(errText(cause));});
  return()=>{live=false;};
 },[layout,data?.artifact_revision,data?.proposal_id,endpoint]);
 const originalRows=useMemo(()=>new Map((data?.original_rows??[]).map(row=>[rowKey(row),row])),[data?.original_rows]);
 const proposedRows=useMemo(()=>new Map((data?.proposed_rows??[]).map(row=>[rowKey(row),row])),[data?.proposed_rows]);
 const currentRows=useMemo(()=>new Map(rows.map(row=>[rowKey(row),row])),[rows]);
 const columns=data?.columns??[],issues=data?.issues??[];
 const allItems=useMemo(()=>[...new Map([...baseItems,...proposedItems].map(item=>[String(item.id),item])).values()],[baseItems,proposedItems]);
 const notes=useMemo(()=>globalIssues(issues,allItems,columns),[issues,allItems,columns]);
 const gridRows=useMemo(()=>{
  const all=new Map<string,ReviewRow>();
  for(const row of [...(data?.proposed_rows??[]),...(data?.original_rows??[]),...rows])if(!all.has(rowKey(row)))all.set(rowKey(row),row);
  return [...all.values()].filter(row=>{
   if(filter==='all')return true;
   if(filter==='changes')return changes.some(change=>change.itemId===row.item_id);
   const comments=columns.flatMap((column,index)=>issuesForCell(issues,row.item_id,column,index===Math.max(0,columns.findIndex(value=>canonicalField(value)==='id'))));
   if(filter==='comments')return comments.length>0;
   return comments.length>0||allItems.some(item=>String(item.id)===row.item_id&&Object.keys(item._template_field_notes??{}).length>0);
  });
 },[data?.proposed_rows,data?.original_rows,rows,filter,changes,columns,issues,allItems]);
 const hiddenChanges=useMemo(()=>changes.filter(change=>change.field!=='$row'&&!columns.some(column=>cellChanges([change],change.itemId,column,null).length)),[changes,columns]);
 const decide=(chosen:ReviewChange[],decision:'accept'|'reject')=>{
  if(locked||!data)return;
  setDraft(old=>applyDecisions(old,data.original_items,data.proposed_items,chosen,decision));
  setDecisions(old=>({...old,...Object.fromEntries(chosen.map(change=>[change.key,decision]))}));setActiveCell(null);setNotice('');
 };
 const openEditor=(itemId:string,column:ReviewColumn)=>{
  if(locked||!editable(column))return;
  if(editor){setNotice('请先保存或取消当前单元格编辑。');return;}
  const item=draft.find(value=>String(value.id)===itemId);if(!item){setNotice('请先保留此用例，再编辑字段。');return;}
  const field=canonicalField(column);
  if(field==='steps'||field==='expected')setEditor({itemId,field:'steps',header:'步骤和预期结果',value:'',steps:structuredClone(item.steps??[])});
  else setEditor({itemId,field,header:column.header,value:typeof item[field]==='object'?JSON.stringify(item[field],null,2):String(item[field]??'')});
  setActiveCell(null);
 };
 const saveEditor=()=>{
  if(!editor||locked)return;
  const existing=draft.find(item=>String(item.id)===editor.itemId);if(!existing)return;
  let value:unknown=editor.value;
  if(editor.steps){
   if(!editor.steps.length||editor.steps.some(step=>!String(step.action??'').trim()||!String(step.expected??'').trim())){setEditor({...editor,error:'每一步都需要填写操作和对应的预期结果。'});return;}
   value=editor.steps;
  }else if(existing[editor.field]!==null&&typeof existing[editor.field]==='object'){
   try{value=JSON.parse(editor.value);if(Array.isArray(value)!==Array.isArray(existing[editor.field])||value===null||typeof value!=='object')throw new Error();}catch{setEditor({...editor,error:'请保留原字段的 JSON 数组或对象格式。'});return;}
  }else if(typeof existing[editor.field]==='number'){
   value=Number(editor.value);if(!editor.value.trim()||!Number.isFinite(value)){setEditor({...editor,error:'此字段需要有效数字。'});return;}
  }else if(typeof existing[editor.field]==='boolean'){
   if(!['true','false'].includes(editor.value)){setEditor({...editor,error:'布尔字段请填写 true 或 false。'});return;}value=editor.value==='true';
  }
  setDraft(old=>old.map(item=>String(item.id)===editor.itemId?{...item,[editor.field]:value}:item));
  const chosen=changes.filter(change=>change.itemId===editor.itemId&&(change.field==='$row'||change.field===editor.field||editor.field==='steps'&&change.field.startsWith('steps.')));
  setDecisions(old=>({...old,...Object.fromEntries(chosen.map(change=>[change.key,'manual' as ReviewDecision]))}));setManual(true);setEditor(null);
 };
 const save=async()=>{
  if(!data||locked||saving.current||unresolved.length||projecting||projectionError)return;
  saving.current=true;setBusy(true);setError('');
  try{
   const payload=body(draft),fingerprint=JSON.stringify(payload);
   if(saveReceipt.current?.fingerprint!==fingerprint)saveReceipt.current={fingerprint,id:globalThis.crypto?.randomUUID?.()??`table-review-${Date.now()}-${Math.random()}`};
   const result=await api<Json>(endpoint+'/save',{...payload,client_request_id:saveReceipt.current.id});
   setDecisions({});setManual(false);latestDirty.current=false;setNotice(result.message??'审阅内容已保存。');await onSaved(result);
  }catch(cause){setError(errText(cause));}
  finally{saving.current=false;setBusy(false);}
 };
 const exportDraft=async()=>{
  if(!data||busy||projecting||projectionError||editor)return;
  setBusy(true);setError('');
  try{
   const response=await fetch('/api'+endpoint+'/export',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body(draft))});
   if(!response.ok){const result=await response.json();throw new Error(typeof result.detail==='string'?result.detail:JSON.stringify(result.detail??result));}
   const blob=await response.blob(),href=URL.createObjectURL(blob),link=document.createElement('a');link.href=href;
   link.download=decodeURIComponent(response.headers.get('Content-Disposition')?.match(/filename\*=UTF-8''([^;]+)/)?.[1]??`${data.title}—审阅稿.xlsx`);document.body.append(link);link.click();link.remove();setTimeout(()=>URL.revokeObjectURL(href),1000);
   setNotice(unresolved.length?'已导出当前预览内容；尚未决定的修改按 AI 建议导出，评审尚未确认。':'已导出当前审阅内容。');
  }catch(cause){setError(errText(cause));}finally{setBusy(false);}
 };
 const send=async()=>{
  if(!chatDraft.trim()||chatSending.current||busy||locked)return;
  if(dirty||editor){setNotice('请先保存本次审阅，或撤销本地编辑后再让 AI 修改；你的修改会保留。');return;}
  chatSending.current=true;setChatBusy(true);setError('');
  try{await onSend(chatDraft.trim(),selected,{artifactRevision:data!.artifact_revision,proposalId:data!.proposal_id,promptId:data!.prompt_id});setChatDraft('');await load(false,true);}
  catch(cause){setError(errText(cause));}finally{chatSending.current=false;setChatBusy(false);}
 };
 const reset=()=>{if(data){setDraft(cloneItems(data.proposed_items));setDecisions({});setManual(false);setEditor(null);setNotice('已撤销本地审阅，恢复 AI 建议预览。');latestDirty.current=false;}};
 const onKeyDown=(event:React.KeyboardEvent<HTMLDivElement>)=>{
  if(event.key==='Escape'){event.preventDefault();event.stopPropagation();close();}
  if(event.key==='Tab'){
   const nodes=Array.from(container.current?.querySelectorAll<HTMLElement>('button:not(:disabled),input:not(:disabled),textarea:not(:disabled),select:not(:disabled),[tabindex="0"]')??[]).filter(node=>!node.hidden);
   const first=nodes[0],last=nodes.at(-1);
   if(event.shiftKey&&document.activeElement===first){event.preventDefault();last?.focus();}else if(!event.shiftKey&&document.activeElement===last){event.preventDefault();first?.focus();}
  }
 };
 return <div className="full-screen-workspace" role="dialog" aria-modal="true" aria-label="全屏表格评审" ref={container} tabIndex={-1} onKeyDown={onKeyDown}>
  <header className="full-review-toolbar"><div className="full-review-heading"><button aria-label="返回对话" onClick={close} disabled={busy||chatBusy}>← 返回对话</button><div><h2 id="full-review-title">{data?.title??'测试用例'} <span>· v{data?.artifact_revision??request.revision??'—'}</span></h2><p>{request.readOnly||data?.read_only?'只读查看':data?.proposal_id?'评审建议 · 逐项决定后保存':'表格编辑 · 保存为新版本'}</p></div></div>
   <div className="full-review-actions"><label>布局 <select aria-label="表格与 Excel 布局" value={layout} disabled={loading||busy||projecting} onChange={event=>setLayout(event.target.value as 'case'|'step')}><option value="case">每条用例一行</option><option value="step">每个步骤一行</option></select></label><button onClick={()=>void exportDraft()} disabled={!data||busy||projecting||projectionError||!!incoming||!!editor}>导出 Excel</button>{!request.readOnly&&<><button onClick={()=>decide(unresolved,'accept')} disabled={locked||!unresolved.length}>全部接受</button><button className="primary" onClick={()=>void save()} disabled={locked||projecting||projectionError||!!unresolved.length||!!editor||(!dirty&&!data?.proposal_id)}>{busy?'正在保存…':data?.proposal_id?'保存并完成评审':'保存更改'}</button></>}<button aria-expanded={sidebar} onClick={()=>setSidebar(value=>!value)}>{sidebar?'收起助手':'打开助手'}</button></div></header>
  <div className="full-review-filterbar"><div role="group" aria-label="筛选评审用例">{([['all','显示全部'],['changes','仅看 AI 修改'],['comments','仅看有批注'],['issues','仅看有问题']] as [Filter,string][]).map(([value,label])=><button key={value} aria-pressed={filter===value} onClick={()=>{setFilter(value);setVisible(100);}}>{label}</button>)}</div><span>{new Set(gridRows.map(row=>row.item_id)).size} 条用例 · {unresolved.length} 处待决定{selected.length?` · 已选 ${selected.length} 条`:''}{projecting?' · 正在更新表格…':''}</span>{dirty&&<button disabled={busy} onClick={reset}>撤销本地审阅</button>}</div>
  {loading&&<p className="full-review-banner" role="status">正在加载表格与评审建议…</p>}
  {error&&<div className="full-review-banner error" role="alert">{error} <button disabled={busy} onClick={()=>void load(false,true)}>刷新最新状态</button></div>}
  {notice&&<p className="full-review-banner" role="status">{notice}</p>}
  {(incoming||data?.stale)&&<div className="full-review-banner warning" role="alert">{data?.message??'当前成果或评审建议已更新。为避免覆盖新内容，保存已暂停。'}<button disabled={busy} onClick={()=>{if(incoming)install(incoming);else void load(true,true);}}>放弃本地审阅并载入最新版本</button></div>}
  {exitWarning&&<div className="full-review-banner warning" role="alert">本地审阅尚未保存，是否返回对话？<button onClick={()=>setExitWarning(false)}>继续审阅</button><button onClick={onClose}>放弃本地审阅并返回</button></div>}
  {notes.length>0&&<details className="full-review-notes"><summary>整体评审意见（{notes.length}）</summary><ul>{notes.map((issue,index)=><li key={index}>{issueText(issue)}</li>)}</ul></details>}
  {hiddenChanges.length>0&&<details className="full-review-notes"><summary>模板列之外的修改（{hiddenChanges.length}）</summary><p>这些字段保留在成果中，不增加 Excel 列。</p>{hiddenChanges.map(change=><div className="review-hidden-change" key={change.key}><strong>{change.itemId} · {change.field}</strong><CellDiffViewer changed before={JSON.stringify(baseItems.find(item=>String(item.id)===change.itemId)?.[change.field])??''} after={JSON.stringify(proposedItems.find(item=>String(item.id)===change.itemId)?.[change.field])??''}/><span>{decisions[change.key]?'已决定':'待决定'}</span><button disabled={locked} onClick={()=>decide([change],'accept')}>接受</button><button disabled={locked} onClick={()=>decide([change],'reject')}>拒绝</button></div>)}</details>}
  <div className={'full-review-body'+(sidebar?' with-assistant':'')}>
   <main className="full-review-grid-region" aria-label="用例评审工作区">
    <div className="excel-grid-scroll"><table className="excel-grid-table" aria-label="测试用例评审表格"><thead><tr><th className="review-selection-column" scope="col"><input type="checkbox" aria-label="选择当前筛选全部用例" checked={gridRows.length>0&&gridRows.every(row=>selected.includes(row.item_id))} onChange={event=>setSelected(event.target.checked?[...new Set(gridRows.map(row=>row.item_id))]:[])} /></th>{columns.map((column,index)=><th key={index} scope="col" className={canonicalField(column)==='id'?'review-id-column':''}>{column.header}</th>)}</tr></thead><tbody>{gridRows.slice(0,visible).map(row=>{
     const key=rowKey(row),before=originalRows.get(key),after=proposedRows.get(key),current=currentRows.get(key),rowChange=changes.find(change=>change.itemId===row.item_id&&change.field==='$row');
     return <tr key={key} data-item-id={row.item_id} className={(selected.includes(row.item_id)?'is-selected ':'')+(rowChange?'review-row-'+rowChange.operation:'')}><td className="review-selection-column"><input type="checkbox" aria-label={`选择 ${row.item_id}${row.step_index===null?'':` 第 ${row.step_index+1} 步`}`} checked={selected.includes(row.item_id)} onChange={event=>setSelected(old=>event.target.checked?[...new Set([...old,row.item_id])]:old.filter(id=>id!==row.item_id))}/>{rowChange&&<div className="review-row-decision"><span>{rowChange.operation==='add'?'AI 新增':'AI 删除'}</span>{decisions[rowChange.key]?<small>{decisions[rowChange.key]==='accept'?'已接受':'已拒绝'}</small>:<><button disabled={locked} aria-label={`接受 ${row.item_id} ${rowChange.operation==='add'?'新增':'删除'}`} onClick={()=>decide([rowChange],'accept')}>接受</button><button disabled={locked} aria-label={`拒绝 ${row.item_id} ${rowChange.operation==='add'?'新增':'删除'}`} onClick={()=>decide([rowChange],'reject')}>拒绝</button></>}</div>}</td>
      {columns.map((column,index)=>{
       const units=cellChanges(changes,row.item_id,column,row.step_index),pending=units.some(change=>!decisions[change.key]);
       const oldText=before?.cells[index]??'',nextText=current?.cells[index]??'',changed=oldText!==nextText;
       const cellKey=key+':'+index,cellIssues=issuesForCell(issues,row.item_id,column,index===Math.max(0,columns.findIndex(value=>canonicalField(value)==='id')));
       const canEdit=editable(column)&&!!draft.find(item=>String(item.id)===row.item_id);
       return <td key={index} className={(canonicalField(column)==='id'?'review-id-column ':'')+(changed?'review-changed-cell ':'')+(pending?'review-pending-cell':'')}>
        {editor&&!editor.steps&&editor.itemId===row.item_id&&editor.field===canonicalField(column)?<div className="review-inline-editor" role="group" aria-label={`编辑 ${editor.itemId} ${editor.header}`}><textarea autoFocus aria-label={`编辑 ${editor.header}`} value={editor.value} onChange={event=>setEditor({...editor,value:event.target.value,error:undefined})}/>{editor.error&&<p role="alert">{editor.error}</p>}<div><button onClick={()=>setEditor(null)}>取消</button><button className="primary" onClick={saveEditor}>保存单元格</button></div></div>:<div className="review-cell-body" role="button" tabIndex={0} aria-label={`${row.item_id} ${column.header}${row.step_index===null?'':` 第 ${row.step_index+1} 步`}`} onClick={()=>setActiveCell(activeCell===cellKey?null:cellKey)} onDoubleClick={()=>openEditor(row.item_id,column)} onKeyDown={event=>{if(event.key==='Enter'){event.preventDefault();setActiveCell(cellKey);}if(event.key==='F2'){event.preventDefault();openEditor(row.item_id,column);}}}>
         <CellDiffViewer before={oldText} after={nextText} changed={changed}/>{!nextText&&!oldText&&<span className="review-empty-cell">—</span>}{units.length>0&&!pending&&<small className="review-cell-resolved">{units.some(change=>decisions[change.key]==='manual')?'已手动编辑':units.every(change=>decisions[change.key]==='reject')?'已拒绝':'已决定'}</small>}
        </div>}
        {cellIssues.length>0&&<details className="review-cell-comment"><summary aria-label={`${row.item_id} ${column.header} 批注`}>批注 {cellIssues.length}</summary><div role="note">{cellIssues.map((issue,issueIndex)=><p key={issueIndex}>{issueText(issue)}</p>)}</div></details>}
        {activeCell===cellKey&&!locked&&<div className="review-cell-menu" role="group" aria-label={`${row.item_id} ${column.header} 操作`}>{units.length>0&&!rowChange&&<><button onClick={()=>decide(units,'accept')}>接受修改</button><button onClick={()=>decide(units,'reject')}>拒绝修改</button></>}{canEdit?<button onClick={()=>openEditor(row.item_id,column)}>手动编辑</button>:<span>此列由模板计算或不可修改</span>}<button aria-label="关闭单元格操作" onClick={()=>setActiveCell(null)}>×</button></div>}
       </td>;
      })}</tr>;
    })}</tbody></table>{!loading&&!gridRows.length&&<p className="review-empty-state">此筛选下没有用例。</p>}{visible<gridRows.length&&<button className="review-load-more" onClick={()=>setVisible(value=>value+100)}>再显示 {Math.min(100,gridRows.length-visible)} 行（共 {gridRows.length} 行）</button>}</div>
    <footer className="full-review-grid-footer">表头、列顺序和单元格内容与当前 Excel 导出一致。双击单元格或按 F2 编辑；步骤与预期保持配对。{unresolved.length>0&&<strong>还有 {unresolved.length} 处建议需要接受或拒绝。</strong>}</footer>
   </main>
   {sidebar&&<aside className="full-review-assistant" aria-label="评审助手"><header><strong>评审助手</strong><span>{selected.length?`针对已选 ${selected.length} 条`:'当前用例成果'}</span></header><div className="full-review-chat-log" ref={chatLog} role="log" aria-label="当前对话">{messages.filter(message=>typeof message.content==='string'&&message.content.trim()).slice(-12).map((message,index)=><div key={message.id??index} className={'review-chat-message '+(message.role==='user'?'user':'assistant')}><small>{message.role==='user'?'你':'AI'}</small><p>{message.content}</p></div>)}{!messages.length&&<p>可以提问，或让 AI 修改当前选中的用例。</p>}</div><form onSubmit={event=>{event.preventDefault();void send();}}><label htmlFor="review-chat-input">{selected.length?`让 AI 处理选中的 ${selected.length} 条用例`:'向 AI 提问或提出修改'}</label><textarea aria-label="评审对话输入" id="review-chat-input" value={chatDraft} onChange={event=>setChatDraft(event.target.value)} placeholder="例如：把选中用例的前置条件改为已认证" disabled={busy||chatBusy||!!request.readOnly}/>{dirty&&<p className="review-draft-hint">本地审阅已保留。保存或撤销后可继续让 AI 修改。</p>}<button className="primary" aria-label="发送评审消息" disabled={!chatDraft.trim()||locked||dirty||!!editor} type="submit">{chatBusy?'AI 正在处理…':'发送'}</button></form></aside>}
  </div>
  {editor?.steps&&<div className="review-editor-backdrop"><section className="review-cell-editor" role="group" aria-label={`编辑 ${editor.itemId} ${editor.header}`}><header><h3>{editor.itemId} · {editor.header}</h3><button aria-label="取消编辑" onClick={()=>setEditor(null)}>×</button></header>{editor.steps?<><p>每行是一组操作和对应的预期结果。保存时两列一起更新。</p><div className="review-step-editor">{editor.steps.map((step,index)=><div className="review-step-pair" key={index}><span>{index+1}</span><label>操作<textarea aria-label={`第 ${index+1} 步操作`} value={String(step.action??'')} onChange={event=>setEditor(old=>old?{...old,steps:old.steps!.map((value,i)=>i===index?{...value,action:event.target.value}:value)}:old)}/></label><label>预期结果<textarea aria-label={`第 ${index+1} 步预期结果`} value={String(step.expected??'')} onChange={event=>setEditor(old=>old?{...old,steps:old.steps!.map((value,i)=>i===index?{...value,expected:event.target.value}:value)}:old)}/></label><button aria-label={`删除第 ${index+1} 步`} disabled={editor.steps!.length<2} onClick={()=>setEditor(old=>old?{...old,steps:old.steps!.filter((_,i)=>i!==index)}:old)}>删除</button></div>)}</div><button onClick={()=>setEditor(old=>old?{...old,steps:[...old.steps!,{action:'',expected:''}]}:old)}>添加步骤</button></>:<textarea autoFocus aria-label={`编辑 ${editor.header}`} value={editor.value} onChange={event=>setEditor({...editor,value:event.target.value,error:undefined})}/>} {editor.error&&<p role="alert">{editor.error}</p>}<footer><button onClick={()=>setEditor(null)}>取消</button><button className="primary" onClick={saveEditor}>保存单元格</button></footer></section></div>}
 </div>;
}
