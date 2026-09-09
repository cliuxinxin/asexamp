import {useEffect,useState} from 'react';
import {Trash2} from 'lucide-react';
import {api,errText} from './api';
import {Dialog,ErrorBox,Spinner} from './ui';
import type {MemoryEntry} from './types';

export function MemoryDialog({projectId,onClose}:{projectId:string;onClose:()=>void}){
 const [items,setItems]=useState<MemoryEntry[]>([]),[content,setContent]=useState(''),[kind,setKind]=useState<'preference'|'business'>('preference'),[error,setError]=useState(''),[working,setWorking]=useState(false);
 useEffect(()=>{api<MemoryEntry[]>('/projects/'+projectId+'/memory').then(setItems).catch(e=>setError(errText(e)));},[projectId]);
 async function save(){setWorking(true);setError('');try{const item=await api<MemoryEntry>('/projects/'+projectId+'/memory',{content:content.trim(),kind});setItems(old=>[...old,item]);setContent('');}catch(e){setError(errText(e));}finally{setWorking(false);}}
 async function remove(item:MemoryEntry){setWorking(true);setError('');try{await api('/projects/'+projectId+'/memory/'+item.id,undefined,'DELETE');setItems(old=>old.filter(x=>x.id!==item.id));}catch(e){setError(errText(e));}finally{setWorking(false);}}
 return <Dialog title="项目记忆" onClose={onClose}><p className="muted">由你维护、供本项目后续测试设计使用的长期信息。</p><div className="memory-list">{items.map(item=><div className="memory-row" key={item.id}><span><strong>{item.kind==='business'?'业务规则':'偏好'}</strong><p>{item.content}</p></span><button className="icon-button" aria-label={'删除 '+item.content} disabled={working} onClick={()=>remove(item)}><Trash2 size={16}/></button></div>)}{!items.length&&<div className="empty-note">暂无项目记忆。</div>}</div><label>记忆类型<select aria-label="记忆类型" value={kind} onChange={e=>setKind(e.target.value as any)}><option value="preference">偏好</option><option value="business">业务规则</option></select></label><label>记忆内容<textarea aria-label="记忆内容" rows={3} value={content} onChange={e=>setContent(e.target.value)}/></label><div className="dialog-actions"><button className="primary" disabled={working||!content.trim()} onClick={save}>{working?<Spinner/>:'保存记忆'}</button></div><ErrorBox message={error}/></Dialog>;
}
