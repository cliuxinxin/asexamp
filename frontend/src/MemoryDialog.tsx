import {useEffect,useState} from 'react';
import {Trash2} from 'lucide-react';
import {api,errText} from './api';
import {Dialog,ErrorBox,Spinner} from './ui';
import type {MemoryEntry} from './types';
type SharedContext={clarifications:{id:string;name:string;text:string;created_at:string;active:boolean;chat_id:string}[];samples:{profile_id:string;profile_name:string;count:number;version:number}[]};

export function MemoryDialog({projectId,onClose}:{projectId:string;onClose:()=>void}){
 const [items,setItems]=useState<MemoryEntry[]>([]),[content,setContent]=useState(''),[kind,setKind]=useState<'preference'|'business'>('preference'),[error,setError]=useState(''),[working,setWorking]=useState(false);
 const [shared,setShared]=useState<SharedContext>({clarifications:[],samples:[]});
 useEffect(()=>{let active=true;Promise.all([api<MemoryEntry[]>('/projects/'+projectId+'/memory'),api<SharedContext>('/projects/'+projectId+'/shared-context')]).then(([memory,context])=>{if(active){setItems(memory);setShared(context);}}).catch(e=>{if(active)setError(errText(e));});return()=>{active=false;};},[projectId]);
 async function save(){setWorking(true);setError('');try{const item=await api<MemoryEntry>('/projects/'+projectId+'/memory',{content:content.trim(),kind});setItems(old=>[...old,item]);setContent('');}catch(e){setError(errText(e));}finally{setWorking(false);}}
 async function remove(item:MemoryEntry){setWorking(true);setError('');try{await api('/projects/'+projectId+'/memory/'+item.id,undefined,'DELETE');setItems(old=>old.filter(x=>x.id!==item.id));}catch(e){setError(errText(e));}finally{setWorking(false);}}
 async function unshare(id:string){setWorking(true);setError('');try{await api('/projects/'+projectId+'/shared-context/'+id,undefined,'DELETE');setShared(old=>({...old,clarifications:old.clarifications.filter(item=>item.id!==id)}));}catch(e){setError(errText(e));}finally{setWorking(false);}}
 return <Dialog title="项目记忆" onClose={onClose}><p className="muted">本项目共享的澄清、写作样例与长期信息，供项目后续会话复用。</p>
 <h3>项目澄清</h3><div className="memory-list">{shared.clarifications.filter(item=>item.active).map(item=><div className="memory-row" key={item.id}><span><strong>{item.name}</strong><p className="preserve">{item.text}</p><small className="muted">{new Date(item.created_at).toLocaleString()}</small></span><button disabled={working} aria-label={'取消共享 '+item.name} onClick={()=>unshare(item.id)}>取消共享</button></div>)}{!shared.clarifications.some(item=>item.active)&&<div className="empty-note">暂无共享澄清。在澄清问题提交时选择保存到项目即可复用。</div>}</div>
 <h3>项目样例</h3>{shared.samples.length?<ul>{shared.samples.map(item=><li key={item.profile_id}>{item.profile_name} · {item.count} 条样例 · v{item.version}</li>)}</ul>:<p className="empty-note">暂无项目样例。可从用例结果选择“保存为项目样例”。</p>}<p className="muted small-text">样例仅指导字段和写法，可在 Profile 设置中查看和移除。</p>
 <h3>长期信息</h3><div className="memory-list">{items.map(item=><div className="memory-row" key={item.id}><span><strong>{item.kind==='business'?'业务规则':'偏好'}</strong><p>{item.content}</p></span><button className="icon-button" aria-label={'删除 '+item.content} disabled={working} onClick={()=>remove(item)}><Trash2 size={16}/></button></div>)}{!items.length&&<div className="empty-note">暂无项目记忆。</div>}</div><label>记忆类型<select aria-label="记忆类型" value={kind} onChange={e=>setKind(e.target.value as any)}><option value="preference">偏好</option><option value="business">业务规则</option></select></label><label>记忆内容<textarea aria-label="记忆内容" rows={3} value={content} onChange={e=>setContent(e.target.value)}/></label><div className="dialog-actions"><button className="primary" disabled={working||!content.trim()} onClick={save}>{working?<Spinner/>:'保存记忆'}</button></div><ErrorBox message={error}/></Dialog>;
}
