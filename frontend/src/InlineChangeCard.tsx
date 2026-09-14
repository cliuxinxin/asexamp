import {useContext,useEffect,useRef,useState} from 'react';
import {api,errText} from './api';
import {ConversationContext,newClientMessageId,postTurn} from './conversation';
import {TableReviewContext} from './tableReviewContext';
import {ChangePreview} from './ChangePreview';
import {EvidenceRefs} from './EvidenceRefs';
import {ErrorBox,Spinner,TextValue} from './ui';
import type {Artifact,ConversationPrompt,Json,TurnResponse} from './types';

type Props={chatId?:string;prompt?:ConversationPrompt;proposalId:string;changes?:Json[];readOnly?:boolean;disabled?:boolean;onChanged:()=>void;onTurnResolved?:(response:TurnResponse)=>void|Promise<void>;onRevise?:()=>void;onBusyChange?:(busy:boolean)=>void};

function reviewChanges(proposal:Json,prompt?:ConversationPrompt):Json[]{
 const changes=Array.isArray(proposal.changes)?proposal.changes:[];
 if(changes.some((change:Json)=>change.op))return [{artifact_id:proposal.artifact_id??prompt?.artifact_id,expected_revision:proposal.expected_revision??proposal.artifact_revision??prompt?.artifact_revision,title:'评审建议',before_items:changes.flatMap((change:Json)=>change.before?[change.before]:[]),items:changes.flatMap((change:Json)=>change.after?[change.after]:[])}];
 if(!changes.length&&Array.isArray(proposal.items))return [{...proposal,artifact_id:proposal.artifact_id??prompt?.artifact_id,expected_revision:proposal.expected_revision??proposal.artifact_revision??prompt?.artifact_revision}];
 return changes;
}

function ReviewNotes({value}:{value:Json}){
 const [visible,setVisible]=useState(5);
 const issues=Array.isArray(value.issues)?value.issues:[];
 const scope=value.scope??{};
 return <div className="inline-review-notes">
  {scope.reviewed_count!==undefined&&<p className="muted small-text">本次评审 {scope.reviewed_count}{scope.total_count!==undefined?` / ${scope.total_count}`:''} 条用例</p>}
  {issues.length>0?<div className="table-scroll conversation-review"><table aria-label="AI 评审意见"><thead><tr><th scope="col">评审意见</th><th scope="col">说明</th><th scope="col">相关用例与依据</th></tr></thead><tbody>{issues.slice(0,visible).map((issue:unknown,index:number)=>{const item=typeof issue==='object'&&issue?issue as Json:{title:String(issue)};return <tr key={index}><td>{item.title??item.summary??item.message??'评审意见'}</td><td className="preserve">{item.detail??item.description??'未提供详细说明'}</td><td>{item.case_ids?.length>0&&<p>{item.case_ids.join('、')}</p>}{item.refs?.length>0&&<EvidenceRefs refs={item.refs}/>} {!item.case_ids?.length&&!item.refs?.length&&<span className="muted">未指定具体用例</span>}</td></tr>;})}</tbody></table></div>:<p className="muted small-text">本次 AI 评审未列出问题。</p>}
  {issues.length>visible&&<button className="inline-fields-more" onClick={()=>setVisible(old=>old+5)}>再查看 {Math.min(5,issues.length-visible)} 项评审意见</button>}
  {Array.isArray(value.notes)&&value.notes.length>0&&<ul>{value.notes.map((note:unknown,index:number)=><li key={index}><TextValue value={note}/></li>)}</ul>}
  <p className="review-feedback-hint">可以接受评审建议后修改用例，也可以直接在输入框中补充意见；未确认前保留当前用例。</p>
 </div>;
}

