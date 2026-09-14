import {useEffect,useRef,useState} from 'react';
import {api,errText} from './api';
import {Dialog,ErrorBox,Spinner} from './ui';
import type {Profile} from './types';
import {ProfileConfigDiff} from './ProfileConfigDiff';

type ProfileChange={key:string;label:string;before:unknown;after:unknown;before_present:boolean;after_present:boolean};
type ProfilePreview={prompt_id:string;profile_id:string;profile_name:string;expected_version:number;summary:string;template_ids:string[];changes:ProfileChange[]};
type ApplyResult={status:'succeeded';profile:Profile;message:string};
// Loading a comparison never mutates the Profile or sends a model request.
export function ProfileChangeDialog({chatId,promptId,onClose,onApplied}:{chatId:string;promptId:string;onClose:()=>void;onApplied:(result:ApplyResult)=>void|Promise<void>}){
 const [preview,setPreview]=useState<ProfilePreview>();
 const [selected,setSelected]=useState<string[]>([]);
 const [loading,setLoading]=useState(true);const [working,setWorking]=useState(false);
 const [error,setError]=useState('');const [saved,setSaved]=useState(false);const [notice,setNotice]=useState('');
 const inFlight=useRef(false);const alive=useRef(true);
 useEffect(()=>{alive.current=true;return()=>{alive.current=false;};},[]);
 useEffect(()=>{
  let active=true;setLoading(true);setError('');setPreview(undefined);setSelected([]);setSaved(false);setNotice('');
  const params=new URLSearchParams({prompt_id:promptId});
  api<ProfilePreview>('/chats/'+encodeURIComponent(chatId)+'/profile-change?'+params).then(value=>{
   if(!active)return;
   if(value.prompt_id!==promptId)throw new Error('模板建议已改变，请关闭后重新查看。');
   setPreview(value);setSelected(value.changes.map(change=>change.key));
  }).catch(e=>{if(active)setError(errText(e));}).finally(()=>{if(active)setLoading(false);});
  return()=>{active=false;};
 },[chatId,promptId]);
 async function apply(){
  if(!preview||!selected.length||inFlight.current||saved)return;
  inFlight.current=true;setWorking(true);setError('');
  try{
   const result=await api<ApplyResult>('/chats/'+encodeURIComponent(chatId)+'/profile-change/apply',{
    prompt_id:preview.prompt_id,expected_version:preview.expected_version,selected_keys:selected,
   });
   if(!alive.current)return;
   setSaved(true);setNotice(result.message||'已应用所选更改。');
   await onApplied(result);
  }catch(e){if(alive.current)setError(errText(e));}
  finally{inFlight.current=false;if(alive.current)setWorking(false);}
 }
 const allSelected=!!preview?.changes.length&&selected.length===preview.changes.length;
 return <Dialog title="查看 Profile 更改" onClose={onClose} closeDisabled={working} wide><div className="profile-change-preview">
  {loading?<p role="status"><Spinner/> 正在读取更改对比…</p>:preview&&<>
   <p><strong>{preview.profile_name}</strong> · 当前版本 v{preview.expected_version}</p>
   {preview.summary&&<p className="preserve">{preview.summary}</p>}
   <p className="muted small-text">勾选要应用的更改；未勾选项保留原配置。导出列及其顺序作为整组确认。</p>
   {preview.changes.length?<>
    <label className="check-label"><input type="checkbox" aria-label="选择全部更改" checked={allSelected} disabled={working||saved} onChange={e=>setSelected(e.target.checked?preview.changes.map(change=>change.key):[])}/>选择全部更改（已选 {selected.length} / {preview.changes.length} 项）</label>
    <p className="profile-diff-legend"><ins>新增内容</ins><del>删除内容</del><span>未高亮部分保持不变</span></p>
    <div className="table-scroll"><table className="profile-change-table profile-change-diff-table" aria-label="Profile 更改对比"><thead><tr><th scope="col">应用</th><th scope="col">设置</th><th scope="col">变更内容</th></tr></thead><tbody>{preview.changes.map(change=><tr key={change.key} className={selected.includes(change.key)?'is-selected':''}>
     <td><input type="checkbox" aria-label={'应用'+change.label} disabled={working||saved} checked={selected.includes(change.key)} onChange={e=>setSelected(old=>e.target.checked?[...old,change.key]:old.filter(key=>key!==change.key))}/></td>
     <th scope="row">{change.label}<small className="profile-field-key">{change.key}</small></th>
     <td><ProfileConfigDiff before={change.before} after={change.after} beforePresent={change.before_present} afterPresent={change.after_present} field={change.key}/></td>
    </tr>)}</tbody></table></div>
   </>:<p>该模板与当前 Profile 一致，无需更改。</p>}
  </>}
  <ErrorBox message={error}/>{notice&&<p role="status" className="success">{notice}</p>}
  <div className="dialog-actions"><button disabled={working} onClick={onClose}>{saved?'关闭':'暂不应用'}</button><button className="primary" disabled={loading||working||saved||!preview||!selected.length} onClick={()=>void apply()}>{working&&<Spinner/>}确认应用所选更改</button></div>
 </div></Dialog>;
}
