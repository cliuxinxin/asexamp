import {useEffect,useRef,useState} from 'react';
import {api,errText} from './api';
import {notifyDraftChanged,useConversationCommand} from './conversation';
import {hasClarificationAnswer} from './clarificationDraft';
import {ErrorBox,Spinner} from './ui';
import type {ClarificationDraft,Json} from './types';

export function ClarificationDraftEditor({runId,initialDraft,refreshKey,onChanged,disabled=false,summaryOnly=false,saveToProject=true,onSaveToProjectChange,controlVersion,interruptId}:{controlVersion?:number;interruptId?:string;runId:string;initialDraft?:ClarificationDraft;refreshKey?:string;onChanged:()=>void;disabled?:boolean;summaryOnly?:boolean;saveToProject?:boolean;onSaveToProjectChange?:(value:boolean)=>void}){
 const [draft,setDraft]=useState(initialDraft);const [answer,setAnswer]=useState(initialDraft?.answer??'');const [error,setError]=useState('');const [working,setWorking]=useState(false);
 const current=useRef(initialDraft),text=useRef(initialDraft?.answer??''),dirty=useRef(false),dirtyBase=useRef<{revision:number;question_set_version:string}|undefined>(undefined),editVersion=useRef(0),alive=useRef(true),queue=useRef<Promise<any>>(Promise.resolve());const command=useConversationCommand();
 function accept(value:ClarificationDraft,preserveDraft=true){
  if(!alive.current)return;
  if(current.current&&value.revision<current.current.revision)return;
  current.current=value;setDraft(value);
  if(!preserveDraft||!dirty.current){dirty.current=false;dirtyBase.current=undefined;text.current=value.answer??'';setAnswer(text.current);}
 }
 useEffect(()=>{alive.current=true;return()=>{alive.current=false;};},[]);
 useEffect(()=>{
  let active=true;
  const load=()=>api<ClarificationDraft>('/runs/'+encodeURIComponent(runId)+'/clarification-draft').then(value=>{if(active)accept(value);}).catch(e=>{if(active)setError(errText(e));});
  void load();
  const changed=(event:Event)=>{const id=(event as CustomEvent).detail?.runId;if(!id||id===runId)void load();};
  window.addEventListener('tcg:clarification-changed',changed);
  return()=>{active=false;window.removeEventListener('tcg:clarification-changed',changed);};
 },[runId,refreshKey]);
 function patch(body:Json){
  const request=queue.current.catch(()=>{}).then(async()=>{
   if(!current.current)throw new Error('澄清草稿尚未加载。');
   const revision=editVersion.current;
   const value=await api<ClarificationDraft>('/runs/'+encodeURIComponent(runId)+'/clarification-draft',{expected_revision:body.answer!==undefined&&dirtyBase.current?dirtyBase.current.revision:current.current.revision,question_set_version:body.answer!==undefined&&dirtyBase.current?dirtyBase.current.question_set_version:current.current.question_set_version,...body},'PATCH');
   if(revision===editVersion.current){dirty.current=false;if(current.current&&current.current.revision>value.revision)accept(current.current,false);}
   else if(!current.current||current.current.revision<=value.revision)dirtyBase.current={revision:value.revision,question_set_version:value.question_set_version};
   accept(value);notifyDraftChanged(runId);return value;
  });
  queue.current=request;return request;
 }
 async function flush(){await queue.current.catch(()=>{});if(dirty.current)return patch({answer:text.current});return current.current;}
 async function persist(){if(!dirty.current||disabled)return;setError('');try{await flush();}catch(e){setError(errText(e));}}
 async function adopt(ids?:string[]){setWorking(true);setError('');try{await flush();await patch(ids?{adopt_ids:ids}:{adopt_all:true});onChanged();}catch(e){setError(errText(e));}finally{setWorking(false);}}
 async function execute(name:string,binding:Json={}){
  const value=current.current;
  const result=await command({name,arguments:{run_id:runId,...binding,...(value&&name.startsWith('clarification.')?{expected_revision:value.revision,question_set_version:value.question_set_version}:{})}},({'clarification.save':'保存澄清答案，先别继续','clarification.share':'将已保存澄清共享到项目','workflow.continue':'按已保存澄清继续当前任务'} as Record<string,string>)[name]??name);
  const updated=result.parts?.find(part=>part.type==='clarification_draft');if(updated?.type==='clarification_draft')accept(updated.draft);
  if(result.status!=='succeeded'&&result.status!=='deferred')throw new Error(result.message);
  onChanged();return result;
 }
 async function action(name:string){setWorking(true);setError('');try{await flush();await execute(name);}catch(e){setError(errText(e));}finally{setWorking(false);}}
 async function continueRun(){const binding={...(controlVersion!==undefined?{expected_control_version:controlVersion}:{}),...(interruptId?{interrupt_id:interruptId}:{})};setWorking(true);setError('');try{await flush();if(!current.current?.submitted)await execute('clarification.save');if(saveToProject&&!current.current?.shared)await execute('clarification.share');await execute('workflow.continue',binding);}catch(e){setError(errText(e));}finally{setWorking(false);}}
 if(!draft)return <div className="clarification"><ErrorBox message={error}/>{!error&&<p><Spinner/>正在读取澄清草稿…</p>}</div>;
 const unanswered=draft.questions.filter(item=>dirty.current?!hasClarificationAnswer(answer,item.question):!item.answer?.trim());
 if(summaryOnly)return <section className="clarification-draft-summary" aria-label="澄清草稿状态"><strong>澄清草稿 · v{draft.revision}</strong><p>{draft.submitted?'答案已保存':'答案尚未提交'}{draft.shared?' · 已共享到项目':''}</p><p className="preserve">{draft.answer}</p></section>;
 const locked=disabled||working;
 return <div className="clarification"><p className="muted small-text">草稿 v{draft.revision} · {draft.submitted?'答案已保存':'采用和编辑只更新草稿'}{draft.shared?' · 已共享到项目':''}</p>
  {unanswered.length>0&&<><p>以下信息会影响测试设计，请补充后继续。</p><button disabled={locked} onClick={()=>void adopt()}>采用全部建议</button><ol>{unanswered.map(item=><li key={item.id}><p>{item.question}</p><div className="answer-suggestion"><strong>{item.suggestion.confidence==='supported'?'需求中已有答案':'建议假设'}</strong><p>{item.suggestion.answer}</p><small>依据：{item.suggestion.basis}</small>{!!item.suggestion.refs?.length&&<small className="preserve">引用：{item.suggestion.refs.join(' · ')}</small>}<button disabled={locked} onClick={()=>void adopt([item.id])}>采用此答案</button></div></li>)}</ol><p className="muted small-text">采用后问题将从上方移除，答案保留在下方草稿中，可修改后提交。</p></>}
  <textarea aria-label="回答澄清问题" value={answer} disabled={locked} onChange={event=>{if(!dirty.current&&current.current)dirtyBase.current={revision:current.current.revision,question_set_version:current.current.question_set_version};dirty.current=true;editVersion.current++;text.current=event.target.value;setAnswer(event.target.value);}} onBlur={()=>void persist()} placeholder="输入你的确认或补充说明…" rows={4}/>
  <ErrorBox message={error}/>{error&&<div className="actions"><button disabled={locked} onClick={()=>void persist()}>重试保存草稿</button><button disabled={locked} onClick={async()=>{try{accept(await api<ClarificationDraft>('/runs/'+encodeURIComponent(runId)+'/clarification-draft'),false);setError('');}catch(e){setError(errText(e));}}}>载入最新草稿（放弃本地修改）</button></div>}
  <div className="actions"><button disabled={locked||!answer.trim()} onClick={()=>void action('clarification.save')}>保存答案</button><button disabled={locked||!draft.submitted||dirty.current||!!draft.shared} onClick={()=>void action('clarification.share')}>共享到项目</button></div>
  <label className="check-label"><input type="checkbox" aria-label="保存澄清到项目" checked={saveToProject} disabled={locked} onChange={event=>onSaveToProjectChange?.(event.target.checked)}/>提交并继续时，同时保存到项目</label>
  <button className="primary" disabled={locked||!answer.trim()} onClick={()=>void continueRun()}>{working?<Spinner/>:null}提交并继续</button>
 </div>;
}
