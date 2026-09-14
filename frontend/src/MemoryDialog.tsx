import {useEffect,useRef,useState} from 'react';
import {Trash2} from 'lucide-react';
import {api,errText} from './api';
import {Dialog,ErrorBox,Spinner} from './ui';
import {knowledgeOrigin} from './KnowledgeUsage';
import type {MemoryEntry,SharedClarification,SharedProjectContext} from './types';

const emptyShared:SharedProjectContext={clarifications:[],samples:[]};

export function MemoryDialog({projectId,chatId,onClose,onChanged}:{projectId:string;chatId?:string;onClose:()=>void;onChanged?:()=>void}){
 const [items,setItems]=useState<MemoryEntry[]>([]),[content,setContent]=useState(''),[kind,setKind]=useState<'preference'|'business'>('preference'),[error,setError]=useState(''),[working,setWorking]=useState(false);
 const [shared,setShared]=useState<SharedProjectContext>(emptyShared),[loading,setLoading]=useState(true),[notice,setNotice]=useState(''),[reload,setReload]=useState(0);
 const scope=projectId+':'+(chatId??''),scopeRef=useRef(scope),saving=useRef(false);
 scopeRef.current=scope;
 useEffect(()=>{
  let active=true;setLoading(true);setError('');setNotice('');setItems([]);setShared(emptyShared);setContent('');
  const query=chatId?'?chat_id='+encodeURIComponent(chatId):'';
  Promise.all([api<MemoryEntry[]>('/projects/'+projectId+'/memory'),api<SharedProjectContext>('/projects/'+projectId+'/shared-context'+query)])
   .then(([memory,context])=>{if(active){setItems(memory);setShared(context);}}).catch(e=>{if(active)setError(errText(e));}).finally(()=>{if(active)setLoading(false);});
  return()=>{active=false;};
 },[projectId,chatId,reload]);
 async function mutate(action:()=>Promise<void>){
  if(saving.current)return;saving.current=true;setWorking(true);setError('');setNotice('');
  try{await action();}catch(e){if(scopeRef.current===scope)setError(errText(e));}finally{saving.current=false;if(scopeRef.current===scope)setWorking(false);}
 }
 async function save(){await mutate(async()=>{const item=await api<MemoryEntry>('/projects/'+projectId+'/memory',{content:content.trim(),kind});if(scopeRef.current!==scope)return;setItems(old=>[...old,item]);setContent('');onChanged?.();});}
 async function remove(item:MemoryEntry){await mutate(async()=>{await api('/projects/'+projectId+'/memory/'+item.id,undefined,'DELETE');if(scopeRef.current!==scope)return;setItems(old=>old.filter(x=>x.id!==item.id));onChanged?.();});}
 async function unshare(id:string){await mutate(async()=>{await api('/projects/'+projectId+'/shared-context/'+id,undefined,'DELETE');if(scopeRef.current!==scope)return;setShared(old=>({...old,clarifications:old.clarifications.filter(item=>item.id!==id)}));setNotice('已取消项目共享。');onChanged?.();});}
 async function toggle(item:SharedClarification){
  if(!chatId||shared.preference_version===undefined)return;
  await mutate(async()=>{
   const next=await api<SharedProjectContext>('/chats/'+chatId+'/project-knowledge/'+encodeURIComponent(item.id),{enabled:item.enabled_in_chat===false,expected_version:shared.preference_version},'PATCH');
   if(scopeRef.current!==scope)return;setShared(next);setNotice(next.message??(next.requires_rebuild?'本次对话规则已更新，下次继续将重新理解需求。':'已更新本次对话使用的规则。'));onChanged?.();
  });
 }
 const activeRules=shared.clarifications.filter(item=>item.active);
 return <Dialog title="项目知识库" onClose={onClose} closeDisabled={working} wide>
  <div className="knowledge-dialog-intro"><p className="muted">查看项目共享规则的来源，选择本次对话要使用的规则。</p><button disabled={loading||working} onClick={()=>setReload(value=>value+1)}>刷新规则</button></div>
  {chatId?<p className="knowledge-scope-note">关闭“本次对话使用”只影响当前对话，其他成员仍可使用。已有成果保留原来的依据；继续生成时会按更新后的规则处理。</p>:<p className="muted small-text">打开具体会话后，可分别设置该会话是否使用某条共享规则。</p>}
  <ErrorBox message={error}/>{notice&&<p className="knowledge-notice" role="status">{notice}</p>}
  {loading?<p className="muted inline"><Spinner/>正在读取项目知识…</p>:<>
   <h3>项目澄清{activeRules.length?` · ${activeRules.length}`:''}</h3>
   <div className="memory-list knowledge-rule-list">{activeRules.map(item=>{
    const enabled=item.enabled_in_chat!==false;
    return <div className={'memory-row knowledge-rule '+(!enabled?'knowledge-rule-disabled':'')} key={item.id}>
     <div className="knowledge-rule-content"><strong>{item.name}</strong><p className="preserve">{item.text}</p><small className="muted">{knowledgeOrigin(item)}{item.source_version!==undefined?` · 来源 v${item.source_version}`:''}</small></div>
     <div className="knowledge-rule-controls">{chatId&&<button type="button" className="knowledge-toggle" role="switch" aria-checked={enabled} aria-label={'本次对话使用 '+item.name} disabled={working||shared.preference_version===undefined} onClick={()=>void toggle(item)}><span className="knowledge-toggle-track" aria-hidden="true"><span/></span><span>{enabled?'本次对话使用':'本次对话已禁用'}</span></button>}<button disabled={working} className="knowledge-unshare" aria-label={'取消共享 '+item.name} onClick={()=>void unshare(item.id)}>取消共享</button></div>
    </div>;
   })}{!activeRules.length&&<div className="empty-note">暂无共享澄清。在澄清问题提交时选择保存到项目即可复用。</div>}</div>
   <p className="muted small-text">“取消共享”会停止在项目后续会话中复用该规则；已保存的成果和来源记录仍保留。</p>
   <h3>项目样例</h3>{shared.samples.length?<ul>{shared.samples.map(item=><li key={item.profile_id}>{item.profile_name} · {item.count} 条样例 · v{item.version}</li>)}</ul>:<p className="empty-note">暂无项目样例。可从用例结果选择“保存为项目样例”。</p>}<p className="muted small-text">样例仅指导字段和写法，可在 Profile 设置中查看和移除。</p>
   <details className="knowledge-legacy"><summary>长期信息 · {items.length}</summary><p className="muted small-text">这些是项目保存的长期信息，不代表本次已使用的需求依据；实际引入的历史规则会在对话中列出。</p><div className="memory-list">{items.map(item=><div className="memory-row" key={item.id}><span><strong>{item.kind==='business'?'业务规则':'偏好'}</strong><p>{item.content}</p></span><button className="icon-button" aria-label={'删除 '+item.content} disabled={working} onClick={()=>void remove(item)}><Trash2 size={16}/></button></div>)}{!items.length&&<div className="empty-note">暂无项目记忆。</div>}</div><label>记忆类型<select aria-label="记忆类型" value={kind} onChange={e=>setKind(e.target.value as 'preference'|'business')}><option value="preference">偏好</option><option value="business">业务规则</option></select></label><label>记忆内容<textarea aria-label="记忆内容" rows={3} value={content} onChange={e=>setContent(e.target.value)}/></label><div className="dialog-actions"><button className="primary" disabled={working||!content.trim()} onClick={()=>void save()}>{working?<Spinner/>:'保存记忆'}</button></div></details>
  </>}
 </Dialog>;
}
