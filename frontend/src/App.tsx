import {useCallback,useEffect,useRef,useState} from 'react';
import {ArrowUp,FileText,Folder,Menu,MessageSquare,Paperclip,Plus,Settings as SettingsIcon,X} from 'lucide-react';
import {api,errText} from './api';
import {ArtifactWorkspace} from './ArtifactWorkspace';
import {ArtifactCard} from './ArtifactCard';
import {ChatEstimate} from './ChatEstimate';
import {ConversationParts} from './ConversationParts';
import {ConversationContext,newClientMessageId,postTurn} from './conversation';
import {SettingsDialog} from './SettingsDialog';
import {SourcesDialog} from './SourcesDialog';
import {MemoryDialog} from './MemoryDialog';
import {RunCard} from './RunCard';
import {RunTimeline} from './RunTimeline';
import {StageArtifact} from './StageArtifact';
import {Dialog,ErrorBox,Logo,Spinner} from './ui';
import {useDrafts} from './useDrafts';
import {busy,intents,roles,depthNames} from './types';
import {nextTaskDraft} from './taskPrompts';
import type {Artifact,Chat,Depth,Json,Profile,Project,Settings,Snapshot,TurnRequest} from './types';

export function App(){
 const [openedArtifact,setOpenedArtifact]=useState<Artifact>();const manualFocus=useRef<string|undefined>(undefined);const automaticArtifact=useRef<string|undefined>(undefined);const artifactNavigation=useRef(0);const profileChoice=useRef({version:0,dirty:false,lastServer:''});
 const [settingsTab,setSettingsTab]=useState<'model'|'profile'>('model');const [runOverride,setRunOverride]=useState<Json>();
 const [mobilePanel,setMobilePanel]=useState<'results'|'chat'>('chat');
 const [optionsOpen,setOptionsOpen]=useState(false);const [depth,setDepth]=useState<Depth>('auto');const [mode,setMode]=useState<'auto'|'hitp'>('auto');const [caseTypes,setCaseTypes]=useState(['Business','Negative','Boundary']);const [caseTypesTouched,setCaseTypesTouched]=useState(false);
 const [projectLoading,setProjectLoading]=useState(true);const [projects,setProjects]=useState<Project[]>([]);const [projectId,setProjectId]=useState('');const [chats,setChats]=useState<Chat[]>([]);const [profiles,setProfiles]=useState<Profile[]>([]);const [profileId,setProfileId]=useState('');
 const [chatId,setChatId]=useState('');const [snapshot,setSnapshot]=useState<Snapshot>();const [intent,setIntent]=useState('auto');
 const [artifactEpoch,setArtifactEpoch]=useState(0);const [settings,setSettings]=useState<Settings>();const [settingsOpen,setSettingsOpen]=useState(false);const [sourcesOpen,setSourcesOpen]=useState(false);const [memoryOpen,setMemoryOpen]=useState(false);const [proposal,setProposal]=useState<Json>();
 const [projectDialog,setProjectDialog]=useState(false);const [projectName,setProjectName]=useState('');const [sidebar,setSidebar]=useState(false);const [error,setError]=useState('');const [pendingSendCount,setPendingSendCount]=useState(0);const [uploading,setUploading]=useState(false);const [operationBusy,setOperationBusy]=useState(false);const [projectSharing,setProjectSharing]=useState<Record<string,boolean>>({});const [uploadRole,setUploadRole]=useState('auto');const [uploadResults,setUploadResults]=useState<{name:string;ok:boolean;message:string}[]>([]);
 const draftKey=projectId+':'+(chatId||'new');const {content,target,asRequirement,setContent,setTarget,setAsRequirement,move:moveDraft,clearSubmitted,version:draftVersion}=useDrafts(draftKey);const projectRef=useRef(projectId);projectRef.current=projectId;const nav=useRef(0);const creating=useRef<{project:string;token:number;promise:Promise<string>}|null>(null);const fileInput=useRef<HTMLInputElement>(null);const textarea=useRef<HTMLTextAreaElement>(null);const bottom=useRef<HTMLDivElement>(null);const chatRef=useRef(chatId);chatRef.current=chatId;
 useEffect(()=>{if(!uploadResults.some(result=>result.ok))return;const timer=setTimeout(()=>setUploadResults(previous=>previous.filter(result=>!result.ok)),3000);return()=>clearTimeout(timer);},[uploadResults]);
 const pendingSends=useRef(new Map<string,{clientMessageId:string;inFlight:boolean}>());const sending=pendingSendCount>0;
 const orderedRuns=snapshot?.runs?.slice().sort((a,b)=>String(b.updated_at??'').localeCompare(String(a.updated_at??'')))??[];const latestRun=orderedRuns.find(r=>busy(r));const currentRun=latestRun??(orderedRuns[0]&&['failed','cancelled'].includes(orderedRuns[0].status)?orderedRuns[0]:undefined);const running=busy(latestRun);const refreshKey=snapshot?.runs?.map(r=>r.id+':'+r.updated_at+':'+r.status).join('|')+':'+artifactEpoch+':'+snapshot?.sources.map(s=>s.id+':'+s.role).join('|');
 const displayedMode=latestRun?.mode==='hitp'||latestRun?.mode==='auto'?latestRun.mode:mode;
 const modeLocked=running||sending;
 useEffect(()=>{if(latestRun?.mode==='hitp'||latestRun?.mode==='auto')setMode(latestRun.mode);},[latestRun?.id,latestRun?.mode]);
 const refresh=useCallback(async()=>{if(!chatId)return;try{const state=await api<Snapshot>('/chats/'+chatId);if(chatRef.current===chatId)setSnapshot(state);}catch(e){if(chatRef.current===chatId)setError(errText(e));}},[chatId]);
 const reloadSettings=useCallback(async()=>{const [s,p]=await Promise.all([api<Settings>('/settings'),projectId?api<Profile[]>('/projects/'+projectId+'/profiles'):Promise.resolve([])]);setSettings(s);if(projectRef.current===projectId){setProfiles(p);setProfileId(old=>p.some(x=>x.id===old)?old:p[0]?.id??'');}},[projectId]);
 useEffect(()=>{api<Project[]>('/projects').then(list=>{setProjects(list);setProjectId(list[0]?.id??'');}).catch(e=>setError(errText(e)));api<Settings>('/settings').then(setSettings).catch(e=>setError(errText(e)));},[]);
 useEffect(()=>{if(!projectId)return;let active=true;setProjectLoading(true);setSnapshot(undefined);setChatId('');chatRef.current='';setChats([]);setProfiles([]);setProfileId('');Promise.all([api<Chat[]>('/projects/'+projectId+'/chats'),api<Profile[]>('/projects/'+projectId+'/profiles')]).then(([c,p])=>{if(active){setChats(c);setProfiles(p);setProfileId(p[0]?.id??'');setChatId(c[0]?.id??'');chatRef.current=c[0]?.id??'';}}).catch(e=>active&&setError(errText(e))).finally(()=>{if(active)setProjectLoading(false);});return()=>{active=false;};},[projectId]);
 useEffect(()=>{const selected=snapshot?.chat.profile_id,key=snapshot?.chat.id+':'+selected;if(selected&&profiles.some(profile=>profile.id===selected)&&!profileChoice.current.dirty&&profileChoice.current.lastServer!==key){profileChoice.current.lastServer=key;setProfileId(selected);}},[snapshot?.chat.id,snapshot?.chat.profile_id,profiles]);
 useEffect(()=>{setSnapshot(undefined);setError('');refresh();},[refresh]);
 useEffect(()=>{if(!chatId)return;const timer=setInterval(refresh,7000);return()=>clearInterval(timer);},[chatId,running,refresh]);
 useEffect(()=>{bottom.current?.scrollIntoView({behavior:'smooth',block:'end'});},[chatId,snapshot?.messages.length,latestRun?.interrupt?.type]);
 function selectProject(id:string){profileChoice.current.dirty=false;profileChoice.current.lastServer='';manualFocus.current=undefined;automaticArtifact.current=undefined;setOpenedArtifact(undefined);setProjectLoading(true);nav.current++;projectRef.current=id;chatRef.current='';setProjectId(id);setChatId('');setChats([]);setProfiles([]);setSnapshot(undefined);setSettingsOpen(false);setSourcesOpen(false);}
 function selectChat(id:string){profileChoice.current.dirty=false;profileChoice.current.lastServer='';manualFocus.current=undefined;automaticArtifact.current=undefined;setOpenedArtifact(undefined);nav.current++;chatRef.current=id;setChatId(id);setSidebar(false);setSourcesOpen(false);}
 async function ensureChat(){
  if(projectLoading)throw new Error('正在加载项目，请稍候');
  if(chatRef.current)return chatRef.current;
  const pid=projectRef.current;const token=nav.current;
  if(!pid)throw new Error('项目尚未加载，请稍后重试');
  if(creating.current?.project===pid&&creating.current.token===token)return creating.current.promise;
  const promise=api<Chat>('/projects/'+pid+'/chats',{}).then(chat=>{if(projectRef.current===pid&&nav.current===token){setChats(old=>[chat,...old]);moveDraft(pid+':new',pid+':'+chat.id);chatRef.current=chat.id;setChatId(chat.id);}return chat.id;});
  creating.current={project:pid,token,promise};
  try{return await promise;}finally{if(creating.current?.promise===promise)creating.current=null;}
 }
 async function newChat(){
  if(projectLoading)return;
  const pid=projectRef.current;const token=++nav.current;setError('');manualFocus.current=undefined;automaticArtifact.current=undefined;setOpenedArtifact(undefined);
  try{const chat=await api<Chat>('/projects/'+pid+'/chats',{});if(projectRef.current!==pid||nav.current!==token)return;setChats(old=>[chat,...old]);chatRef.current=chat.id;setChatId(chat.id);setSidebar(false);textarea.current?.focus();}catch(e){if(nav.current===token)setError(errText(e));}
 }
 async function send(generationMessage?:string, requestedIntent?:string, generationArgs?:Json){
  const taskIntent=requestedIntent??intent;
  if(!(generationMessage??content).trim()||projectLoading||uploading)return;
  const pid=projectRef.current;const token=nav.current;const message=generationMessage??content;const submittedVersion=draftVersion;const submittedProfileVersion=profileChoice.current.version;const submittedTarget=generationMessage?undefined:target;
  setMobilePanel(generationMessage?'results':'chat');
  setPendingSendCount(count=>count+1);setError('');let requestSignature='';
  try{
   const id=await ensureChat();
   const artifact=generationArgs?.artifact_id?await api<Artifact>('/artifacts/'+encodeURIComponent(generationArgs.artifact_id)):submittedTarget?.artifact??openedArtifact;
   const options={depth,...(caseTypesTouched?{case_types:caseTypes}:{}),profile_override:runOverride,as_requirement:generationArgs?.as_requirement??asRequirement,experience:'reliable'};
   const readonlyCaseReview=taskIntent==='review_case'&&!generationArgs;
   const body:Omit<TurnRequest,'client_message_id'>={content:message,intent_hint:taskIntent,mode:displayedMode,profile_id:profileId||undefined,...options,...(artifact?{artifact_id:artifact.id,artifact_revision:artifact.revision,view_order:artifact.items.map(item=>String(item.id))}:{}),...(submittedTarget?{selected_ids:submittedTarget.ids}:{}),...(generationMessage?{command:{name:readonlyCaseReview?'artifact.review_cases':'workflow.start',arguments:readonlyCaseReview?{instruction:message}:{intent:taskIntent==='auto'?'generate_case':taskIntent,depth,...(caseTypesTouched?{case_types:caseTypes}:{}),...(runOverride?{profile_override:runOverride}:{}),as_requirement:generationArgs?.as_requirement??asRequirement}}}:{})};
   const signature=JSON.stringify([id,body]);
   const previous=pendingSends.current.get(signature);if(previous?.inFlight)return;
   const pending={clientMessageId:previous?.clientMessageId??newClientMessageId(),inFlight:true};pendingSends.current.set(signature,pending);requestSignature=signature;
   const response=await postTurn(id,{...body,client_message_id:pending.clientMessageId});
   if(response.status==='failed'||response.status==='cancelled')throw new Error(response.message||'本轮操作未完成。');
   if(profileChoice.current.version===submittedProfileVersion)profileChoice.current.dirty=false;
   const saved=(response.parts??[]).filter(part=>part.type==='artifact').at(-1);
   if(saved?.type==='artifact'&&(!manualFocus.current||manualFocus.current===saved.artifact_id)){const latest=await api<Artifact>('/artifacts/'+saved.artifact_id);if(chatRef.current===id&&nav.current===token)setOpenedArtifact(latest);}
   setArtifactEpoch(value=>value+1);
   pendingSends.current.delete(signature);
   setRunOverride(undefined);clearSubmitted(pid+':'+id,submittedVersion);
   const [state,list]=await Promise.all([api<Snapshot>('/chats/'+id),api<Chat[]>('/projects/'+pid+'/chats')]);
   if(chatRef.current===id&&projectRef.current===pid)setSnapshot(state);
   if(projectRef.current===pid)setChats(list);
  }catch(e){if(nav.current===token)setError(errText(e));}finally{if(requestSignature){const pending=pendingSends.current.get(requestSignature);if(pending)pending.inFlight=false;}setPendingSendCount(count=>Math.max(0,count-1));}
 }
 async function upload(files:FileList|null){
  if(!files?.length||uploading)return;const token=nav.current;setUploading(true);setError('');
  try{const id=await ensureChat();const results=[];for(const file of Array.from(files)){const form=new FormData();form.append('file',file);form.append('role',uploadRole);try{await api('/chats/'+id+'/sources',form);results.push({name:file.name,ok:true,message:'上传成功；请在工作区更新需求理解'});}catch(e){results.push({name:file.name,ok:false,message:errText(e)});}}setUploadResults(results);const state=await api<Snapshot>('/chats/'+id);if(chatRef.current===id)setSnapshot(state);}catch(e){if(nav.current===token)setError(errText(e));}finally{setUploading(false);if(fileInput.current)fileInput.current.value='';}
 }
 useEffect(()=>{if(target)textarea.current?.focus();},[target]);
 function selectTarget(artifact:Artifact,ids:string[]){manualFocus.current=artifact.id;setMobilePanel('chat');setTarget({artifact,ids});textarea.current?.focus();}
 async function prepareRunPrompt(text:string,artifactId?:string){
  const currentChat=chatRef.current,token=nav.current;
  try{
   const artifact=artifactId?await api<Artifact>('/artifacts/'+encodeURIComponent(artifactId)):undefined;
   if(chatRef.current!==currentChat||nav.current!==token)return;
   if(artifact){manualFocus.current=artifact.id;setOpenedArtifact(artifact);}
   setTarget(undefined);setContent(text);setMobilePanel('chat');textarea.current?.focus();
  }catch(e){if(chatRef.current===currentChat&&nav.current===token)setError(errText(e));}
 }
 const lastAssistantForRun=new Map(snapshot?.messages.filter(m=>m.role==='assistant'&&m.metadata?.run_id).map(m=>[m.metadata.run_id,m.id]));
 const latestOutput=snapshot?.messages.flatMap(m=>[...(m.metadata?.artifact_ids??[]),...(m.metadata?.turn_response?.parts??[]).filter((part:Json)=>part.type==='artifact').map((part:Json)=>part.artifact_id)]).at(-1) as string|undefined;
 const draftOutput=snapshot?.runs.filter(r=>r.status!=='completed'&&r.draft_artifact_id).at(-1)?.draft_artifact_id;
 const visibleOutput=draftOutput??latestOutput;
 useEffect(()=>{let alive=true;const id=manualFocus.current??visibleOutput??automaticArtifact.current;if(!id){setOpenedArtifact(undefined);return;}api<Artifact>('/artifacts/'+id).then(a=>{if(alive)setOpenedArtifact(old=>['analysis','scenarios','cases'].includes(a.type)?a:old);}).catch(()=>{});return()=>{alive=false;};},[visibleOutput,chatId,refreshKey]);
 const completedRuns=new Set(snapshot?.runs.filter(r=>r.status==='completed').map(r=>r.id));
 const openSettings=(value?:Json,tab:'model'|'profile'='model')=>{setProposal(value);setSettingsTab(tab);setSettingsOpen(true);};
 const openResult=(a:Artifact)=>{manualFocus.current=a.id;setOpenedArtifact(a);setMobilePanel('results');};
 const selectProfile=(value:string)=>{profileChoice.current.version++;profileChoice.current.dirty=true;setProfileId(value);};
 const selectIntent=(value:string)=>{setContent(nextTaskDraft(content,intent,value));setIntent(value);setUploadRole(value==='learn_template'?'example':'auto');textarea.current?.focus();};
 async function selectWorkspaceArtifact(id:string,manual=true){
  const token=nav.current,selection=++artifactNavigation.current;setError('');
  try{const artifact=await api<Artifact>('/artifacts/'+encodeURIComponent(id));if(nav.current===token&&selection===artifactNavigation.current){if(manual)manualFocus.current=id;else automaticArtifact.current=id;setOpenedArtifact(artifact);if(manual){setTarget(undefined);setMobilePanel('results');}}}catch(e){if(nav.current===token)setError(errText(e));}
 }
 const changed=()=>{setArtifactEpoch(x=>x+1);void refresh();};
 return <ConversationContext.Provider value={{chatId,onChanged:changed}}><div className={'app-shell v28 panel-'+mobilePanel+(snapshot?.messages.length?' has-messages':' intake-page')}>
  <div className="mobile-panel-switch"><button className={mobilePanel==='results'?'active':''} onClick={()=>setMobilePanel('results')}>设计工作区</button><button className={mobilePanel==='chat'?'active':''} onClick={()=>setMobilePanel('chat')}>AI 助手</button><button onClick={()=>setSidebar(true)} aria-label="项目导航"><Menu size={18}/></button></div>
  {sidebar&&<button className="sidebar-backdrop" aria-label="收起对话列表" onClick={()=>setSidebar(false)}/>}
  <aside className={'sidebar '+(sidebar?'open':'')}><div className="brand"><Logo/><span>TCG <small>TEST WORKSPACE</small></span><button className="icon-button mobile-only" aria-label="收起侧栏" onClick={()=>setSidebar(false)}><X size={20}/></button></div><button className="primary new-chat" disabled={!projectId||projectLoading} onClick={newChat}><Plus size={18}/>新建生成会话</button><div className="project-select"><Folder size={17}/><select aria-label="当前项目" value={projectId} onChange={e=>selectProject(e.target.value)}>{projects.map(p=><option key={p.id} value={p.id}>{p.name}</option>)}</select><button className="icon-button" aria-label="创建项目" onClick={()=>setProjectDialog(true)}><Plus size={15}/></button></div><div className="nav-section"><button onClick={()=>setMemoryOpen(true)}><MessageSquare size={17}/>项目记忆</button></div><p className="sidebar-label">生成会话</p><nav className="chat-list" aria-label="对话历史">{chats.map(chat=><button key={chat.id} className={chat.id===chatId?'active':''} onClick={()=>selectChat(chat.id)}><MessageSquare size={16}/><span>{chat.title}</span></button>)}{!chats.length&&<p className="muted small-text sidebar-empty">需求、设计与修改记录会保留在会话中。</p>}</nav><button className="settings-button" onClick={()=>openSettings()}><SettingsIcon size={18}/>模型与设置<span className={'connection-dot '+(settings?.model?'ready':'')}/></button></aside>
  <main className="main"><header className="topbar"><div className="inline"><h1>{snapshot?.chat.title||'新的测试设计'}</h1></div><div className="topbar-actions"><button disabled={projectLoading} onClick={async()=>{try{await ensureChat();setSourcesOpen(true);}catch(e){setError(errText(e));}}}><FileText size={16}/>需求资料{snapshot?.sources.length?` · ${snapshot.sources.length}`:''}</button><button onClick={()=>{setOptionsOpen(value=>!value);setMobilePanel('chat');}}><SettingsIcon size={15}/>任务设置</button></div></header>
  <div className="workspace-layout">
   <ArtifactWorkspace chatId={chatId} artifact={openedArtifact} refreshKey={refreshKey} onTarget={selectTarget} onChanged={changed} sourceCount={snapshot?.sources.length??0} running={sending||uploading||operationBusy||projectLoading||!!currentRun&&['queued','running'].includes(currentRun.status)} currentRun={currentRun} onUpload={()=>fileInput.current?.click()} onGenerate={(text,task,args)=>void send(text??'请分析当前需求资料。',task??'review_requirement',args)} onSelectArtifact={(id,manual)=>void selectWorkspaceArtifact(id,manual)} renderRun={currentRun?hideActions=><RunCard run={currentRun} hideArtifact hideActions={hideActions} compact sources={snapshot?.sources??[]} interactionBusy={operationBusy||sending} onSupplementBusy={setOperationBusy} saveToProject={projectSharing[currentRun.id]??true} onSaveToProjectChange={value=>setProjectSharing(old=>({...old,[currentRun.id]:value}))} onChanged={changed} onTarget={selectTarget} onPrompt={prepareRunPrompt} onSettings={()=>openSettings()}/>:undefined}/>
   <section className="chat-panel" aria-label="AI 助手"><header className="chat-panel-header"><MessageSquare size={18}/><strong>AI 助手</strong><span>{displayedMode==='hitp'?'逐步确认':'自动完成'}</span></header>
   <div className="conversation" id="conversation"><div className="conversation-inner">
   {!snapshot?.messages.length?<div className="welcome"><Logo/><h2>一起完善测试设计</h2><p>描述业务、提出问题，或说明你希望调整的地方。</p>{!settings?.model&&<button className="setup-link" onClick={()=>openSettings()}>连接模型，开始设计</button>}</div>:snapshot.messages.map(message=><article className={'message '+message.role} key={message.id}><div className="message-body"><div className="message-text preserve">{message.content}</div>{message.role==='assistant'&&message.metadata?.turn_response&&<ConversationParts compact response={message.metadata.turn_response} refreshKey={refreshKey} onTarget={selectTarget} onOpen={openResult} onChanged={changed}/>}{message.role==='assistant'&&!message.metadata?.turn_response&&message.metadata?.case_estimate&&<details><summary>查看用例数量估算</summary><ChatEstimate estimate={message.metadata.case_estimate}/></details>}{message.metadata?.stage_artifact_id&&<StageArtifact id={message.metadata.stage_artifact_id} revision={message.metadata.stage_revision} stage={message.metadata.stage} onOpenLatest={openResult}/>}{!message.metadata?.turn_response&&message.metadata?.artifact_ids?.map((id:string)=><ArtifactCard key={id} id={id} compact readOnly onOpen={openResult} refreshKey={refreshKey} onTarget={selectTarget} onChanged={changed}/>)}{message.metadata?.refs?.length>0&&<details><summary>查看回答依据</summary><p className="muted small-text">{message.metadata.refs.join(' · ')}</p></details>}{message.metadata?.proposal?.template_kinds?.length>0&&<p className="muted small-text">已识别模板：{message.metadata.proposal.template_kinds.filter((kind:string)=>['scenarios','cases'].includes(kind)).map((kind:string)=>kind==='scenarios'?'场景模板':'用例模板').join('、')}</p>}{message.metadata?.proposal&&<button onClick={()=>openSettings(message.metadata.proposal)}>查看并应用 Profile 建议</button>}{message.role==='assistant'&&completedRuns.has(message.metadata?.run_id)&&lastAssistantForRun.get(message.metadata.run_id)===message.id&&<RunTimeline runId={message.metadata.run_id} initialOpen={false} agentLive={false} agentMode={snapshot.runs.find(r=>r.id===message.metadata.run_id)?.experience==='agent'} initialAgent={snapshot.runs.find(r=>r.id===message.metadata.run_id)?.agent}/>}</div></article>)}<div ref={bottom}/>
   </div></div>
   <div className="composer-wrap"><div className="composer-inner"><ErrorBox message={error}/>{(target||openedArtifact)&&<div className="target-bar"><span>当前对象：{(target?.artifact??openedArtifact)?.title} · v{(target?.artifact??openedArtifact)?.revision}{target?.ids.length?` · 选中 ${target.ids.length} 条`:''}</span>{target&&<button className="icon-button" aria-label="取消修改目标" onClick={()=>setTarget(undefined)}><X size={15}/></button>}</div>}
   {uploadResults.length>0&&<div className="upload-results" aria-label="上传结果">{uploadResults.map((result,index)=><span className={result.ok?'success':'error-text'} key={result.name+index}>{result.name}：{result.message}</span>)}</div>}
   <div className="composer"><div className="composer-input"><textarea ref={textarea} aria-label="聊天输入" rows={2} placeholder={target?'说明如何修改所选条目…':'提问、说明修改，或补充新需求…'} value={content} onChange={e=>setContent(e.target.value)} onKeyDown={e=>{if(e.key==='Enter'&&!e.shiftKey&&!e.nativeEvent.isComposing){e.preventDefault();void send();}}}/><button className="send-button" aria-label="发送消息" disabled={!content.trim()||projectLoading||uploading} onClick={()=>void send()}>{sending?<Spinner/>:<ArrowUp size={21}/>}</button></div><div className="composer-tools"><input ref={fileInput} type="file" multiple accept=".docx,.pdf,.xlsx,.xls,.csv,.txt,.md" hidden onChange={e=>void upload(e.target.files)}/><button className="icon-button attachment-button" disabled={projectLoading||uploading||sending||operationBusy} title="上传需求文档" aria-label="上传需求文档" onClick={()=>fileInput.current?.click()}>{uploading?<Spinner/>:<Paperclip size={19}/>}</button><span className="muted small-text">{target?.ids.length?`已限定 ${target.ids.length} 条`:running?'可随时提问或补充要求':'Enter 发送'}</span></div></div>
   {optionsOpen&&<div className="task-options" aria-label="任务设置"><div className="task-settings-heading"><strong>任务设置</strong><button className="icon-button" aria-label="收起任务设置" onClick={()=>setOptionsOpen(false)}><X size={16}/></button></div><label>Profile<select aria-label="运行 Profile" value={profileId} disabled={running} onChange={e=>selectProfile(e.target.value)}>{profiles.map(p=><option key={p.id} value={p.id}>{p.name}</option>)}</select></label><button onClick={()=>openSettings(undefined,'profile')}>编辑 Profile / Excel 格式</button>{runOverride&&<p className="small-text">本轮使用临时配置 <button onClick={()=>setRunOverride(undefined)}>取消</button></p>}<label>执行方式<select aria-label="运行模式" value={displayedMode} disabled={modeLocked} onChange={e=>setMode(e.target.value as 'auto'|'hitp')}><option value="auto">Auto · 自动完成</option><option value="hitp">Human · 逐步确认</option></select></label><label>测试设计深度<select value={depth} onChange={e=>setDepth(e.target.value as Depth)}>{(['auto','quick','standard','deep'] as Depth[]).map(value=><option key={value} value={value}>{depthNames[value]}</option>)}</select></label><fieldset className="case-type-options"><legend>用例类型</legend>{['Business','Negative','Boundary','Security'].map(value=><label className="check-label" key={value}><input type="checkbox" aria-label={value} checked={caseTypes.includes(value)} onChange={e=>{setCaseTypesTouched(true);setCaseTypes(old=>e.target.checked?[...old,value]:old.filter(x=>x!==value));}}/>{value}</label>)}</fieldset><details><summary>指定任务与资料用途</summary><label>任务目标<select aria-label="任务目标" disabled={!!target} value={intent} onChange={e=>selectIntent(e.target.value)}>{intents.map(([value,label])=><option key={value} value={value}>{label}</option>)}</select></label><label>上传文件角色<select aria-label="附件用途" value={uploadRole} disabled={running} onChange={e=>setUploadRole(e.target.value)}>{Object.entries(roles).map(([value,label])=><option key={value} value={value}>{label}</option>)}</select></label><label className="check-label"><input type="checkbox" aria-label="正文作为需求" checked={asRequirement} disabled={running} onChange={e=>setAsRequirement(e.target.checked)}/>将这条消息保存为需求正文</label></details></div>}
   <p className="composer-note">{latestRun?.status==='waiting'?'任务等待确认，可以先提问或修改。':'Shift + Enter 换行 · 修改与来源记录会保留'}</p>
   </div></div></section>
  </div></main>
  {settingsOpen&&<SettingsDialog initialTab={settingsTab} onUseOnce={setRunOverride} projectId={projectId} profiles={profiles} activeProfileId={profileId} onSelectProfile={selectProfile} proposal={proposal} onClose={()=>setSettingsOpen(false)} onSaved={()=>reloadSettings().catch(e=>setError(errText(e)))}/>}
  {sourcesOpen&&chatId&&<SourcesDialog chatId={chatId} sources={snapshot?.sources??[]} onChanged={changed} onClose={()=>setSourcesOpen(false)}/>}
  {memoryOpen&&projectId&&<MemoryDialog projectId={projectId} onClose={()=>setMemoryOpen(false)}/>}
  {projectDialog&&<Dialog title="创建项目" onClose={()=>setProjectDialog(false)}><label>项目名称<input autoFocus value={projectName} onChange={e=>setProjectName(e.target.value)} placeholder="例如：订单服务"/></label><div className="dialog-actions"><button className="primary" disabled={!projectName.trim()} onClick={async()=>{try{const p=await api<Project>('/projects',{name:projectName.trim()});setProjects(old=>[...old,p]);selectProject(p.id);setProjectDialog(false);setProjectName('');}catch(e){setError(errText(e));}}}>创建项目</button></div></Dialog>}
 </div></ConversationContext.Provider>;
}