// The pending prompt owns authorization; a historical receipt is always read-only.
export function InlineChangeCard({chatId:chatIdProp,prompt,proposalId,changes=[],readOnly=false,disabled=false,onChanged,onTurnResolved,onRevise,onBusyChange}:Props){
 const tableReview=useContext(TableReviewContext);
 const context=useContext(ConversationContext),chatId=chatIdProp??context.chatId;
 const [open,setOpen]=useState(!readOnly),[preview,setPreview]=useState<Json>(),[error,setError]=useState('');
 const [working,setWorking]=useState(false),[loading,setLoading]=useState(true),[resolution,setResolution]=useState(''),[refreshCount,setRefreshCount]=useState(0);
 const alive=useRef(true),inFlight=useRef(false),busyCallback=useRef(onBusyChange);
 busyCallback.current=onBusyChange;
 const review=prompt?.kind==='case_result_review';
 useEffect(()=>{alive.current=true;return()=>{alive.current=false;if(inFlight.current)busyCallback.current?.(false);};},[]);
 useEffect(()=>{
  if(readOnly&&!open)return;
  let active=true;setLoading(true);setError('');setResolution('');
  setPreview(changes.length?{changes}:undefined);
  async function load(){
   let proposal:Json;
   if(review){
    if(!prompt?.run_id)throw new Error('缺少评审任务，请刷新当前对话。');
    proposal=await api<Json>('/runs/'+encodeURIComponent(prompt.run_id)+'/review-proposals/'+encodeURIComponent(proposalId));
    if(proposal.id!==proposalId)throw new Error('评审建议已改变，请刷新当前对话。');
   }else if(readOnly)proposal={id:proposalId,changes};
   else{
    const workspace=await api<Json>('/chats/'+encodeURIComponent(chatId)+'/workspace-state');
    proposal=workspace.pending_proposal;
    if(!prompt?.id||!proposal||proposal.id!==proposalId||(proposal.prompt_id&&proposal.prompt_id!==prompt.id))throw new Error('修改预览已改变，请刷新当前对话后查看最新建议。');
   }
   const rows=review?reviewChanges(proposal,prompt):(proposal.changes??[]);
   const completed=await Promise.all(rows.map(async(change:Json)=>Array.isArray(change.before_items)?change:{...change,before_items:(await api<Artifact>('/artifacts/'+encodeURIComponent(change.artifact_id)+'/revisions/'+change.expected_revision)).items}));
   if(active)setPreview({...proposal,changes:completed});
  }
  void load().catch(e=>{if(active)setError(errText(e));}).finally(()=>{if(active)setLoading(false);});
  return()=>{active=false;};
  // Receipts and proposal IDs are immutable; refreshing the surrounding chat must not reset an active submission.
 },[chatId,prompt?.id,prompt?.run_id,proposalId,review,readOnly,open,refreshCount]);
 async function resolve(accept:boolean){
  if(!prompt||!preview||loading||preview.stale||error||disabled||readOnly||inFlight.current)return;
  inFlight.current=true;setWorking(true);setError('');busyCallback.current?.(true);
  try{
   const result=await postTurn(chatId,{client_message_id:newClientMessageId(),reply_to:prompt.id,reply_kind:'confirm',
    content:review?(accept?'确认评审建议，并按建议修改用例。':'拒绝当前评审建议，保留当前用例。'):(accept?'确认应用当前修改预览。':'取消当前修改预览。'),
    command:{name:review?'workflow.resume':accept?'artifact.apply':'artifact.discard',arguments:review?{run_id:prompt.run_id,action:accept?'approved':'rejected'}:{proposal_id:proposalId,artifact_id:prompt.artifact_id,expected_revision:prompt.artifact_revision}}});
   if(!alive.current)return;
   if(!['succeeded','needs_confirmation'].includes(result.status)){setError(result.message||'本次操作尚未完成，请刷新当前提示。');return;}
   setResolution(result.message||(accept?'已接受这项修改。':'已拒绝这项修改。'));
   if(onTurnResolved)await onTurnResolved(result);else onChanged();
  }catch(e){if(alive.current)setError(errText(e));}
  finally{inFlight.current=false;busyCallback.current?.(false);if(alive.current)setWorking(false);}
 }
 const locked=disabled||working||loading||!preview||!!preview?.stale||!!error||!!prompt?.busy;
 const summary=preview?.summary??preview?.report?.summary??prompt?.review?.summary;
 const content=<section className={'inline-change-card'+(readOnly?' inline-change-history':'')} aria-label={review?'评审建议预览':'建议修改预览'}>
  <header><strong>{review?'评审建议':'建议修改'}</strong><span className="muted small-text">{readOnly?'历史记录':resolution?'已处理':'等待确认 · 尚未应用'}</span></header>
  {summary&&<p className="preserve inline-change-summary">{summary}</p>}
  {tableReview.open&&preview&&!loading&&(review||preview.changes?.some((change:Json)=>change.type==='cases'||change.kind==='cases'||change.items?.some((item:Json)=>Array.isArray(item.steps))))&&<button className="text-accent" disabled={working||disabled||!!error} onClick={()=>{const change=preview.changes?.find((item:Json)=>item.type==='cases'||item.kind==='cases'||item.items?.some((row:Json)=>Array.isArray(row.steps)))??preview.changes?.[0];tableReview.open?.({artifactId:preview.artifact_id??change?.artifact_id??prompt?.artifact_id,revision:preview.artifact_revision??change?.expected_revision??prompt?.artifact_revision,runId:review?prompt?.run_id:undefined,proposalId,readOnly,promptId:readOnly?undefined:prompt?.id});}}>进入全屏表格评审</button>}
  {review&&(prompt?.review||preview?.report)&&<ReviewNotes value={prompt?.review??preview?.report??{}}/>}
  {loading?<p role="status"><Spinner/> 正在读取差异…</p>:preview&&<div className="inline-change-diffs">{preview.changes.map((change:Json,index:number)=><ChangePreview key={change.artifact_id+':'+index} change={change} compact/>)}</div>}
  {preview?.stale&&!readOnly&&<p role="alert">成果版本已改变，请刷新对话并重新生成修改预览。</p>}
  <ErrorBox message={error}/>
  {resolution?<p role="status" className="inline-change-resolution">{resolution}</p>:!readOnly&&<footer>
   <button className="primary" disabled={locked} onClick={()=>void resolve(true)}>{working&&<Spinner/>}{review?'接受评审建议':'接受修改'}</button>
   <button disabled={locked} onClick={()=>void resolve(false)}>{review?'拒绝评审建议':'拒绝修改'}</button>
   {onRevise&&<button className="text-button" disabled={working||disabled} onClick={onRevise}>{review?'补充评审意见':'补充修改意见'}</button>}
   {(error||preview?.stale)&&<button disabled={working} onClick={()=>{setRefreshCount(old=>old+1);onChanged();}}>刷新当前提示</button>}
  </footer>}
 </section>;
 return readOnly?<details className="receipt-details" onToggle={event=>setOpen(event.currentTarget.open)}><summary>{review?'评审建议 · 查看当时的差异':'修改预览 · 查看当时的差异'}</summary>{open&&content}</details>:content;
}
