import {EvidenceRefs} from './EvidenceRefs';
import type {ConversationPrompt as Prompt} from './types';

const confirmationGates=new Set(['workflow_gate','strategy_review','scenario_review','case_draft_review','case_result_review']);

// WorkflowSummary owns the stage; this area only shows material to answer or review.
export function ConversationPrompt({prompt,waiting=false}:{prompt?:Prompt|null;waiting?:boolean}){
 if(!prompt||prompt.busy||prompt.kind==='busy'||prompt.kind==='profile')return null;
 const review=prompt.kind==='case_result_review'?prompt.review:undefined;
 const hasQuestions=!!prompt.questions?.length;
 const hasChoices=!!prompt.choices?.length;
 if(confirmationGates.has(prompt.kind)&&!review&&!hasQuestions&&!hasChoices)return null;
 return <section className="conversation-prompt" aria-label="当前对话提示" data-prompt-id={prompt.id} aria-busy={prompt.busy||waiting||undefined}>
  {review?<>
   <h3>AI 评审意见</h3>
   {review.summary&&<p className="preserve">{review.summary}</p>}
   {typeof review.scope?.reviewed_count==='number'&&<p className="muted small-text">本次评审 {review.scope.reviewed_count}{typeof review.scope.total_count==='number'?` / ${review.scope.total_count}`:''} 条用例</p>}
   {review.issues.length?<div className="table-scroll conversation-review"><table aria-label="AI 评审意见"><thead><tr><th scope="col">评审意见</th><th scope="col">说明</th><th scope="col">相关用例与依据</th></tr></thead><tbody>{review.issues.map((issue,index)=><tr key={index}><td>{issue.title}</td><td className="preserve">{issue.detail||'未提供详细说明'}</td><td>{!!issue.case_ids?.length&&<p>{issue.case_ids.join('、')}</p>}{!!issue.refs?.length&&<EvidenceRefs refs={issue.refs}/>} {!issue.case_ids?.length&&!issue.refs?.length&&<span className="muted">未指定具体用例</span>}</td></tr>)}</tbody></table></div>:<p>本次 AI 评审未列出问题。</p>}
   {!!review.notes?.length&&<ul>{review.notes.map((note,index)=><li className="preserve" key={index}>{note}</li>)}</ul>}
   <p className="review-feedback-hint">可以确认评审结果并完成，也可以直接在输入框中补充意见；修改后会再次请你确认。</p>
  </>:<><h3>{prompt.title}</h3><p className="preserve">{prompt.message}</p></>}
  {!!prompt.questions?.length&&<ol className="conversation-questions">{prompt.questions.map(question=><li key={question.id}><strong>{question.question}</strong>{question.answer?<p>已确认：{question.answer}</p>:question.suggestion&&<p className="muted">建议假设：{question.suggestion}</p>}</li>)}</ol>}
  {!!prompt.choices?.length&&<ol>{prompt.choices.map(choice=><li key={choice.id}>{choice.title}</li>)}</ol>}
 </section>;
}
