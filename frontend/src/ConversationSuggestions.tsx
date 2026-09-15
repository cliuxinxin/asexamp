import type {ConversationPrompt,TurnRequest} from './types';

type ReplyKind=TurnRequest['reply_kind'];
type Reply={id:string;label:string;text:string;kind?:ReplyKind};
const reviewReplies:Record<string,{label:string;text:string;subject:string}>={
 strategy_review:{label:'确认需求理解并继续',text:'确认当前需求理解并继续流程。',subject:'需求理解'},
 scenario_review:{label:'确认场景并继续',text:'确认当前测试场景并继续流程。',subject:'测试场景'},
 case_draft_review:{label:'确认用例草稿并继续评审',text:'确认当前用例草稿并继续评审。',subject:'用例草稿'},
 case_result_review:{label:'确认评审建议并修改用例',text:'确认当前评审建议，并按建议修改用例。',subject:'用例评审建议'},
};

// Quick replies are ordinary chat turns, scoped to the current server prompt.
export function ConversationSuggestions({prompt,disabled=false,onChoose,onFill,onViewProfileChange,onViewArtifactChange,hasArtifactPreview=false}:{prompt?:ConversationPrompt|null;disabled?:boolean;onChoose:(text:string,kind?:ReplyKind)=>void;onFill?:(text:string)=>void;onViewProfileChange?:()=>void;onViewArtifactChange?:()=>void;hasArtifactPreview?:boolean}){
 if(!prompt||prompt.busy||prompt.kind==='busy')return null;
 if(prompt.kind==='profile')return onViewProfileChange?<div className="conversation-suggestions" role="group" aria-label="快捷回复" aria-busy={disabled||undefined}><span className="suggestions-hint">点击查看更改，勾选后再确认应用 · 输入框草稿会保留</span><div className="suggestion-group"><span className="suggestions-label">模板建议</span><button type="button" disabled={disabled} onClick={()=>{if(!disabled)onViewProfileChange();}}>查看 Profile 更改</button></div>{hasArtifactPreview&&onViewArtifactChange&&<div className="suggestion-group"><span className="suggestions-label">成果修改</span><button type="button" disabled={disabled} onClick={onViewArtifactChange}>查看修改预览</button></div>}</div>:null;
 if(prompt.kind==='artifact_proposal')return onViewArtifactChange?<div className="conversation-suggestions" role="group" aria-label="快捷回复" aria-busy={disabled||undefined}><span className="suggestions-hint">修改建议已准备好 · 查看差异后确认应用，也可以直接在对话中回复</span><div className="suggestion-group"><span className="suggestions-label">成果修改</span><button type="button" disabled={disabled||!hasArtifactPreview} onClick={()=>{if(!disabled&&hasArtifactPreview)onViewArtifactChange();}}>查看修改预览</button></div></div>:null;
 const unanswered=(prompt.questions??[]).filter(question=>!question.answer?.trim());
 const answerPrefix='仅提交以下澄清答案并更新需求理解，不确认需求理解。';
 const questions:Reply[]=prompt.kind==='clarification'?unanswered.filter(question=>question.suggestion?.trim()).map(question=>({
  id:'question:'+question.id,label:'采用 '+question.id+' 建议：'+question.suggestion,
  text:answerPrefix+'\n'+question.id+'（'+question.question+'）：'+question.suggestion,kind:'clarification',
 })):[];
 const groups:{label:string;replies:Reply[]}[]=[];
 if(questions.length>1&&!unanswered.some(question=>(question.options?.length??0)>1)){
  const all=questions.length>1?[{id:'questions:all',label:questions.length===unanswered.length?'采用全部建议并更新理解':'采用现有 '+questions.length+' 条建议并更新理解',text:answerPrefix+'\n'+questions.map(question=>question.text.slice(answerPrefix.length+1)).join('\n'),kind:'clarification' as const}]:[];
  groups.push({label:'澄清答复',replies:all});
 }
 const review=reviewReplies[prompt.kind];
 if(review&&!unanswered.length)groups.push({label:'流程确认',replies:[{id:'gate:'+prompt.kind,label:review.label,text:review.text,kind:'confirm'}]});
 if(prompt.kind==='clarification'||review){
  const subject=review?.subject;
  groups.push({label:'先询问',replies:[{id:'explain:'+prompt.kind,label:subject?'先解释'+subject:'先解释澄清问题',text:subject?'解释当前'+subject+'，暂不确认也不继续流程。':'解释这些澄清问题的依据，不采用答案也不继续流程。',kind:'question'}]});
 }
 if(!review&&prompt.kind!=='clarification'){
  const choices=(prompt.choices??[]).filter(choice=>choice.title?.trim()).map(choice=>({id:'choice:'+choice.id,label:choice.title,text:choice.title}));
  if(choices.length)groups.push({label:'建议答复',replies:choices});
  else if(prompt.kind==='failed'&&prompt.message.includes('重试当前步骤'))groups.push({label:'恢复流程',replies:[{id:'retry',label:'重试当前步骤',text:'重试当前步骤'}]});
 }
 if(!groups.length)return null;
 return <div className="conversation-suggestions" role="group" aria-label="快捷回复" aria-busy={disabled||undefined}><span className="suggestions-hint">{onFill?'点击发送，或填入输入框 · 草稿会保留':'点击即发送 · 输入框草稿会保留'}</span>{hasArtifactPreview&&onViewArtifactChange&&<div className="suggestion-group"><span className="suggestions-label">修改建议</span><button type="button" disabled={disabled} onClick={onViewArtifactChange}>查看修改预览</button></div>}{groups.map(group=><div className="suggestion-group" role="group" aria-label={group.label} key={group.label}><span className="suggestions-label">{group.label}</span>{group.replies.map(reply=>onFill?<span className="suggestion-actions" key={reply.id}><button type="button" disabled={disabled} aria-label={reply.label} title={reply.label} onClick={()=>{if(!disabled)onChoose(reply.text,reply.kind);}}>{reply.label}</button><button type="button" className="composer-fill-action" disabled={disabled} aria-label={'填入输入框：'+reply.label} title={'填入输入框：'+reply.text} onClick={()=>{if(!disabled)onFill(reply.text);}}>填入</button></span>:<button key={reply.id} type="button" disabled={disabled} aria-label={reply.label} title={reply.label} onClick={()=>{if(!disabled)onChoose(reply.text,reply.kind);}}>{reply.label}</button>)}</div>)}</div>;
}
