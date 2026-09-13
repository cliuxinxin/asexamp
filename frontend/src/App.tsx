import {useCallback,useEffect,useLayoutEffect,useRef,useState} from 'react';
import {ArrowUp,FileText,Folder,Menu,MessageSquare,Paperclip,Plus,Settings as SettingsIcon,X} from 'lucide-react';
import {api,errText} from './api';
import {ArtifactCard} from './ArtifactCard';
import {ChatEstimate} from './ChatEstimate';
import {ConversationParts} from './ConversationParts';
import {ConversationPrompt} from './ConversationPrompt';
import {ConversationSuggestions} from './ConversationSuggestions';
import {withLocalTurnReceipts} from './chatFeedback';
import type {LocalTurnReceipt} from './chatFeedback';
import {ConversationContext,newClientMessageId,postTurn} from './conversation';
import {SettingsDialog} from './SettingsDialog';
import {SourcesDialog} from './SourcesDialog';
import {MemoryDialog} from './MemoryDialog';
import {ProjectFacts} from './ProjectSamples';
import {RunTimeline} from './RunTimeline';
import {StageArtifact} from './StageArtifact';
import {Dialog,ErrorBox,Logo,Spinner} from './ui';
import {useDrafts} from './useDrafts';
import {busy,intents,roles,depthNames} from './types';
import {nextTaskDraft} from './taskPrompts';
import {WorkflowSummary} from './WorkflowSummary';
import {ChangePreview} from './ArtifactActions';
import type {Artifact,Chat,ConversationPrompt as Prompt,Depth,Json,Profile,Project,Settings,Snapshot,TurnRequest,TurnResponse} from './types';

