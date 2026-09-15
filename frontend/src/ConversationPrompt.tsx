import {EvidenceRefs} from './EvidenceRefs';
import {ClarificationQuestions,type ClarificationAnswer} from './ClarificationQuestions';
export {clarificationReplyText} from './ClarificationQuestions';
import type {ConversationPrompt as Prompt} from './types';

const confirmationGates=new Set(['workflow_gate','strategy_review','scenario_review','case_draft_review','case_result_review']);

// WorkflowSummary owns the stage; this area only shows material to answer or review.
export function ConversationPrompt({prompt,waiting=false,errorInConversation=false,active=true,onAnswer,onFill}:{prompt?:Prompt|null;waiting?:boolean;errorInConversation?:boolean;active?:boolean;onAnswer?:ClarificationAnswer;onFill?:(text:string)=>void}){
 if(!prompt||prompt.busy||prompt.kind==='busy'||prompt.kind==='profile'||prompt.kind==='artifact_proposal')return null;
 if(errorInConversation&&['failed','cancelled'].includes(prompt.kind))return null;
 const review=prompt.kind==='case_result_review'?prompt.review:undefined;
 const hasQuestions=!!prompt.questions?.length;
 const hasChoices=!!prompt.choices?.length;
 if(confirmationGates.has(prompt.kind)&&!review&&!hasQuestions&&!hasChoices)return null;
 return <article className="message assistant"><div className="message-body"><section className="conversation-prompt" aria-label="当前对话提示" data-prompt-id={prompt.id} aria-busy={prompt.busy||waiting||undefined}>
  {review?<>
   <h3>AI 评审意见</h3>
   {review.summary&&<p className="preserve">{review.summary}</p>}
   {typeof review.scope?.reviewed_count==='number'&&<p className="muted small-text">本次评审 {review.scope.reviewed_count}{typeof review.scope.total_count==='number'?` / ${review.scope.total_count}`:''} 条用例</p>}
   {review.issues.length?<div className="table-scroll conversation-review"><table aria-label="AI 评审意见"><thead><tr><th scope="col">评审意见</th><th scope="col">说明</th><th scope="col">相关用例与依据</th></tr></thead><tbody>{review.issues.map((issue,index)=><tr key={index}><td>{issue.title}</td><td className="preserve">{issue.detail||'未提供详细说明'}</td><td>{!!issue.case_ids?.length&&<p>{issue.case_ids.join('、')}</p>}{!!issue.refs?.length&&<EvidenceRefs refs={issue.refs}/>} {!issue.case_ids?.length&&!issue.refs?.length&&<span className="muted">未指定具体用例</span>}</td></tr>)}</tbody></table></div>:<p>本次 AI 评审未列出问题。</p>}
   {!!review.notes?.length&&<ul>{review.notes.map((note,index)=><li className="preserve" key={index}>{note}</li>)}</ul>}
   <p className="review-feedback-hint">可以确认评审建议后修改用例，也可以直接在输入框中补充意见；未确认前保留当前用例。</p>
  </>:<><h3>{prompt.title}</h3><p className="preserve">{prompt.message}</p></>}
  {!!prompt.questions?.length&&<ClarificationQuestions key={prompt.id} questions={prompt.questions} waiting={waiting} active={active&&prompt.kind==='clarification'} onAnswer={onAnswer} onFill={onFill}/>}
  {!!prompt.choices?.length&&<ol>{prompt.choices.map(choice=><li key={choice.id}>{choice.title}</li>)}</ol>}
 </section></div></article>;
}
