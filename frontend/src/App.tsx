import {useCallback,useEffect,useRef,useState} from 'react';
import {ArrowUp,ChevronDown,FileText,Folder,Link,Menu,MessageSquare,Paperclip,Plus,Settings as SettingsIcon,X} from 'lucide-react';
import {api,errText} from './api';
import {ArtifactCard} from './ArtifactCard';
import {SettingsDialog} from './SettingsDialog';
import {SourcesDialog} from './SourcesDialog';
import {RunCard} from './RunCard';
import {RunTimeline} from './RunTimeline';
import {Dialog,ErrorBox,Logo,Spinner} from './ui';
import {useDrafts} from './useDrafts';
import {busy,intents,roles} from './types';
import type {Artifact,Chat,Json,Profile,Project,Settings,Snapshot} from './types';

export function App(){
 const [projectLoading,setProjectLoading]=useState(true);const [projects,setProjects]=useState<Project[]>([]);const [projectId,setProjectId]=useState('');const [chats,setChats]=useState<Chat[]>([]);const [profiles,setProfiles]=useState<Profile[]>([]);const [profileId,setProfileId]=useState('');
 const [chatId,setChatId]=useState('');const [snapshot,setSnapshot]=useState<Snapshot>();const [intent,setIntent]=useState('auto');const [mode,setMode]=useState('auto');
 const [artifactEpoch,setArtifactEpoch]=useState(0);const [settings,setSettings]=useState<Settings>();const [settingsOpen,setSettingsOpen]=useState(false);const [sourcesOpen,setSourcesOpen]=useState(false);const [proposal,setProposal]=useState<Json>();
 const [projectDialog,setProjectDialog]=useState(false);const [projectName,setProjectName]=useState('');const [sidebar,setSidebar]=useState(false);const [error,setError]=useState('');const [sending,setSending]=useState(false);const [uploading,setUploading]=useState(false);const [uploadRole,setUploadRole]=useState('primary');
 const draftKey=projectId+':'+(chatId||'new');const {content,target,asRequirement,setContent,setTarget,setAsRequirement,move:moveDraft,clearSubmitted,version:draftVersion}=useDrafts(draftKey);const projectRef=useRef(projectId);projectRef.current=projectId;const nav=useRef(0);const creating=useRef<{project:string;token:number;promise:Promise<string>}|null>(null);const fileInput=useRef<HTMLInputElement>(null);const textarea=useRef<HTMLTextAreaElement>(null);const bottom=useRef<HTMLDivElement>(null);const chatRef=useRef(chatId);chatRef.current=chatId;
 const latestRun=snapshot?.runs?.slice().sort((a,b)=>b.updated_at.localeCompare(a.updated_at)).find(r=>busy(r));const running=busy(latestRun);const canEditPaused=!!(target&&latestRun?.status==='waiting'&&latestRun.interrupt?.type==='scenario_review'&&target.artifact.id===latestRun.interrupt.artifact_id);const refreshKey=snapshot?.runs?.map(r=>r.id+':'+r.updated_at+':'+r.status).join('|')+':'+artifactEpoch;
 const refresh=useCallback(async()=>{if(!chatId)return;try{const state=await api<Snapshot>('/chats/'+chatId);if(chatRef.current===chatId)setSnapshot(state);}catch(e){if(chatRef.current===chatId)setError(errText(e));}},[chatId]);
 const reloadSettings=useCallback(async()=>{const [s,p]=await Promise.all([api<Settings>('/settings'),projectId?api<Profile[]>('/projects/'+projectId+'/profiles'):Promise.resolve([])]);setSettings(s);if(projectRef.current===projectId){setProfiles(p);setProfileId(old=>p.some(x=>x.id===old)?old:p[0]?.id??'');}},[projectId]);
 useEffect(()=>{api<Project[]>('/projects').then(list=>{setProjects(list);setProjectId(list[0]?.id??'');}).catch(e=>setError(errText(e)));api<Settings>('/settings').then(setSettings).catch(e=>setError(errText(e)));},[]);
 useEffect(()=>{if(!projectId)return;let active=true;setProjectLoading(true);setSnapshot(undefined);setChatId('');chatRef.current='';setChats([]);setProfiles([]);setProfileId('');Promise.all([api<Chat[]>('/projects/'+projectId+'/chats'),api<Profile[]>('/projects/'+projectId+'/profiles')]).then(([c,p])=>{if(active){setChats(c);setProfiles(p);setProfileId(p[0]?.id??'');setChatId(c[0]?.id??'');chatRef.current=c[0]?.id??'';}}).catch(e=>active&&setError(errText(e))).finally(()=>{if(active)setProjectLoading(false);});return()=>{active=false;};},[projectId]);
 useEffect(()=>{setSnapshot(undefined);setError('');refresh();},[refresh]);
 useEffect(()=>{if(!chatId)return;const timer=setInterval(refresh,7000);return()=>clearInterval(timer);},[chatId,running,refresh]);
 useEffect(()=>{bottom.current?.scrollIntoView({behavior:'smooth',block:'end'});},[chatId,snapshot?.messages.length,latestRun?.interrupt?.type]);
 function selectProject(id:string){setProjectLoading(true);nav.current++;projectRef.current=id;chatRef.current='';setProjectId(id);setChatId('');setChats([]);setProfiles([]);setSnapshot(undefined);setSettingsOpen(false);setSourcesOpen(false);}
 function selectChat(id:string){nav.current++;chatRef.current=id;setChatId(id);setSidebar(false);setSourcesOpen(false);}
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
  const pid=projectRef.current;const token=++nav.current;setError('');
  try{const chat=await api<Chat>('/projects/'+pid+'/chats',{});if(projectRef.current!==pid||nav.current!==token)return;setChats(old=>[chat,...old]);chatRef.current=chat.id;setChatId(chat.id);setSidebar(false);textarea.current?.focus();}catch(e){if(nav.current===token)setError(errText(e));}
 }
 async function send(){
  if(!content.trim()||projectLoading||sending||uploading||(running&&!canEditPaused))return;
  const pid=projectRef.current;const token=nav.current;const message=content;const submittedVersion=draftVersion;const submittedTarget=target;
  setSending(true);setError('');
  try{
   const id=await ensureChat();
   if(canEditPaused&&latestRun)await api('/runs/'+latestRun.id+'/edit',{content:message,...(submittedTarget?.ids.length?{selected_ids:submittedTarget.ids}:{})});
   else await api('/chats/'+id+'/messages',{content:message,as_requirement:asRequirement,intent:submittedTarget?'modify':intent,mode,profile_id:profileId||undefined,...(submittedTarget?{artifact_id:submittedTarget.artifact.id,...(submittedTarget.ids.length?{selected_ids:submittedTarget.ids}:{})}:{})});
   clearSubmitted(pid+':'+id,submittedVersion);
   const [state,list]=await Promise.all([api<Snapshot>('/chats/'+id),api<Chat[]>('/projects/'+pid+'/chats')]);
   if(chatRef.current===id&&projectRef.current===pid)setSnapshot(state);
   if(projectRef.current===pid)setChats(list);
  }catch(e){if(nav.current===token)setError(errText(e));}finally{setSending(false);}
 }
 async function upload(files:FileList|null){
  if(!files?.length||uploading)return;const token=nav.current;setUploading(true);setError('');
  try{const id=await ensureChat();for(const file of Array.from(files)){const form=new FormData();form.append('file',file);form.append('role',uploadRole);await api('/chats/'+id+'/sources',form);}const state=await api<Snapshot>('/chats/'+id);if(chatRef.current===id)setSnapshot(state);}catch(e){if(nav.current===token)setError(errText(e));}finally{setUploading(false);if(fileInput.current)fileInput.current.value='';}
 }
 function selectTarget(artifact:Artifact,ids:string[]){setTarget({artifact,ids});textarea.current?.focus();}
 const lastAssistantForRun=new Map(snapshot?.messages.filter(m=>m.role==='assistant'&&m.metadata?.run_id).map(m=>[m.metadata.run_id,m.id]));
 const completedRuns=new Set(snapshot?.runs.filter(r=>r.status==='completed').map(r=>r.id));
 const openSettings=(value?:Json)=>{setProposal(value);setSettingsOpen(true);};
 return <div className="app-shell">
  {sidebar&&<button className="sidebar-backdrop" aria-label="收起对话列表" onClick={()=>setSidebar(false)}/>}
  <aside className={'sidebar '+(sidebar?'open':'')}><div className="brand"><Logo/><span>TCG</span><button className="icon-button mobile-only" aria-label="收起侧栏" onClick={()=>setSidebar(false)}><X size={20}/></button></div><button className="primary new-chat" disabled={!projectId||projectLoading} onClick={newChat}><Plus size={20}/>新对话</button><div className="project-select"><Folder size={18}/><select aria-label="当前项目" value={projectId} onChange={e=>selectProject(e.target.value)}>{projects.map(p=><option key={p.id} value={p.id}>{p.name}</option>)}</select><button className="icon-button" aria-label="创建项目" onClick={()=>setProjectDialog(true)}><Plus size={15}/></button></div><p className="sidebar-label">最近对话</p><nav className="chat-list" aria-label="对话历史">{chats.map(chat=><button key={chat.id} className={chat.id===chatId?'active':''} onClick={()=>selectChat(chat.id)}><MessageSquare size={17}/><span>{chat.title}</span></button>)}{!chats.length&&<p className="muted small-text sidebar-empty">开始一次对话，需求和结果会保留在这里。</p>}</nav><button className="settings-button" onClick={()=>openSettings()}><SettingsIcon size={20}/>模型与设置<span className={'connection-dot '+(settings?.model?'ready':'')}/></button></aside>
  <main className="main"><header className="topbar"><div className="inline"><button className="icon-button mobile-only" aria-label="打开对话列表" onClick={()=>setSidebar(true)}><Menu size={22}/></button><h1>{snapshot?.chat.title??'新对话'}</h1></div><button className="text-accent" disabled={projectLoading} onClick={async()=>{try{await ensureChat();setSourcesOpen(true);}catch(e){setError(errText(e));}}}><Link size={17}/>需求来源{snapshot?.sources.length?` · ${snapshot.sources.length}`:''}</button></header>
  <div className="conversation" id="conversation"><div className="conversation-inner">
   {!snapshot?.messages.length?<div className="welcome"><Logo/><h2>从需求开始，把测试想周全。</h2><p>上传文档，或勾选“需求正文”后粘贴需求。<br/>我会分析需求、设计用例，并保留每一条的依据。</p><div className="starter-actions"><button onClick={()=>{setIntent('generate_case');textarea.current?.focus();}}><FileText size={18}/>生成测试用例</button><button onClick={()=>{setIntent('review_requirement');textarea.current?.focus();}}><MessageSquare size={18}/>一起梳理需求</button></div>{!settings?.model&&<button className="setup-link" onClick={()=>openSettings()}>首次使用，先连接一个模型 <ArrowUp size={15} className="arrow-right"/></button>}</div>:snapshot.messages.map(message=><article className={'message '+message.role} key={message.id}>{message.role==='assistant'&&<Logo small/>}<div className="message-body"><div className="message-text preserve">{message.content}</div>{message.metadata?.artifact_ids?.map((id:string)=><ArtifactCard key={id} id={id} refreshKey={refreshKey} onTarget={selectTarget} onChanged={()=>{setArtifactEpoch(x=>x+1);refresh();}}/>)}{message.metadata?.refs?.length>0&&<p className="muted small-text">依据：{message.metadata.refs.join(' · ')}</p>}{message.metadata?.proposal&&<button className="primary" onClick={()=>openSettings(message.metadata.proposal.config??message.metadata.proposal)}>查看并应用 Profile 建议</button>}{message.role==='assistant'&&completedRuns.has(message.metadata?.run_id)&&lastAssistantForRun.get(message.metadata.run_id)===message.id&&<RunTimeline runId={message.metadata.run_id} initialOpen={false}/>} {message.role==='assistant'&&message.metadata?.run_id&&<a className="run-log-link text-accent" href={'/api/runs/'+encodeURIComponent(message.metadata.run_id)+'/diagnostics?download=true'} download>下载本轮诊断日志</a>}</div></article>)}
   {snapshot?.runs.filter(run=>run.status!=='completed').map(run=><div className="message assistant" key={run.id}><Logo small/><div className="message-body"><RunCard run={run} onChanged={()=>{setArtifactEpoch(x=>x+1);refresh();}} onTarget={selectTarget}/></div></div>)}<div ref={bottom}/>
  </div></div>
  <div className="composer-wrap"><div className="composer-inner"><ErrorBox message={error}/>{target&&<div className="target-bar"><span>修改：{target.artifact.title}{target.ids.length?` · 选中 ${target.ids.length} 条`:' · 全部条目'}</span><button className="icon-button" aria-label="取消修改目标" onClick={()=>setTarget(undefined)}><X size={15}/></button></div>}
   {snapshot?.sources.length?<div className="attachment-list">{snapshot.sources.map(source=><button key={source.id} className="attachment-chip" onClick={()=>setSourcesOpen(true)}><FileText size={15}/><span>{source.name}</span><small>{roles[source.role]??source.role}</small></button>)}</div>:null}
   <div className="composer"><div className="composer-input"><textarea ref={textarea} aria-label="聊天输入" rows={2} placeholder={target?'告诉我如何修改这些条目…':'输入需求，或告诉我如何修改用例…'} value={content} onChange={e=>setContent(e.target.value)} onKeyDown={e=>{if(e.key==='Enter'&&!e.shiftKey&&!e.nativeEvent.isComposing){e.preventDefault();send();}}}/><button className="send-button" aria-label="发送消息" disabled={!content.trim()||projectLoading||sending||(running&&!canEditPaused)||uploading} onClick={send}>{sending?<Spinner/>:<ArrowUp size={23}/>}</button></div><div className="composer-tools"><div className="tool-group"><input ref={fileInput} type="file" multiple accept=".docx,.pdf,.xlsx,.xls,.csv,.txt,.md" hidden onChange={e=>upload(e.target.files)}/><button className="icon-button attachment-button" disabled={projectLoading||uploading||running} title="上传需求文档" aria-label="上传需求文档" onClick={()=>fileInput.current?.click()}>{uploading?<Spinner/>:<Paperclip size={21}/>}</button><select className="source-role-select" aria-label="上传文件角色" title="上传文件角色" value={uploadRole} onChange={e=>setUploadRole(e.target.value)}>{Object.entries(roles).map(([value,label])=><option key={value} value={value}>{label}</option>)}</select><span className="tool-divider"/><select aria-label="任务目标" disabled={!!target} value={intent} onChange={e=>setIntent(e.target.value)}>{intents.map(([value,label])=><option key={value} value={value}>{label}</option>)}</select><select aria-label="运行模式" value={mode} onChange={e=>setMode(e.target.value)}><option value="auto">Auto</option><option value="hitp">HITP · 人工确认</option></select><select aria-label="当前 Profile" value={profileId} onChange={e=>setProfileId(e.target.value)}>{profiles.map(p=><option key={p.id} value={p.id}>{p.name}</option>)}</select><label className="requirement-toggle"><input type="checkbox" aria-label="这条消息是需求正文" checked={asRequirement} onChange={e=>setAsRequirement(e.target.checked)}/>这条消息是需求正文</label></div></div></div><p className="composer-note">{running?'任务执行中，停止或完成后可发送下一条消息。':'Enter 发送 · Shift + Enter 换行'}<span>结果可追溯至需求原文，请结合业务复核。</span></p>
  </div></div></main>
  {settingsOpen&&<SettingsDialog projectId={projectId} profiles={profiles} activeProfileId={profileId} proposal={proposal} onClose={()=>setSettingsOpen(false)} onSaved={()=>reloadSettings().catch(e=>setError(errText(e)))}/>}
  {sourcesOpen&&chatId&&<SourcesDialog chatId={chatId} sources={snapshot?.sources??[]} onChanged={()=>{setArtifactEpoch(x=>x+1);refresh();}} onClose={()=>setSourcesOpen(false)}/>}
  {projectDialog&&<Dialog title="创建项目" onClose={()=>setProjectDialog(false)}><label>项目名称<input autoFocus value={projectName} onChange={e=>setProjectName(e.target.value)} placeholder="例如：订单服务"/></label><div className="dialog-actions"><button className="primary" disabled={!projectName.trim()} onClick={async()=>{try{const p=await api<Project>('/projects',{name:projectName.trim()});setProjects(old=>[...old,p]);selectProject(p.id);setProjectDialog(false);setProjectName('');}catch(e){setError(errText(e));}}}>创建项目</button></div></Dialog>}
 </div>;
}
