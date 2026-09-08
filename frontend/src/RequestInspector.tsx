import {useEffect,useState} from 'react';
import {Dialog,ErrorBox,Spinner} from './ui';
import {errText} from './api';
import type {Json} from './types';

export function RequestInspector({runId,callId,onClose}:{runId:string;callId:string;onClose:()=>void}){
 const [snapshot,setSnapshot]=useState<Json|null>(null);
 const [error,setError]=useState('');const [tab,setTab]=useState('system');const [retry,setRetry]=useState(0);
 const identity=runId+':'+callId;
 const path=`/api/runs/${encodeURIComponent(runId)}/model-calls/${encodeURIComponent(callId)}/request`;
 useEffect(()=>{
  const controller=new AbortController();let active=true;
  setSnapshot(null);setError('');setTab('system');
  async function load(){
   try{
    const response=await fetch(path,{signal:controller.signal,cache:'no-store'});
    const body=await response.json();
    if(!response.ok)throw new Error(typeof body.detail==='string'?body.detail:`读取失败（${response.status}）`);
    if(body.run_id!==runId||body.call_id!==callId)throw new Error('请求记录不属于当前调用，请重新读取');
    if(active)setSnapshot({...body,_identity:identity});
   }catch(e){if(active)setError(errText(e));}
  }
  void load();return()=>{active=false;controller.abort();};
 },[path,runId,callId,identity,retry]);
 const current=snapshot?._identity===identity?snapshot:null;
 const full=current?Object.fromEntries(Object.entries(current).filter(([key])=>key!=='_identity')):null;
 const message=current?.messages?.find((m:Json)=>m.role===(tab==='system'?'system':'user'))?.content??'';
 let content=message;
 if(tab==='context'){try{content=JSON.stringify(JSON.parse(message),null,2);}catch{}}
 if(tab==='record')content=JSON.stringify(full,null,2);
 return <Dialog title="发送给 AI 的内容" onClose={onClose} wide>
  <p className="muted">这是应用交给 LangChain 的消息和显式参数快照，后续修改不会改变此记录。</p>
  <p className="muted small-text">适配器可能调整消息角色或省略参数，因此下列内容不等同于服务商收到的完整 HTTP 请求。</p>
  <ErrorBox message={error}/>{error&&<button onClick={()=>setRetry(value=>value+1)}>重新读取</button>}
  {!current&&!error&&<Spinner/>}
  {current&&<><p className="request-meta">模型：{current.model} · 请求超时：{current.timeout_seconds} 秒<br/>服务地址：{current.base_url}<br/>调用：{callId}</p>
   <div className="tabs request-tabs">{[['system','系统提示词'],['context','任务上下文'],['record','请求记录']].map(([value,label])=><button key={value} className={tab===value?'active':''} onClick={()=>setTab(value)}>{label}</button>)}</div>
   <pre className="request-body" aria-label="发送内容">{content||'本次没有这部分消息。'}</pre>
   <div className="dialog-actions"><span className="muted">消息正文完整保留；认证信息不包含在记录中。</span><a href={path+'?download=true'} download>下载请求记录</a></div>
  </>}
 </Dialog>;
}
