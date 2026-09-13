import {useEffect,useState} from 'react';
import {api,errText} from './api';
import type {Artifact} from './types';

export type ProcessArtifact={key:string;artifact_id:string;revision:number;type:string;title:string;phase:string;label:string;is_current:boolean;created_at:string};

export function ProcessArtifactPicker({chatId,artifact,refreshKey,onSelect}:{chatId:string;artifact:Artifact;refreshKey?:string;onSelect:(entry:ProcessArtifact)=>void}){
 const [entries,setEntries]=useState<ProcessArtifact[]>([]);const [error,setError]=useState('');const [retry,setRetry]=useState(0);const [loading,setLoading]=useState(true);
 useEffect(()=>{let alive=true;setLoading(true);setError('');api<{items:ProcessArtifact[]}>('/chats/'+encodeURIComponent(chatId)+'/artifacts').then(result=>{if(alive)setEntries(Array.isArray(result.items)?result.items:[]);}).catch(e=>{if(alive)setError(errText(e));}).finally(()=>{if(alive)setLoading(false);});return()=>{alive=false;};},[chatId,refreshKey,retry]);
 const selected=(artifact.view_item_ids?'subset:':'')+artifact.id+':'+artifact.revision;
 const labels:Record<string,string>={analysis:'需求理解',scenarios:'测试场景',cases:'用例草稿与评审',review:'评审结果'};
 return <div className="process-artifact-picker">
  <label>过程成果<select aria-label="切换过程成果" value={selected} disabled={loading} onChange={e=>{const entry=entries.find(value=>value.key===e.target.value);if(entry)onSelect(entry);}}>
   {!entries.some(entry=>entry.key===selected)&&<option value={selected}>{artifact.title} · v{artifact.revision}{artifact.view_item_ids?` · 所查看的 ${artifact.items.length} 条`:''}</option>}
   {Object.entries(labels).map(([type,label])=>{const values=entries.filter(entry=>entry.type===type);return values.length?<optgroup key={type} label={label}>{values.map(entry=><option key={entry.key} value={entry.key}>{entry.label} · {entry.title} · v{entry.revision}{entry.is_current?' · 当前版本':' · 历史版本'}</option>)}</optgroup>:null;})}
  </select></label>
  <small className="muted">查看已保存的需求理解、场景、用例草稿和评审结果，不改变当前流程。</small>
  {error&&<p role="alert">{error} <button onClick={()=>setRetry(value=>value+1)}>重试读取成果列表</button></p>}
 </div>;
}
