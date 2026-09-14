import {useEffect,useRef,useState} from 'react';
import {api,errText} from './api';
import {newClientMessageId,postTurn} from './conversation';
import {ChangePreview} from './ChangePreview';
import {Dialog,ErrorBox,Spinner} from './ui';
import type {Artifact,ConversationPrompt,Json,TurnResponse} from './types';

export function ArtifactChangeDialog({chatId,prompt,proposalId,onClose,onResolved,onRevise}:{chatId:string;prompt:ConversationPrompt;proposalId:string;onClose:()=>void;onResolved:(response:TurnResponse)=>void|Promise<void>;onRevise:()=>void}){
 const [preview,setPreview]=useState<Json>();const [error,setError]=useState('');const [working,setWorking]=useState(false);const [loading,setLoading]=useState(true);
 const alive=useRef(true),inFlight=useRef(false);const review=prompt.kind==='case_result_review';
 useEffect(()=>{alive.current=true;return()=>{alive.current=false;};},[]);
 useEffect(()=>{let active=true;setLoading(true);setError('');setPreview(undefined);
  async function load(){
   const value=await api<Json>(review?'/runs/'+encodeURIComponent(prompt.run_id!)+ '/review-proposals/'+encodeURIComponent(proposalId):'/chats/'+encodeURIComponent(chatId)+'/workspace-state');
   const proposal=review?value:value.pending_proposal;
   if(!proposal||proposal.id!==proposalId||(!review&&proposal.prompt_id&&proposal.prompt_id!==prompt.id))throw new Error('修改预览已改变，请关闭后重新查看当前建议。');
   let changes=proposal.changes??[];
   if(review&&changes.some((change:Json)=>change.op))changes=[{artifact_id:proposal.artifact_id??prompt.artifact_id,expected_revision:proposal.expected_revision??prompt.artifact_revision,title:'评审建议',before_items:changes.flatMap((change:Json)=>change.before?[change.before]:[]),items:changes.flatMap((change:Json)=>change.after?[change.after]:[])}];
   if(review&&!changes.length&&Array.isArray(proposal.items))changes=[{...proposal,artifact_id:proposal.artifact_id??prompt.artifact_id,expected_revision:proposal.expected_revision??prompt.artifact_revision}];
   const originals=await Promise.all(changes.map(async(change:Json)=>change.before_items?change:{...change,before_items:(await api<Artifact>('/artifacts/'+encodeURIComponent(change.artifact_id)+'/revisions/'+change.expected_revision)).items}));
   if(active)setPreview({...proposal,changes:originals});
  }
  void load().catch(e=>{if(active)setError(errText(e));}).finally(()=>{if(active)setLoading(false);});
  return()=>{active=false;};
 },[chatId,prompt.id,proposalId,review,prompt.run_id]);
 async function resolve(apply:boolean){
  if(!preview||inFlight.current)return;inFlight.current=true;setWorking(true);setError('');
  try{
   const response=await postTurn(chatId,{client_message_id:newClientMessageId(),reply_to:prompt.id,reply_kind:'confirm',content:review?'确认评审建议，并按建议修改用例。':apply?'确认应用当前修改预览。':'取消当前修改预览。',command:{name:review?'workflow.resume':apply?'artifact.apply':'artifact.discard',arguments:review?{run_id:prompt.run_id,action:'approved'}:{proposal_id:proposalId,artifact_id:prompt.artifact_id,expected_revision:prompt.artifact_revision}}});
   if(!alive.current)return;
   if(!['succeeded','needs_confirmation'].includes(response.status)){setError(response.message||'本次修改尚未应用，请查看当前提示。');return;}
   await onResolved(response);
  }catch(e){if(alive.current)setError(errText(e));}finally{inFlight.current=false;if(alive.current)setWorking(false);}
 }
 return <Dialog title="查看修改预览" onClose={onClose} closeDisabled={working} wide><div className="artifact-change-dialog">
  {loading?<p role="status"><Spinner/> 正在读取本次修改…</p>:preview&&<><p className="preserve">{preview.summary??preview.report?.summary??prompt.message}</p><p className="muted small-text">{review?'当前用例尚未按评审建议修改。确认后应用下面的修改。':'仅应用预览中的成果修改；关联需求为 N/A 的条目不会改写上游需求。'}</p><p className="artifact-diff-legend"><ins>新增内容</ins><del>删除内容</del><span>未高亮内容保持不变</span></p>{preview.changes.map((change:Json,index:number)=><ChangePreview key={change.artifact_id+':'+index} change={change}/>)}{preview.stale&&<p role="alert">成果版本已改变，请返回对话重新生成修改预览。</p>}</>}
  <ErrorBox message={error}/><div className="dialog-actions"><button disabled={working} onClick={onRevise}>返回对话修改</button>{!review&&<button disabled={loading||working||!preview} onClick={()=>void resolve(false)}>取消这项修改</button>}<button className="primary" disabled={loading||working||!preview||!!preview.stale} onClick={()=>void resolve(true)}>{working&&<Spinner/>}{review?'确认评审建议并修改用例':'确认应用修改'}</button></div>
 </div></Dialog>;
}
