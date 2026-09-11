import {createContext,useContext} from 'react';
import {api} from './api';
import type {Artifact,Json,TurnCommand,TurnRequest,TurnResponse} from './types';

export const ConversationContext=createContext<{chatId:string;onChanged:()=>void}>({chatId:'',onChanged:()=>{}});
export const newClientMessageId=()=>globalThis.crypto?.randomUUID?.()??'turn-'+Date.now().toString(36)+'-'+Math.random().toString(36).slice(2);
export function notifyDraftChanged(runId?:string){window.dispatchEvent(new CustomEvent('tcg:clarification-changed',{detail:{runId}}));}
export async function postTurn(chatId:string,body:TurnRequest){
 if(!chatId)throw new Error('请先打开成果所属的会话。');
 const result=await api<TurnResponse>('/chats/'+encodeURIComponent(chatId)+'/turns',body);
 for(const part of result.parts??[])if(part.type==='clarification_draft')notifyDraftChanged(part.draft.run_id);
 return result;
}
export function useConversationCommand(){
 const context=useContext(ConversationContext);
 return async(command:TurnCommand,content:string,artifact?:Artifact,chatId?:string,options:Json={})=>{
  const result=await postTurn(chatId||artifact?.chat_id||context.chatId,{client_message_id:newClientMessageId(),content,...(artifact?{artifact_id:artifact.id,artifact_revision:artifact.revision,view_order:artifact.items.map(item=>String(item.id))}:{}),...options,command});
  context.onChanged();
  if(result.status==='failed'||result.status==='cancelled')throw new Error(result.message||'操作未完成');
  return result;
 };
}
