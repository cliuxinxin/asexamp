import {useState} from 'react';
import type {ConversationPrompt} from './types';

// Use only the current server prompt. Choosing a suggestion prepares an editable reply.
export function ConversationSuggestions({prompt,draft='',disabled=false,onChoose}:{prompt?:ConversationPrompt|null;draft?:string;disabled?:boolean;onChoose:(text:string)=>void}){
 const [chosen,setChosen]=useState<Set<string>>(()=>new Set());
 if(!prompt||prompt.busy||prompt.kind==='busy')return null;
 const questions=(prompt.questions??[]).filter(question=>!question.answer&&question.suggestion?.trim()).map(question=>({
  id:'question:'+question.id,label:question.question+' — '+question.suggestion,
  text:question.id+'（'+question.question+'）：'+question.suggestion,
 }));
 const choices=(prompt.choices??[]).filter(choice=>choice.title?.trim()).map(choice=>({id:'choice:'+choice.id,label:choice.title,text:choice.title}));
 const gateReply=['strategy_review','scenario_review','case_draft_review','case_result_review'].includes(prompt.kind)?'同意，继续':prompt.kind==='failed'&&prompt.message.includes('重试当前步骤')?'重试当前步骤':undefined;
 const gateChoices=!questions.length&&!choices.length&&gateReply?[{id:'gate:'+prompt.kind,label:gateReply,text:gateReply}]:[];
 const available=[...questions,...choices,...gateChoices].filter(choice=>!chosen.has(choice.id)&&!draft.includes(choice.text));
 if(!available.length)return null;
 return <div className="conversation-suggestions" role="group" aria-label="建议回复"><span className="suggestions-label">填入回复</span>{available.map(choice=><button key={choice.id} type="button" disabled={disabled} aria-label={'填入回复：'+choice.label} title={choice.label} onClick={()=>{if(disabled)return;setChosen(old=>new Set([...old,choice.id]));onChoose(choice.text);}}>{choice.label}</button>)}</div>;
}
