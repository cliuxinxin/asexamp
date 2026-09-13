import {useEffect,useState} from 'react';
import {api,errText} from './api';
import {useConversationCommand} from './conversation';
import {Dialog,ErrorBox,Spinner} from './ui';
import type {Artifact,Profile} from './types';

type ProjectFact={id:string;name:string;text:string;status:'confirmed'|'provisional'|'superseded';scope:{module?:string;version?:string};created_at:string;chat_id:string;fact_key?:string;supersedes:string[];superseded_by?:string;provenance:{confirmed_by?:string;confirmed_at?:string;message_id?:string;run_id?:string;chat_id?:string}};
type FactContext={clarifications:ProjectFact[];fact_history?:ProjectFact[]};

export function ProjectFacts({projectId}:{projectId:string}){
 const [open,setOpen]=useState(false),[loading,setLoading]=useState(false),[working,setWorking]=useState(false),[error,setError]=useState(''),[context,setContext]=useState<FactContext>({clarifications:[]});
 useEffect(()=>{if(!open)return;let active=true;setLoading(true);setError('');api<FactContext>('/projects/'+projectId+'/shared-context').then(result=>{if(active)setContext(result);}).catch(e=>{if(active)setError(errText(e));}).finally(()=>{if(active)setLoading(false);});return()=>{active=false;};},[open,projectId]);
 async function remove(id:string){setWorking(true);setError('');try{await api('/projects/'+projectId+'/shared-context/'+id,undefined,'DELETE');setContext(old=>({...old,clarifications:old.clarifications.filter(item=>item.id!==id)}));}catch(e){setError(errText(e));}finally{setWorking(false);}}
 function rule(item:ProjectFact,history=false){const origin=item.provenance??{},scope=item.scope??{};return <article className="memory-row" key={item.id}>
  <div><strong>{item.fact_key||item.name}</strong><div className="muted small-text">{history?'已替代':'已确认'} · {scope.module||'本项目'}{scope.version?' / '+scope.version:''}</div><p className="preserve">{item.text}</p>
   <details><summary>确认来源与历史</summary><p className="muted small-text">{origin.confirmed_by||'历史提交用户'} · {new Date(origin.confirmed_at||item.created_at).toLocaleString()}</p><dl className="small-text"><dt>来源对话</dt><dd>{origin.chat_id||item.chat_id}</dd>{origin.message_id&&<><dt>确认消息</dt><dd>{origin.message_id}</dd></>}{origin.run_id&&<><dt>来源任务</dt><dd>{origin.run_id}</dd></>}<dt>依据来源</dt><dd>{item.id}</dd>{!!item.supersedes?.length&&<><dt>替代了</dt><dd>{item.supersedes.join('、')}</dd></>}{item.superseded_by&&<><dt>后续规则</dt><dd>{item.superseded_by}</dd></>}</dl></details>
  </div>{!history&&<button disabled={working} aria-label={'取消共享 '+(item.fact_key||item.name)} onClick={()=>remove(item.id)}>取消共享</button>}
 </article>;}
 return <><button onClick={()=>setOpen(true)}>已确认规则</button>{open&&<Dialog title="项目已确认规则" onClose={()=>{if(!working)setOpen(false);}}>
  <p>同一项目的新任务可复用适用的已确认规则。当前任务回答保存后即可使用；阶段仍需确认。</p>
  {loading?<Spinner/>:<><div className="memory-list">{context.clarifications.map(item=>rule(item))}{!context.clarifications.length&&<p className="empty-note">暂无已确认规则。在聊天中明确确认业务结论后会保存到这里。</p>}</div>{!!context.fact_history?.length&&<details><summary>已替代规则（{context.fact_history.length}）</summary><div className="memory-list">{context.fact_history.map(item=>rule(item,true))}</div></details>}<p className="muted small-text">“先按假设继续”只用于当前任务。规则更新保留历史依据；表达规范与用例样例保存在 Profile。</p></>}
  <ErrorBox message={error}/><div className="dialog-actions"><button disabled={working} onClick={()=>setOpen(false)}>关闭</button></div>
 </Dialog>}</>;
}

export function PinSamples({artifact,selectedIds,onChanged}:{artifact:Artifact;selectedIds:string[];onChanged:()=>void}){
 const command=useConversationCommand();
 const [open,setOpen]=useState(false),[profiles,setProfiles]=useState<Profile[]>([]),[profileId,setProfileId]=useState(''),[selected,setSelected]=useState<string[]>([]),[working,setWorking]=useState(false),[loading,setLoading]=useState(false),[error,setError]=useState(''),[saved,setSaved]=useState('');
 useEffect(()=>{if(!open)return;let active=true;setLoading(true);setError('');api<{profiles:Profile[]}>('/artifacts/'+artifact.id+'/export-options').then(result=>{if(active){setProfiles(result.profiles??[]);setProfileId(result.profiles?.[0]?.id??'');}}).catch(e=>{if(active)setError(errText(e));}).finally(()=>{if(active)setLoading(false);});return()=>{active=false;};},[open,artifact.id]);
 function show(){setSelected((selectedIds.length?selectedIds:artifact.items.map(item=>String(item.id))).slice(0,5));setSaved('');setOpen(true);}
 async function save(){const profile=profiles.find(item=>item.id===profileId);if(!profile)return;setWorking(true);setError('');try{const result=await command({name:'project.pin_samples',arguments:{profile_id:profile.id,expected_version:profile.version,selected_ids:selected}},'将选中的用例保存为项目样例',artifact);if(result.status!=='succeeded')throw new Error(result.message);setOpen(false);setSaved('已保存到 '+profile.name);onChanged();}catch(e){setError(errText(e));}finally{setWorking(false);}}
 return <><button disabled={!artifact.items.length} onClick={show}>保存为项目样例</button>{saved&&<span className="muted small-text" role="status">{saved}</span>}{open&&<Dialog title="保存为项目样例" onClose={()=>{if(!working)setOpen(false);}}>
  <p>将选中的用例保存到项目 Profile，供后续生成学习字段和写法。样例不会作为业务需求依据。</p>
  {loading?<Spinner/>:<><label>样例目标 Profile<select aria-label="样例目标 Profile" value={profileId} disabled={working} onChange={e=>setProfileId(e.target.value)}>{profiles.map(profile=><option key={profile.id} value={profile.id}>{profile.name} · v{profile.version}</option>)}</select></label>{!profiles.length&&<p className="empty-note">本项目暂无可用 Profile，请先在设置中创建。</p>}
  <fieldset><legend>选择样例（{selected.length}/5）</legend>{artifact.items.map(item=><label className="check-label" key={item.id}><input type="checkbox" aria-label={'样例 '+item.id} checked={selected.includes(String(item.id))} disabled={working||(!selected.includes(String(item.id))&&selected.length>=5)} onChange={e=>setSelected(old=>e.target.checked?[...old,String(item.id)]:old.filter(id=>id!==String(item.id)))}/>{item.id} · {item.title}</label>)}</fieldset>
  <p className="muted small-text">会替换目标 Profile 原有样例；最多 5 条、共 12,000 字符。可在 Profile 设置中查看和移除。</p></>}
  <div className="dialog-actions"><button disabled={working} onClick={()=>setOpen(false)}>取消</button><button className="primary" disabled={working||loading||!profileId||!selected.length} onClick={save}>{working?<Spinner/>:'保存样例'}</button></div><ErrorBox message={error}/>
 </Dialog>}</>;
}
