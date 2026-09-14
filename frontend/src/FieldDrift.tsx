import {useEffect,useRef,useState} from 'react';
import {api,errText} from './api';
import {ErrorBox,Spinner} from './ui';
import type {Json} from './types';

type Candidate={field:string;header:string;item_ids:string[]};
type Drift={artifact_id:string;revision:number;head_revision:number;profile_id:string;profile_version:number;candidates:Candidate[]};
type Actions={onProposed:(promptId:string)=>void;disabled?:boolean;onBusyChange?:(busy:boolean)=>void;onRefresh?:()=>void};
function SyncButton({chatId,drift,label,disabled,onProposed,onBusyChange,onRefresh}:Actions&{chatId:string;drift:Drift;label:string}){
 const [working,setWorking]=useState(false),[error,setError]=useState('');const locked=useRef(false),alive=useRef(true);
 useEffect(()=>{alive.current=true;return()=>{alive.current=false;};},[]);
 async function stage(){
  if(disabled||locked.current)return;locked.current=true;setWorking(true);setError('');onBusyChange?.(true);
  try{
   const {artifact_id,revision,head_revision,profile_id,profile_version,candidates}=drift;
   const result=await api<Json>('/chats/'+encodeURIComponent(chatId)+'/field-drift/propose',{artifact_id,revision,head_revision,profile_id,profile_version,fields:candidates.map(c=>c.field)});
   const promptId=result.pending?.[0]?.id;
   if(!promptId)throw new Error('未取得 Profile 更改建议，请刷新对话后查看。');
   if(alive.current)onProposed(promptId);
  }catch(e){if(alive.current)setError(errText(e));}finally{locked.current=false;if(alive.current)setWorking(false);onBusyChange?.(false);}
 }
 return <><button disabled={disabled||working} onClick={()=>void stage()}>{working&&<Spinner/>}{label}</button><ErrorBox message={error}/>{error&&onRefresh&&<button disabled={working} onClick={onRefresh}>刷新字段建议</button>}</>;
}

export function FieldDriftSuggestion({chatId,profileId,refreshKey,hidden=false,...actions}:Actions&{chatId:string;profileId?:string;refreshKey?:string;hidden?:boolean}){
 const [reload,setReload]=useState(0);
 const [detected,setDetected]=useState<{key:string;drift:Drift}>();
 const key=[chatId,profileId,refreshKey,reload].join('|');
 useEffect(()=>{
  if(!chatId||hidden){setDetected(undefined);return;}let active=true;
  const query=profileId?'?profile_id='+encodeURIComponent(profileId):'';
  api<Drift>('/chats/'+encodeURIComponent(chatId)+'/field-drift'+query).then(drift=>{if(active)setDetected({key,drift});}).catch(()=>{if(active)setDetected(undefined);});
  return()=>{active=false;};
 },[key,hidden]);
 const drift=detected?.key===key?detected.drift:undefined;
 if(hidden||!drift?.candidates?.length)return null;
 return <div className="field-drift-suggestion" aria-label="模板字段同步建议"><span>发现尚未配置导出的字段：{drift.candidates.map(c=>c.header).join('、')}。</span><SyncButton key={key} chatId={chatId} drift={drift} label="同步到 Profile · 查看更改" {...actions} onRefresh={()=>setReload(value=>value+1)}/></div>;
}

export function ExportFieldDrift({chatId,artifactId,revision,profileId,options,exportIds,...actions}:Actions&{chatId:string;artifactId:string;revision:number;profileId:string;options?:Json;exportIds?:string[]}){
 const selected=options?.profiles?.find((p:Json)=>p.id===profileId);
 const candidates:Candidate[]=(profileId?selected?.field_drift:options?.snapshot_drift)??[];
 const gaps=candidates.filter(c=>exportIds===undefined||c.item_ids.some(id=>exportIds.includes(id)));
 if(!gaps.length)return null;
 const drift={artifact_id:artifactId,revision,head_revision:options?.head_revision,profile_id:profileId,profile_version:selected?.version,candidates:gaps};
 return <div className="field-drift-export" role="status"><p>以下字段未配置导出：<strong>{gaps.map(c=>c.header).join('、')}</strong>。继续下载将不包含这些列。</p>{profileId?<SyncButton key={[artifactId,revision,profileId,selected?.version,exportIds?.join(',')].join(':')} chatId={chatId} drift={drift} label="补充到模板 · 查看更改" {...actions}/>:<p className="muted small-text">配置快照保留原格式。请选择当前 Profile，再补充到模板。</p>}</div>;
}
