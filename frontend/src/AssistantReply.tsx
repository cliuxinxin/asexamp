import type {TurnResponse} from './types';

export function assistantReplyTexts(content:string,response?:TurnResponse):string[]{
 const finalText=(content||response?.message||'').trim();
 const seen=new Set<string>();
 const notes:string[]=[];
 for(const part of response?.parts??[]){
  if(part.type!=='assistant_note'||typeof part.text!=='string')continue;
  const text=part.text.trim();
  if(!text||text===finalText||seen.has(text))continue;
  seen.add(text);notes.push(text);
 }
 return finalText?[...notes,finalText]:notes;
}

export function AssistantReply({texts}:{texts:string[]}){
 return <>{texts.map((text,index)=><div key={index} className="message-text preserve">{text}</div>)}</>;
}