export function App(){
 const [viewer,setViewer]=useState<{artifact:Artifact;historical:boolean}>();const viewerRequest=useRef(0);const profileChoice=useRef({version:0,dirty:false,lastServer:''});
 const [viewedTable,setViewedTable]=useState<{artifact:Artifact;viewOrder:string[]}>();
 const [selectionResetKey,setSelectionResetKey]=useState(0);
 const [settingsTab,setSettingsTab]=useState<'model'|'profile'>('model');const [runOverride,setRunOverride]=useState<Json>();
 const [workspaceProposal,setWorkspaceProposal]=useState<Json>();const [nearBottom,setNearBottom]=useState(true);const [newActivity,setNewActivity]=useState(false);
 const [sidebarCollapsed,setSidebarCollapsed]=useState(false);const conversationScroll=useRef<HTMLDivElement>(null);const viewerScroll=useRef(0);const viewerFocus=useRef<HTMLElement|null>(null);
 const [optionsOpen,setOptionsOpen]=useState(false);const [depth,setDepth]=useState<Depth>('auto');const [mode,setMode]=useState<'auto'|'hitp'>('auto');const [caseTypes,setCaseTypes]=useState(['Business','Negative','Boundary']);const [caseTypesTouched,setCaseTypesTouched]=useState(false);
 const [projectLoading,setProjectLoading]=useState(true);const [projects,setProjects]=useState<Project[]>([]);const [projectId,setProjectId]=useState('');const [chats,setChats]=useState<Chat[]>([]);const [profiles,setProfiles]=useState<Profile[]>([]);const [profileId,setProfileId]=useState('');
 const [chatId,setChatId]=useState('');const [snapshot,setSnapshot]=useState<Snapshot>();const [intent,setIntent]=useState('auto');
 const [artifactEpoch,setArtifactEpoch]=useState(0);const [settings,setSettings]=useState<Settings>();const [settingsOpen,setSettingsOpen]=useState(false);const [sourcesOpen,setSourcesOpen]=useState(false);const [memoryOpen,setMemoryOpen]=useState(false);const [proposal,setProposal]=useState<Json>();
 const [projectDialog,setProjectDialog]=useState(false);const [projectName,setProjectName]=useState('');const [sidebar,setSidebar]=useState(false);const [error,setError]=useState('');const [pendingSendCount,setPendingSendCount]=useState(0);const [uploading,setUploading]=useState(false);const [operationBusy,setOperationBusy]=useState(false);const [projectSharing,setProjectSharing]=useState<Record<string,boolean>>({});const [uploadRole,setUploadRole]=useState('auto');const [uploadResults,setUploadResults]=useState<{name:string;ok:boolean;message:string}[]>([]);
 const [localReceipts,setLocalReceipts]=useState<Record<string,LocalTurnReceipt[]>>({});
 const messages=withLocalTurnReceipts(snapshot?.chat.id===chatId?snapshot.messages:[],localReceipts[chatId]??[]);
 const draftKey=projectId+':'+(chatId||'new');const {content,target,asRequirement,setContent,setTarget,setAsRequirement,move:moveDraft,clearSubmitted,version:draftVersion}=useDrafts(draftKey);const draftContent=useRef(content);draftContent.current=content;const targetRef=useRef(target);targetRef.current=target;const projectRef=useRef(projectId);projectRef.current=projectId;const nav=useRef(0);const creating=useRef<{project:string;token:number;promise:Promise<string>}|null>(null);const fileInput=useRef<HTMLInputElement>(null);const textarea=useRef<HTMLTextAreaElement>(null);const bottom=useRef<HTMLDivElement>(null);const chatRef=useRef(chatId);chatRef.current=chatId;
 useEffect(()=>{if(!uploadResults.some(result=>result.ok))return;const timer=setTimeout(()=>setUploadResults(previous=>previous.filter(result=>!result.ok)),3000);return()=>clearTimeout(timer);},[uploadResults]);
 useLayoutEffect(()=>{const input=textarea.current;if(!input)return;input.style.height='48px';input.style.height=Math.min(144,Math.max(48,input.scrollHeight))+'px';},[content]);
 useEffect(()=>{viewerRequest.current++;setViewer(undefined);setViewedTable(undefined);setWorkspaceProposal(undefined);setNewActivity(false);setNearBottom(true);},[chatId,projectId]);
 // Keep every concurrent reply bound to the prompt the user has actually seen.
 const promptAnchors=useRef(new Map<string,{prompt?:Prompt|null;count:number}>());
 const quickSending=useRef(false);
 const pendingSends=useRef(new Map<string,{clientMessageId:string;inFlight:boolean}>());const sending=pendingSendCount>0;
 const orderedRuns=snapshot?.runs?.slice().sort((a,b)=>String(b.updated_at??'').localeCompare(String(a.updated_at??'')))??[];const latestRun=orderedRuns.find(r=>busy(r));const currentRun=latestRun??(orderedRuns[0]&&['failed','cancelled'].includes(orderedRuns[0].status)?orderedRuns[0]:undefined);const running=busy(latestRun);const refreshKey=snapshot?.runs?.map(r=>r.id+':'+r.updated_at+':'+r.status).join('|')+':'+artifactEpoch+':'+snapshot?.sources.map(s=>s.id+':'+s.role).join('|');
 const displayedPrompt=promptAnchors.current.has(chatId)?promptAnchors.current.get(chatId)?.prompt:snapshot?.conversation_prompt;
 const displayedMode=latestRun?.mode==='hitp'||latestRun?.mode==='auto'?latestRun.mode:mode;
 const modeLocked=running||sending;
 useEffect(()=>{if(latestRun?.mode==='hitp'||latestRun?.mode==='auto')setMode(latestRun.mode);},[latestRun?.id,latestRun?.mode]);
 const refresh=useCallback(async(quiet=false)=>{if(!chatId)return;try{const state=await api<Snapshot>('/chats/'+chatId);if(chatRef.current===chatId)setSnapshot(state);}catch(e){if(!quiet&&chatRef.current===chatId)setError(errText(e));}},[chatId]);
 const reloadSettings=useCallback(async()=>{const [s,p]=await Promise.all([api<Settings>('/settings'),projectId?api<Profile[]>('/projects/'+projectId+'/profiles'):Promise.resolve([])]);setSettings(s);if(projectRef.current===projectId){setProfiles(p);setProfileId(old=>p.some(x=>x.id===old)?old:p[0]?.id??'');}},[projectId]);
 useEffect(()=>{api<Project[]>('/projects').then(list=>{setProjects(list);setProjectId(list[0]?.id??'');}).catch(e=>setError(errText(e)));api<Settings>('/settings').then(setSettings).catch(e=>setError(errText(e)));},[]);
 useEffect(()=>{if(!projectId)return;let active=true;setProjectLoading(true);setSnapshot(undefined);setChatId('');chatRef.current='';setChats([]);setProfiles([]);setProfileId('');Promise.all([api<Chat[]>('/projects/'+projectId+'/chats'),api<Profile[]>('/projects/'+projectId+'/profiles')]).then(([c,p])=>{if(active){setChats(c);setProfiles(p);setProfileId(p[0]?.id??'');setChatId(c[0]?.id??'');chatRef.current=c[0]?.id??'';}}).catch(e=>active&&setError(errText(e))).finally(()=>{if(active)setProjectLoading(false);});return()=>{active=false;};},[projectId]);
 useEffect(()=>{const selected=snapshot?.chat.profile_id,key=snapshot?.chat.id+':'+selected;if(selected&&profiles.some(profile=>profile.id===selected)&&!profileChoice.current.dirty&&profileChoice.current.lastServer!==key){profileChoice.current.lastServer=key;setProfileId(selected);}},[snapshot?.chat.id,snapshot?.chat.profile_id,profiles]);
 useEffect(()=>{setSnapshot(undefined);setError('');refresh();},[refresh]);
 useEffect(()=>{if(!chatId)return;let alive=true;api<Json>('/chats/'+chatId+'/workspace-state').then(value=>{if(alive)setWorkspaceProposal(value.pending_proposal);}).catch(()=>{if(alive)setWorkspaceProposal(undefined);});return()=>{alive=false;};},[chatId,refreshKey]);
 useEffect(()=>{if(!chatId)return;const timer=setInterval(()=>void refresh(true),7000);return()=>clearInterval(timer);},[chatId,running,refresh]);
 useEffect(()=>{if(viewer)return;if(nearBottom){bottom.current?.scrollIntoView({behavior:'smooth',block:'end'});setNewActivity(false);}else setNewActivity(true);},[chatId,messages.length,latestRun?.updated_at,displayedPrompt?.id]);
 function selectProject(id:string){profileChoice.current.dirty=false;profileChoice.current.lastServer='';viewerRequest.current++;setViewer(undefined);setViewedTable(undefined);setWorkspaceProposal(undefined);setNewActivity(false);setProjectLoading(true);nav.current++;projectRef.current=id;chatRef.current='';setProjectId(id);setChatId('');setChats([]);setProfiles([]);setSnapshot(undefined);setSettingsOpen(false);setSourcesOpen(false);}
 function selectChat(id:string){profileChoice.current.dirty=false;profileChoice.current.lastServer='';viewerRequest.current++;setViewer(undefined);setViewedTable(undefined);setWorkspaceProposal(undefined);setNewActivity(false);nav.current++;chatRef.current=id;setChatId(id);setSidebar(false);setSourcesOpen(false);}
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
  const pid=projectRef.current;const token=++nav.current;viewerRequest.current++;setError('');setViewer(undefined);setViewedTable(undefined);setWorkspaceProposal(undefined);setNewActivity(false);
  try{const chat=await api<Chat>('/projects/'+pid+'/chats',{});if(projectRef.current!==pid||nav.current!==token)return;setChats(old=>[chat,...old]);chatRef.current=chat.id;setChatId(chat.id);setSidebar(false);textarea.current?.focus();}catch(e){if(nav.current===token)setError(errText(e));}
 }
 function saveLocalReceipt(id:string,receipt:LocalTurnReceipt){setLocalReceipts(old=>({...old,[id]:[...(old[id]??[]).filter(value=>value.clientMessageId!==receipt.clientMessageId),receipt]}));}
 async function refreshAfterFailure(id:string,pid:string){
  const results=await Promise.allSettled([api<Snapshot>('/chats/'+id),api<Chat[]>('/projects/'+pid+'/chats')]);
  if(results[0].status==='fulfilled'&&chatRef.current===id&&projectRef.current===pid)setSnapshot(results[0].value);
  if(results[1].status==='fulfilled'&&projectRef.current===pid)setChats(results[1].value);
 }
 async function send(generationMessage?:string, requestedIntent?:string, generationArgs?:Json,quickReply?:{text:string;promptId:string;kind?:TurnRequest['reply_kind']}){
  const taskIntent=quickReply?'auto':requestedIntent??intent;
  if(!(quickReply?.text??generationMessage??content).trim()||projectLoading||uploading)return;
  if(quickReply&&(quickSending.current||sending||operationBusy||snapshot?.chat.id!==chatRef.current||snapshot?.conversation_prompt?.id!==quickReply.promptId||displayedPrompt?.id!==quickReply.promptId||displayedPrompt.busy||latestRun?.edit_in_progress||latestRun?.status==='queued'||latestRun?.status==='running'||Array.from(pendingSends.current.values()).some(send=>send.inFlight)))return;
  if(quickReply)quickSending.current=true;
  const submittedPrompt=displayedPrompt;const promptChat=chatRef.current;
  if(promptChat){const anchor=promptAnchors.current.get(promptChat);promptAnchors.current.set(promptChat,{prompt:anchor?.prompt??submittedPrompt,count:(anchor?.count??0)+1});}
  const pid=projectRef.current;const token=nav.current;const message=quickReply?.text??generationMessage??content;const submittedVersion=draftVersion;const submittedProfileVersion=profileChoice.current.version;const submittedTarget=generationMessage||quickReply?undefined:target;
  setPendingSendCount(count=>count+1);setError('');let requestSignature='',requestChat='',clientMessageId='';let receivedResponse:TurnResponse|undefined;const startedAt=new Date().toISOString();
  try{
   const id=await ensureChat();
   const artifact=generationArgs?.artifact_id?await api<Artifact>('/artifacts/'+encodeURIComponent(generationArgs.artifact_id)):submittedTarget?.artifact;
   const options={depth,...(caseTypesTouched?{case_types:caseTypes}:{}),profile_override:runOverride,as_requirement:quickReply?false:generationArgs?.as_requirement??asRequirement,experience:'reliable'};
   const readonlyCaseReview=taskIntent==='review_case'&&!generationArgs;
   const explicitInitialTask=!quickReply&&!submittedPrompt&&!generationMessage&&!artifact&&!submittedTarget&&!sending&&!Array.from(pendingSends.current.values()).some(send=>send.inFlight)&&snapshot?.chat.id===id&&snapshot.runs.length===0&&['review_requirement','generate_scenario','generate_case'].includes(taskIntent);
   const body:Omit<TurnRequest,'client_message_id'>={content:message,...(submittedPrompt?{reply_to:submittedPrompt.id}:{}),...(quickReply?.kind?{reply_kind:quickReply.kind}:{}),intent_hint:taskIntent,mode:displayedMode,profile_id:profileId||undefined,...options,...(artifact?{artifact_id:artifact.id,artifact_revision:artifact.revision}:{}),...(submittedTarget?.viewOrder?{view_order:submittedTarget.viewOrder}:{}),...(submittedTarget?.ids.length?{selected_ids:submittedTarget.ids}:{}),...((generationMessage||explicitInitialTask)?{command:{name:readonlyCaseReview?'artifact.review_cases':'workflow.start',arguments:readonlyCaseReview?{instruction:message}:{intent:taskIntent==='auto'?'generate_case':taskIntent,depth,...(caseTypesTouched?{case_types:caseTypes}:{}),...(runOverride?{profile_override:runOverride}:{}),as_requirement:generationArgs?.as_requirement??asRequirement}}}:{})};
   const signature=JSON.stringify([id,body]);
   const previous=pendingSends.current.get(signature);if(previous?.inFlight)return;
   const pending={clientMessageId:previous?.clientMessageId??newClientMessageId(),inFlight:true};pendingSends.current.set(signature,pending);requestSignature=signature;requestChat=id;clientMessageId=pending.clientMessageId;
   const response=await postTurn(id,{...body,client_message_id:pending.clientMessageId});
   receivedResponse=response;
   if(response.status==='failed'||response.status==='cancelled'){
    saveLocalReceipt(id,{clientMessageId,content:message,createdAt:startedAt,response});
    pendingSends.current.delete(signature);setArtifactEpoch(value=>value+1);
    await refreshAfterFailure(id,pid);return;
   }
   setLocalReceipts(old=>({...old,[id]:(old[id]??[]).filter(value=>value.clientMessageId!==clientMessageId)}));
   if(profileChoice.current.version===submittedProfileVersion)profileChoice.current.dirty=false;
   if(!quickReply)setRunOverride(undefined);if(!generationMessage&&!quickReply)clearSubmitted(pid+':'+id,submittedVersion,true);
   const saved=(response.parts??[]).filter(part=>part.type==='artifact').at(-1);
   if(saved?.type==='artifact'&&(viewedTable?.artifact.id===saved.artifact_id||submittedTarget?.artifact.id===saved.artifact_id)){const latest=await api<Artifact>('/artifacts/'+encodeURIComponent(saved.artifact_id));if(chatRef.current===id&&nav.current===token){setViewedTable(previous=>previous?.artifact.id===latest.id?{artifact:latest,viewOrder:previous.viewOrder.filter(rowId=>latest.items.some(item=>String(item.id)===rowId))}:previous);const liveTarget=targetRef.current;if(liveTarget?.artifact.id===latest.id)setTarget({...liveTarget,artifact:latest,ids:liveTarget.ids.filter(rowId=>latest.items.some(item=>String(item.id)===rowId))});}}
   setArtifactEpoch(value=>value+1);
   const [state,list]=await Promise.all([api<Snapshot>('/chats/'+id),api<Chat[]>('/projects/'+pid+'/chats')]);
   if(chatRef.current===id&&projectRef.current===pid)setSnapshot(state);
   if(projectRef.current===pid)setChats(list);
   pendingSends.current.delete(signature);
  }catch(e){if(requestChat&&!receivedResponse){saveLocalReceipt(requestChat,{clientMessageId,content:message,createdAt:startedAt,error:errText(e)});await refreshAfterFailure(requestChat,pid);}else if(nav.current===token)setError(errText(e));}finally{if(quickReply)quickSending.current=false;if(promptChat){const anchor=promptAnchors.current.get(promptChat);if(anchor&&anchor.count>1)anchor.count--;else promptAnchors.current.delete(promptChat);}if(requestSignature){const pending=pendingSends.current.get(requestSignature);if(pending)pending.inFlight=false;}setPendingSendCount(count=>Math.max(0,count-1));}
 }
 async function upload(files:FileList|null){
  if(!files?.length||uploading)return;const token=nav.current;setUploading(true);setError('');
  try{const id=await ensureChat();const results=[];for(const file of Array.from(files)){const form=new FormData();form.append('file',file);form.append('role',uploadRole);try{await api('/chats/'+id+'/sources',form);results.push({name:file.name,ok:true,message:'已上传；可告诉 AI 这份资料的用途和要更新的对象'});}catch(e){results.push({name:file.name,ok:false,message:errText(e)});}}setUploadResults(results);const state=await api<Snapshot>('/chats/'+id);if(chatRef.current===id)setSnapshot(state);}catch(e){if(nav.current===token)setError(errText(e));}finally{setUploading(false);if(fileInput.current)fileInput.current.value='';}
 }
 useEffect(()=>{if(target)textarea.current?.focus();},[target]);
 function selectTarget(artifact:Artifact,ids:string[],viewOrder?:string[]){setTarget({artifact,ids,viewOrder});textarea.current?.focus();}
 function trackSelection(artifact:Artifact,ids:string[],viewOrder:string[]){setViewedTable({artifact,viewOrder});setTarget(ids.length?{artifact,ids,viewOrder}:undefined);}
 async function prepareRunPrompt(text:string,artifactId?:string){
  const currentChat=chatRef.current,token=nav.current;
  try{
   const artifact=artifactId?await api<Artifact>('/artifacts/'+encodeURIComponent(artifactId)):undefined;
   if(chatRef.current!==currentChat||nav.current!==token)return;
   if(artifact)setTarget({artifact,ids:[],viewOrder:artifact.items.map(item=>String(item.id))});
   setTarget(undefined);setContent(draftContent.current.trim()?draftContent.current+'\n\n'+text:text);textarea.current?.focus();
  }catch(e){if(chatRef.current===currentChat&&nav.current===token)setError(errText(e));}
 }
 const lastAssistantForRun=new Map(snapshot?.messages.filter(m=>m.role==='assistant'&&m.metadata?.run_id).map(m=>[m.metadata.run_id,m.id]));
 const latestOutput=snapshot?.messages.flatMap(m=>[...(m.metadata?.artifact_ids??[]),...(m.metadata?.turn_response?.parts??[]).filter((part:Json)=>part.type==='artifact').map((part:Json)=>part.artifact_id)]).at(-1) as string|undefined;
 const draftOutput=snapshot?.runs.filter(r=>r.status!=='completed'&&r.draft_artifact_id).at(-1)?.draft_artifact_id;
 const visibleOutput=draftOutput??latestOutput;
 const completedRuns=new Set(snapshot?.runs.filter(r=>r.status==='completed').map(r=>r.id));
 const openSettings=(value?:Json,tab:'model'|'profile'='model')=>{setProposal(value);setSettingsTab(tab);setSettingsOpen(true);};
 const openResult=(a:Artifact)=>{viewerRequest.current++;viewerScroll.current=conversationScroll.current?.scrollTop??0;viewerFocus.current=document.activeElement as HTMLElement;setViewer({artifact:a,historical:true});};
 const closeViewer=()=>{viewerRequest.current++;setViewer(undefined);const restore=()=>{if(conversationScroll.current)conversationScroll.current.scrollTop=viewerScroll.current;viewerFocus.current?.focus();};if(typeof window.requestAnimationFrame==='function')window.requestAnimationFrame(restore);else setTimeout(restore,0);};
 const openCurrentResult=async()=>{if(!visibleOutput)return;const request=++viewerRequest.current,currentChat=chatId;viewerScroll.current=conversationScroll.current?.scrollTop??0;viewerFocus.current=document.activeElement as HTMLElement;try{const artifact=await api<Artifact>('/artifacts/'+encodeURIComponent(visibleOutput));if(request===viewerRequest.current&&chatRef.current===currentChat)setViewer({artifact,historical:false});}catch(e){if(request===viewerRequest.current&&chatRef.current===currentChat)setError(errText(e));}};
 const openLatestFromHistory=async(id:string)=>{const request=++viewerRequest.current,currentChat=chatId,token=nav.current;try{const artifact=await api<Artifact>('/artifacts/'+encodeURIComponent(id));if(request===viewerRequest.current&&chatRef.current===currentChat&&nav.current===token)setViewer({artifact,historical:false});}catch(e){if(request===viewerRequest.current&&chatRef.current===currentChat)setError(errText(e));}};
 const selectProfile=(value:string)=>{profileChoice.current.version++;profileChoice.current.dirty=true;setProfileId(value);};
 const selectIntent=(value:string)=>{const input=textarea.current;setContent(nextTaskDraft(content,intent,value,input?.selectionStart,input?.selectionEnd));setIntent(value);setUploadRole(value==='learn_template'?'example':'auto');textarea.current?.focus();};
 const changed=()=>{setArtifactEpoch(x=>x+1);void refresh();};
 return <ConversationContext.Provider value={{chatId,onChanged:changed}}><div className={'app-shell v28 v29'+(sidebarCollapsed?' sidebar-collapsed':'')+(messages.length?' has-messages':' intake-page')}>
  {sidebar&&<button className="sidebar-backdrop" aria-label="收起对话列表" onClick={()=>setSidebar(false)}/>}
  <aside className={'sidebar '+(sidebar?'open':'')}><div className="brand"><Logo/><span>TCG <small>TEST WORKSPACE</small></span><button className="icon-button sidebar-toggle" aria-label="收起侧栏" onClick={()=>{setSidebarCollapsed(true);setSidebar(false);}}><X size={20}/></button></div><button className="primary new-chat" disabled={!projectId||projectLoading} onClick={newChat}><Plus size={18}/>新建生成会话</button><div className="project-select"><Folder size={17}/><select aria-label="当前项目" value={projectId} onChange={e=>selectProject(e.target.value)}>{projects.map(p=><option key={p.id} value={p.id}>{p.name}</option>)}</select><button className="icon-button" aria-label="创建项目" onClick={()=>setProjectDialog(true)}><Plus size={15}/></button></div><div className="nav-section"><button onClick={()=>setMemoryOpen(true)}><MessageSquare size={17}/>项目记忆</button>{projectId&&<ProjectFacts projectId={projectId}/>}</div><p className="sidebar-label">生成会话</p><nav className="chat-list" aria-label="对话历史">{chats.map(chat=><button key={chat.id} className={chat.id===chatId?'active':''} onClick={()=>selectChat(chat.id)}><MessageSquare size={16}/><span>{chat.title}</span></button>)}{!chats.length&&<p className="muted small-text sidebar-empty">需求、设计与修改记录会保留在会话中。</p>}</nav><button className="settings-button" onClick={()=>openSettings()}><SettingsIcon size={18}/>模型与设置<span className={'connection-dot '+(settings?.model?'ready':'')}/></button></aside>
  <main className="main"><header className="topbar"><div className="inline"><button className="icon-button open-sidebar" aria-label="展开侧栏" onClick={()=>{setSidebarCollapsed(false);setSidebar(true);}}><Menu size={19}/></button><span className="project-breadcrumb">{projects.find(project=>project.id===projectId)?.name}<span>/</span></span><h1>{snapshot?.messages.length?snapshot.chat.title:'生成测试用例'}</h1></div><div className="topbar-actions"><span className="run-mode">{displayedMode==='hitp'?'逐步确认':'自动完成'}</span><button disabled={projectLoading} onClick={async()=>{try{await ensureChat();setSourcesOpen(true);}catch(e){setError(errText(e));}}}><FileText size={16}/>需求资料{snapshot?.sources.length?` · ${snapshot.sources.length}`:''}</button></div></header>
  <div className="workspace-layout">

   <section className="chat-panel" aria-label="AI 助手">
   <div className="conversation" id="conversation" role="region" aria-label="测试设计对话" ref={conversationScroll} onScroll={e=>{const node=e.currentTarget;const close=node.scrollHeight-node.scrollTop-node.clientHeight<80;setNearBottom(close);if(close)setNewActivity(false);}}><div className="conversation-inner">
   {!messages.length?<div className="welcome"><Logo/><h2>你想测试什么？</h2><p>描述功能、粘贴需求，或上传一份文档。<br/>一起理解业务、确认场景，再生成可追溯的测试用例。</p>{!settings?.model&&<button className="setup-link" onClick={()=>openSettings()}>首次使用，先连接一个模型</button>}</div>:messages.map(message=>{const responseArtifacts=(message.metadata?.turn_response?.parts??[]).filter((part:Json)=>part.type==='artifact').map((part:Json)=>part.artifact_id+':'+part.revision);const stageDuplicate=responseArtifacts.includes(message.metadata?.stage_artifact_id+':'+message.metadata?.stage_revision);return <article className={'message '+message.role} key={message.id}><div className="message-body"><div className="message-text preserve">{message.content}</div>{message.metadata?.local_failure&&<p className="muted small-text">尚未确认服务器是否完成处理。输入已保留，请检查当前对话后手动重试。</p>}{message.role==='assistant'&&message.metadata?.turn_response&&<ConversationParts chatOnly compact response={message.metadata.turn_response} refreshKey={refreshKey} onTarget={selectTarget} onOpen={openResult} onChanged={changed}/>}{message.role==='assistant'&&!message.metadata?.turn_response&&message.metadata?.case_estimate&&<details><summary>查看用例数量估算</summary><ChatEstimate estimate={message.metadata.case_estimate}/></details>}{message.metadata?.stage_artifact_id&&!stageDuplicate&&<StageArtifact id={message.metadata.stage_artifact_id} revision={message.metadata.stage_revision} stage={message.metadata.stage} onOpenSnapshot={openResult}/>}{!message.metadata?.turn_response&&message.metadata?.artifact_ids?.filter((id:string)=>id!==message.metadata?.stage_artifact_id).map((id:string)=><ArtifactCard key={id} id={id} revision={message.metadata?.artifact_revisions?.[id]} compact readOnly onOpen={openResult} refreshKey={refreshKey} onTarget={selectTarget} onChanged={changed}/>)}{message.metadata?.refs?.length>0&&<details><summary>查看回答依据</summary><p className="muted small-text">{message.metadata.refs.join(' · ')}</p></details>}{message.metadata?.proposal?.template_kinds?.length>0&&<p className="muted small-text">已识别模板：{message.metadata.proposal.template_kinds.filter((kind:string)=>['scenarios','cases'].includes(kind)).map((kind:string)=>kind==='scenarios'?'场景模板':'用例模板').join('、')}</p>}{message.metadata?.proposal&&<details className="receipt-details"><summary>查看 Profile 学习建议</summary><pre>{JSON.stringify(message.metadata.proposal,null,2)}</pre><p className="muted small-text">在对话框输入：将这个模板应用到当前 Profile。</p></details>}{message.role==='assistant'&&completedRuns.has(message.metadata?.run_id)&&lastAssistantForRun.get(message.metadata.run_id)===message.id&&<RunTimeline runId={message.metadata.run_id} initialOpen={false} agentLive={false} agentMode={snapshot?.runs.find(r=>r.id===message.metadata.run_id)?.experience==='agent'} initialAgent={snapshot?.runs.find(r=>r.id===message.metadata.run_id)?.agent}/>}</div></article>;})}
   {workspaceProposal&&<section className="workspace-proposal" aria-label="待应用修改"><details><summary>查看修改明细</summary>{workspaceProposal.changes?.map((change:Json,index:number)=><ChangePreview key={change.artifact_id+':'+index} change={change} before={change.before_items?{id:change.artifact_id,type:'',title:'',revision:change.expected_revision,items:change.before_items}:undefined}/>)}</details><p className="muted small-text">可在聊天中说明是否采用或如何调整。</p></section>}
   <ConversationPrompt prompt={displayedPrompt} waiting={sending}/>
   <div ref={bottom}/>{newActivity&&<button className="back-to-latest" onClick={()=>{bottom.current?.scrollIntoView({behavior:'smooth',block:'end'});setNearBottom(true);setNewActivity(false);}}>回到最新对话</button>}
   </div></div>
   <div className="composer-wrap"><div className="composer-inner"><ErrorBox message={error}/><WorkflowSummary run={currentRun} prompt={displayedPrompt} hasResult={!!visibleOutput} onOpen={()=>void openCurrentResult()}/>{target&&<div className="target-bar"><span>当前对象：{target.artifact.title} · v{target.artifact.revision}{target.ids.length?` · ${target.ids.join('、')}`:''}</span><button className="icon-button" aria-label="取消修改目标" onClick={()=>{setTarget(undefined);setViewedTable(undefined);setSelectionResetKey(value=>value+1);}}><X size={15}/></button></div>}
   {!!snapshot?.sources.length&&<div className="attachment-list" aria-label="已上传资料">{snapshot.sources.map(source=><button key={source.id} className="attachment-chip" onClick={()=>setSourcesOpen(true)}><FileText size={15}/><span>{source.name}</span><small>{roles[source.role]??source.role}</small></button>)}</div>}
   {uploadResults.length>0&&<div className="upload-results" aria-label="上传结果">{uploadResults.map((result,index)=><span className={result.ok?'success':'error-text'} key={result.name+index}>{result.name}：{result.message}</span>)}</div>}
   <ConversationSuggestions key={chatId+':'+(displayedPrompt?.id??'')} prompt={displayedPrompt} disabled={sending||projectLoading||uploading||operationBusy||!!latestRun?.edit_in_progress||latestRun?.status==='queued'||latestRun?.status==='running'} onChoose={(text,kind)=>{if(displayedPrompt)void send(undefined,undefined,undefined,{text,kind,promptId:displayedPrompt.id});}}/>
   <div className="composer"><div className="composer-input"><textarea ref={textarea} aria-label="聊天输入" rows={2} style={{minHeight:'48px',maxHeight:'144px',lineHeight:'24px'}} placeholder={displayedPrompt?'直接回复同意、提出修改，或先问一个问题…':target?.ids.length?'说明如何修改所选条目…':'提问、说明修改，或补充新需求…'} value={content} onChange={e=>setContent(e.target.value)} onKeyDown={e=>{if(e.key==='Enter'&&!e.shiftKey&&!e.nativeEvent.isComposing){e.preventDefault();void send();}}}/><button className="send-button" aria-label="发送消息" disabled={!content.trim()||projectLoading||uploading} onClick={()=>void send()}>{sending?<Spinner/>:<ArrowUp size={21}/>}</button></div><div className="composer-tools"><div className="tool-group">
    <input ref={fileInput} type="file" multiple accept=".docx,.pdf,.xlsx,.xls,.csv,.txt,.md" hidden onChange={e=>void upload(e.target.files)}/><button className="icon-button attachment-button" disabled={projectLoading||uploading||sending||operationBusy} title="上传需求文档" aria-label="上传需求文档" onClick={()=>fileInput.current?.click()}>{uploading?<Spinner/>:<Paperclip size={19}/>}</button>
    <label className="visible-mode">文件用途<select aria-label="附件用途" value={uploadRole} disabled={uploading} onChange={e=>setUploadRole(e.target.value)}>{Object.entries(roles).map(([value,label])=><option key={value} value={value}>{label}</option>)}</select></label>
    <label className="visible-mode">MODE<select aria-label="运行模式" value={displayedMode} disabled={modeLocked} onChange={e=>setMode(e.target.value as 'auto'|'hitp')}><option value="auto">Auto · 自动完成</option><option value="hitp">Human · 逐步确认</option></select></label>
    <label className="visible-mode">Profile<select aria-label="运行 Profile" value={profileId} disabled={running} onChange={e=>selectProfile(e.target.value)}>{profiles.map(p=><option key={p.id} value={p.id}>{p.name}</option>)}</select></label>
    <button className="task-options-toggle" aria-label="任务设置" aria-expanded={optionsOpen} onClick={()=>setOptionsOpen(value=>!value)}>本次方案 · {depthNames[depth]}<SettingsIcon size={14}/></button>
    <label className="check-label"><input type="checkbox" aria-label="正文作为需求" checked={asRequirement} disabled={running} onChange={e=>setAsRequirement(e.target.checked)}/>正文作为需求</label>
   </div></div>
   {optionsOpen&&<div className="task-options" aria-label="任务设置"><div className="task-settings-heading"><strong>本次方案</strong><button className="icon-button" aria-label="收起任务设置" onClick={()=>setOptionsOpen(false)}><X size={16}/></button></div>{runOverride&&<p className="small-text">本轮使用临时配置 <button onClick={()=>setRunOverride(undefined)}>取消</button></p>}<label>测试设计深度<select value={depth} onChange={e=>setDepth(e.target.value as Depth)}>{(['auto','quick','standard','deep'] as Depth[]).map(value=><option key={value} value={value}>{depthNames[value]}</option>)}</select></label><fieldset className="case-type-options"><legend>用例类型</legend>{['Business','Negative','Boundary','Security'].map(value=><label className="check-label" key={value}><input type="checkbox" aria-label={value} checked={caseTypes.includes(value)} onChange={e=>{setCaseTypesTouched(true);setCaseTypes(old=>e.target.checked?[...old,value]:old.filter(x=>x!==value));}}/>{value}</label>)}</fieldset><label>任务目标<select aria-label="任务目标" disabled={!!target} value={intent} onChange={e=>selectIntent(e.target.value)}>{intents.map(([value,label])=><option key={value} value={value}>{label}</option>)}</select></label></div>}
   </div>
   <div className="intent-shortcuts" role="group" aria-label="选择任务"><span>选择任务</span>{intents.map(([value,label])=><button key={value} disabled={!!target} className={intent===value?'active':''} aria-pressed={intent===value} onClick={()=>selectIntent(value)}>{label}</button>)}<button onClick={()=>openSettings(undefined,'profile')}>编辑 Profile / Excel 格式</button></div>
   <p className="composer-note">{latestRun?.status==='waiting'?'在这里回复同意继续，也可以先提问、修改或补充资料。':'Shift + Enter 换行 · 修改与来源记录会保留'}</p>
   </div></div></section>
  </div></main>
  {settingsOpen&&<SettingsDialog initialTab={settingsTab} onUseOnce={setRunOverride} projectId={projectId} profiles={profiles} activeProfileId={profileId} onSelectProfile={selectProfile} proposal={proposal} onClose={()=>setSettingsOpen(false)} onSaved={()=>reloadSettings().catch(e=>setError(errText(e)))}/>}
  {sourcesOpen&&chatId&&<SourcesDialog chatOnly chatId={chatId} sources={snapshot?.sources??[]} onChanged={changed} onClose={()=>setSourcesOpen(false)}/>}
  {memoryOpen&&projectId&&<MemoryDialog projectId={projectId} onClose={()=>setMemoryOpen(false)}/>}
  {viewer&&<Dialog title={(viewer.historical?'历史成果':'查看成果')+' · '+viewer.artifact.title} onClose={closeViewer} wide><div className="result-viewer-label"><strong>{viewer.historical?'历史版本 · 只读':'当前成果'}</strong><span>v{viewer.artifact.revision}</span>{viewer.historical&&<button onClick={()=>void openLatestFromHistory(viewer.artifact.id)}>查看最新版本</button>}</div><ArtifactCard key={viewer.artifact.id+':'+viewer.artifact.revision} chatOnly id={viewer.artifact.id} snapshot={viewer.artifact} initialSelectedIds={!viewer.historical&&target?.artifact.id===viewer.artifact.id?target.ids:[]} readOnly={viewer.historical} simplified={false} refreshKey={refreshKey} onSelectionChange={viewer.historical?undefined:trackSelection} onTarget={()=>{}} onChanged={changed}/>{!viewer.historical&&<div className="dialog-actions viewer-actions"><button onClick={closeViewer}>返回对话</button><button className="primary" disabled={target?.artifact.id!==viewer.artifact.id||!target.ids.length} onClick={()=>{const selected=target?.artifact.id===viewer.artifact.id?target.ids:[];selectTarget(viewer.artifact,selected,viewedTable?.viewOrder);if(!draftContent.current.trim())setContent('请修改所选条目：');closeViewer();}}>在对话中修改所选</button></div>}</Dialog>}
  {projectDialog&&<Dialog title="创建项目" onClose={()=>setProjectDialog(false)}><label>项目名称<input autoFocus value={projectName} onChange={e=>setProjectName(e.target.value)} placeholder="例如：订单服务"/></label><div className="dialog-actions"><button className="primary" disabled={!projectName.trim()} onClick={async()=>{try{const p=await api<Project>('/projects',{name:projectName.trim()});setProjects(old=>[...old,p]);selectProject(p.id);setProjectDialog(false);setProjectName('');}catch(e){setError(errText(e));}}}>创建项目</button></div></Dialog>}
 </div></ConversationContext.Provider>;
}
