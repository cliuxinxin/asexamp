import type {Message,TurnResponse} from './types';

export type LocalTurnReceipt={clientMessageId:string;content:string;createdAt:string;response?:TurnResponse;error?:string};

// Temporary receipts are replaced by the server's authoritative messages on refresh.
export function withLocalTurnReceipts(messages:Message[],receipts:LocalTurnReceipt[]):Message[]{
 const result=[...messages];
 for(const receipt of receipts){
  const savedReply=messages.find(message=>message.role==='assistant'&&(message.metadata?.turn_response?.client_message_id===receipt.clientMessageId||(receipt.response?.id&&message.metadata?.turn_response?.id===receipt.response.id)));
  const turnId=savedReply?.metadata?.turn_response?.id??receipt.response?.id;
  let inputIndex=result.findIndex(message=>message.role==='user'&&(message.metadata?.client_message_id===receipt.clientMessageId||(turnId&&message.metadata?.turn_id===turnId)));
  if(inputIndex<0){
   inputIndex=savedReply?result.indexOf(savedReply):result.length;
   result.splice(inputIndex,0,{id:'local-input:'+receipt.clientMessageId,role:'user',content:receipt.content,created_at:receipt.createdAt,metadata:{}});
  }
  if(!savedReply)result.splice(inputIndex+1,0,{id:'local-reply:'+receipt.clientMessageId,role:'assistant',content:receipt.response?.message||receipt.error||'本轮操作未完成。',created_at:receipt.createdAt,metadata:{...(receipt.response?{turn_response:receipt.response}:{local_failure:true})}});
 }
 return result;
}
