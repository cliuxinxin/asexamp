import {useContext,useState} from 'react';
import {api,errText} from './api';
import {ConversationContext} from './conversation';
import {ArtifactWorkspaceContext} from './workspaceContext';
import {ErrorBox,Spinner} from './ui';
import type {ConversationPrompt,Json,TurnResponse} from './types';

type Props={chatId?:string;prompt?:ConversationPrompt;proposalId:string;artifactId?:string;artifactRevision?:number;changes?:Json[];readOnly?:boolean;disabled?:boolean;onChanged?:()=>void;onTurnResolved?:(response:TurnResponse)=>void|Promise<void>;onRevise?:()=>void;onBusyChange?:(busy:boolean)=>void};

// Cards only navigate. Every proposal decision and commit belongs to the workspace.
export function ArtifactProposalCard({chatId:chatIdProp,prompt,proposalId,artifactId,artifactRevision,changes=[],readOnly=false,disabled=false}:Props){
 const workspace=useContext(ArtifactWorkspaceContext),conversation=useContext(ConversationContext);
 const [loading,setLoading]=useState(false),[error,setError]=useState('');
 const review=prompt?.kind==='case_result_review';
 async function open(){
  if(loading||disabled||!workspace.open)return;
  setLoading(true);setError('');
  try{
   const change=changes.find(value=>value.artifact_id);
   let binding:Json={artifact_id:artifactId??prompt?.artifact_id??change?.artifact_id,artifact_revision:artifactRevision??prompt?.artifact_revision??change?.expected_revision};
   if(!binding.artifact_id||readOnly&&binding.artifact_revision===undefined){
    if(review&&prompt?.run_id)binding=await api<Json>('/runs/'+encodeURIComponent(prompt.run_id)+'/review-proposals/'+encodeURIComponent(proposalId));
    else if(!readOnly){const state=await api<Json>('/chats/'+encodeURIComponent(chatIdProp??conversation.chatId)+'/workspace-state');binding=state.pending_proposal??state.pending_artifact_preview??{};if(binding.id!==proposalId)throw new Error('修改建议已更新，请刷新对话。');}
   }
   if(!binding.artifact_id||readOnly&&binding.artifact_revision===undefined&&binding.expected_revision===undefined)throw new Error('此历史记录缺少成果版本，请从阶段成果查看已保存版本。');
   workspace.open({artifactId:binding.artifact_id,...(readOnly?{revision:binding.artifact_revision??binding.expected_revision,readOnly:true}:{readOnly:false,promptId:prompt?.id}),proposalId,...(prompt?.run_id?{runId:prompt.run_id}:{})});
  }catch(e){setError(errText(e));}finally{setLoading(false);}
 }
 return <section className="artifact-proposal-card" aria-label={review?'评审建议预览':'建议修改预览'}><strong>{review?'评审建议':'成果修改建议'}</strong><p className="muted small-text">{readOnly?'历史记录 · 只读':'等待在成果工作区确认 · 尚未应用'}</p>{prompt?.review?.summary&&<p>{prompt.review.summary}</p>}<button className="primary" disabled={disabled||loading||!workspace.open} onClick={()=>void open()}>{loading&&<Spinner/>}{readOnly?'查看历史工作区':'打开成果工作区'}</button><ErrorBox message={error}/></section>;
}
