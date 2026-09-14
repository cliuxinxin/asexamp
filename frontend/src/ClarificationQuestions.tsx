import {useRef,useState} from 'react';
import type {ConversationQuestion,QuestionOption} from './types';

export type ClarificationAnswer=(questionId:string,questionText:string,answer:string)=>Promise<boolean|void>|boolean|void;
export function clarificationReplyText(id:string,question:string,answer:string){
 return `仅提交以下澄清答案并更新需求理解，不确认需求理解。\n${id}（${question}）：${answer}`;
}

// An incomplete group must not turn an open question into a one-sided yes/no choice.
function completeOptions(options?:QuestionOption[]):QuestionOption[]{
 if(!Array.isArray(options)||options.length<2||options.some(option=>!option||['id','label','answer'].some(key=>typeof option[key as keyof QuestionOption]!=='string'||!option[key as keyof QuestionOption].trim())))return [];
 if(['id','label','answer'].some(key=>new Set(options.map(option=>option[key as keyof QuestionOption].trim())).size!==options.length))return [];
 return options;
}

export function ClarificationQuestions({questions,waiting=false,active=true,onAnswer}:{questions:ConversationQuestion[];waiting?:boolean;active?:boolean;onAnswer?:ClarificationAnswer}){
 const inFlight=useRef(false);
 const [pending,setPending]=useState<string|null>(null);
 const [accepted,setAccepted]=useState<Record<string,string>>({});
 const [error,setError]=useState<{id:string;text:string}|null>(null);
 const answer=async(question:ConversationQuestion,value:string)=>{
  if(!active||waiting||inFlight.current||!onAnswer||question.answer||accepted[question.id])return;
  inFlight.current=true;setPending(question.id);setError(null);
  try{
   if(await onAnswer(question.id,question.question,value)===true)setAccepted(current=>({...current,[question.id]:value}));
  }catch(error){setError({id:question.id,text:error instanceof Error?error.message:'答案提交失败，请重试。'});}
  finally{inFlight.current=false;setPending(null);}
 };
 return <ol className="conversation-questions">{questions.map(question=>{
  const submitted=question.answer||accepted[question.id];
  const options=completeOptions(question.options);
  const interactive=active&&!!onAnswer&&!submitted;
  return <li key={question.id}><div className="clarification-question" role="group" aria-label={question.id+' '+question.question} aria-busy={pending===question.id||undefined}>
   <strong>{question.question}</strong>
   {submitted?<p>{question.answer?'已确认：':'已提交：'}{submitted}</p>:<>
    {options.length?<><p className="muted small-text">请选择适用规则，选择后仅提交本题答案。</p><div className="clarification-options">{options.map(option=><div key={option.id} className="clarification-option">{interactive?<button type="button" disabled={waiting||!!pending} onClick={()=>void answer(question,option.answer)}>{option.label}</button>:<strong>{option.label}</strong>}<p className="muted preserve">{option.answer}</p></div>)}</div></>:<>
     {question.suggestion&&<p className="muted">建议假设：{question.suggestion}</p>}
     {interactive&&question.suggestion?.trim()&&<button type="button" className="clarification-adopt" disabled={waiting||!!pending} aria-label={'采用 '+question.id+' 建议：'+question.suggestion} onClick={()=>void answer(question,question.suggestion!)}>采用这个建议</button>}
    </>}
    {active&&<p className="muted small-text">也可以在输入框中补充或修改答案。</p>}
   </>}
   {error?.id===question.id&&<p className="error-text" role="alert">{error.text}</p>}
  </div></li>;
 })}</ol>;
}
