import type {ConversationPrompt as Prompt} from './types';

// The snapshot owns the only active question. Earlier receipts remain history.
export function ConversationPrompt({prompt,waiting=false}:{prompt?:Prompt|null;waiting?:boolean}){
 if(!prompt)return null;
 return <section className="conversation-prompt" aria-label="当前对话提示" data-prompt-id={prompt.id} aria-busy={prompt.busy||waiting||undefined}>
  <h3>{prompt.title}</h3><p className="preserve">{prompt.message}</p>
  {!!prompt.questions?.length&&<ol className="conversation-questions">{prompt.questions.map(question=><li key={question.id}><strong>{question.question}</strong>{question.answer?<p>已确认：{question.answer}</p>:question.suggestion&&<p className="muted">建议假设：{question.suggestion}</p>}</li>)}</ol>}
  {!!prompt.choices?.length&&<ol>{prompt.choices.map(choice=><li key={choice.id}>{choice.title}</li>)}</ol>}
  {(prompt.busy||waiting)&&<p className="muted small-text" role="status">正在处理你的回复，完成后会更新这里。你仍可以在聊天中提问。</p>}
 </section>;
}
