import {useEffect,useRef,useState} from 'react';
import {AgentPanel} from './AgentPanel';
import type {AgentState,Artifact} from './types';
import {RequestInspector} from './RequestInspector';
import {emptyFeed,reduceEvent} from './runProgress';
import type {ModelOutput} from './runProgress';

const outputStatus:Record<string,string>={running:'生成中 · 尚未校验',returned:'返回完成 · 等待校验',applied:'修正已应用 · 以工作项校验结果为准',accepted:'阶段完成',invalid:'校验未通过',failed:'本次请求或阶段失败',cancelled:'已停止'};
function Output({call,runId}:{call:ModelOutput;runId:string}){
 const [inspecting,setInspecting]=useState(false);
 const [open,setOpen]=useState(true);const output=useRef<HTMLPreElement>(null);const pinned=useRef(true);
 useEffect(()=>{if(output.current&&pinned.current)output.current.scrollTop=output.current.scrollHeight;},[call.text,open]);
 return <div className={'model-output '+call.status}><div className="request-actions"><button disabled={!call.requestAvailable} onClick={()=>setInspecting(true)}>查看发送内容</button>{!call.requestAvailable&&<small className="muted">尚无发送记录；旧版本调用无法补回</small>}</div>{inspecting&&<RequestInspector key={runId+':'+call.id} runId={runId} callId={call.id} onClose={()=>setInspecting(false)}/>}<button className="output-toggle" onClick={()=>setOpen(value=>!value)} aria-expanded={open}><span>AI 输出 · {outputStatus[call.status]}</span><small>{call.attempt?`第 ${call.attempt} 次请求`:'模型请求'}{typeof call.elapsed==='number'?` · ${(call.elapsed/1000).toFixed(1)} 秒`:''} · {open?'收起':'展开'}</small></button>{open&&<pre ref={output} onScroll={()=>{const box=output.current;if(box)pinned.current=box.scrollHeight-box.scrollTop-box.clientHeight<48;}} aria-label={call.label+'的模型输出'}>{call.text||(call.status==='running'?(call.streaming===false?'上游未开启流式返回，正在等待完整响应；收到内容后会显示。':'等待模型返回内容…'):'本次没有返回可展示的内容。')}</pre>}</div>;
}

export function RunTimeline({runId,restartKey='',onChanged,onTarget,initialOpen=true,agentMode=false,agentLive=true,initialAgent,runStatus='running'}:{runId:string;restartKey?:string;onChanged?:()=>void;onTarget?:(artifact:Artifact,ids:string[])=>void;initialOpen?:boolean;agentMode?:boolean;agentLive?:boolean;initialAgent?:AgentState;runStatus?:string}){
 const [liveAgent,setLiveAgent]=useState<{runId:string;data:AgentState}>();
 const [open,setOpen]=useState(initialOpen);const [feed,setFeed]=useState(emptyFeed);const [connection,setConnection]=useState('connecting');const [reconnect,setReconnect]=useState(0);
 const connected=(agentMode&&agentLive)||open;
 const scope=useRef({runId,cursor:0});const onChangedRef=useRef(onChanged);onChangedRef.current=onChanged;
 const initialAgentRef=useRef(initialAgent);initialAgentRef.current=initialAgent;
 useEffect(()=>{setLiveAgent(initialAgent?{runId,data:initialAgent}:undefined);},[runId,initialAgent]);
 useEffect(()=>{
  if(!connected)return;
  if(scope.current.runId!==runId){scope.current={runId,cursor:0};setFeed(emptyFeed());}
  let active=true;let timer:ReturnType<typeof setTimeout>|undefined;let refreshTimer:ReturnType<typeof setTimeout>|undefined;
  let pending:{kind:string;id:number;data:any}[]=[];
  setConnection('connecting');
  const source=new EventSource(`/api/runs/${encodeURIComponent(runId)}/events?after=${scope.current.cursor}`);
  const flush=()=>{timer=undefined;const batch=pending;pending=[];if(active&&batch.length){
   setFeed(old=>batch.reduce((state,e)=>reduceEvent(state,e.kind,e.id,e.data),old));
   const changes=batch.filter(e=>e.kind==='agent'||e.kind==='agent_work');
   if(changes.length)setLiveAgent(old=>{
    let data=old?.runId===runId?old.data:initialAgentRef.current??{};
    for(const event of changes){if(event.kind==='agent'&&event.data.agent)data=event.data.agent;else if(event.kind==='agent_work'){const work=event.data.work??event.data;if(Array.isArray(work.items))data={...data,work};}}
    return{runId,data};
   });
  }};
  const refresh=()=>{if(!refreshTimer)refreshTimer=setTimeout(()=>{refreshTimer=undefined;if(active)onChangedRef.current?.();},250);};
  const receive=(kind:string)=>(raw:Event)=>{
   if(!active)return;
   const event=raw as MessageEvent;const id=Number(event.lastEventId);
   if(!Number.isSafeInteger(id)||id<=scope.current.cursor)return;
   try{const data=JSON.parse(event.data);if(data.run_id&&data.run_id!==runId)return;
    pending.push({kind,id,data});scope.current.cursor=id;if(!timer)timer=setTimeout(flush,80);
    if(kind==='update'||kind==='agent'||kind==='agent_work')refresh();
   }catch{setConnection('invalid');source.close();}
  };
  for(const kind of ['update','progress','model_delta','agent','agent_work'])source.addEventListener(kind,receive(kind));
  source.onopen=()=>{if(active)setConnection('live');};
  source.onerror=()=>{if(active)setConnection(source.readyState===2?'closed':'reconnecting');}; // EventSource resumes with Last-Event-ID.
  source.addEventListener('done',()=>{if(!active)return;flush();setConnection('done');source.close();refresh();});
  return()=>{flush();active=false;source.close();if(timer)clearTimeout(timer);if(refreshTimer)clearTimeout(refreshTimer);};
 },[runId,restartKey,connected,reconnect]);
 return <>{agentMode&&(agentLive||open)&&<AgentPanel agent={liveAgent?.runId===runId?liveAgent.data:initialAgent} runId={runId} runStatus={runStatus} onTarget={onTarget} onChanged={onChanged}/>}<section className="run-timeline" aria-label="执行过程"><button className="timeline-heading" onClick={()=>setOpen(value=>!value)} aria-expanded={open}><strong>{agentMode?(open?'收起调用详情':'查看调用详情'):(open?'执行过程':'查看完整执行过程')}</strong><small>{open?({connecting:'连接进度…',live:'实时更新',reconnecting:'连接中断，自动重连…',done:'记录已同步',closed:'连接已关闭',invalid:'事件解析失败'}[connection]):'阶段、模型输出与校验记录'}</small></button>{open&&<>{['closed','invalid'].includes(connection)&&<button className="timeline-reconnect" onClick={()=>setReconnect(value=>value+1)}>重新连接</button>}{!feed.entries.length&&<p className="muted small-text">正在读取执行记录。旧版本任务可能只有状态记录。</p>}<ol className="timeline-list">{feed.entries.map(entry=><li key={entry.id} className={'timeline-entry '+entry.tone}><div className="timeline-marker"/><div className="timeline-content"><div className="timeline-line"><span>{entry.text}</span><time>{new Date(entry.at).toLocaleTimeString()}</time></div>{entry.callId&&feed.calls[entry.callId]&&<Output runId={runId} call={feed.calls[entry.callId]}/>}</div></li>)}</ol><p className="muted small-text timeline-note">已收到的模型内容会显示在这里；是否逐步返回取决于上游服务。以校验通过并保存的最终结果为准。</p></>}</section></>;
}

