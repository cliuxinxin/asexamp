import {useEffect,useState} from 'react';
import {api,errText} from './api';
import {Dialog,ErrorBox,Spinner} from './ui';
import type {Artifact,Profile} from './types';

export function PinSamples({artifact,selectedIds,onChanged}:{artifact:Artifact;selectedIds:string[];onChanged:()=>void}){
 const [open,setOpen]=useState(false),[profiles,setProfiles]=useState<Profile[]>([]),[profileId,setProfileId]=useState(''),[selected,setSelected]=useState<string[]>([]),[working,setWorking]=useState(false),[loading,setLoading]=useState(false),[error,setError]=useState(''),[saved,setSaved]=useState('');
 useEffect(()=>{if(!open)return;let active=true;setLoading(true);setError('');api<{profiles:Profile[]}>('/artifacts/'+artifact.id+'/export-options').then(result=>{if(active){setProfiles(result.profiles??[]);setProfileId(result.profiles?.[0]?.id??'');}}).catch(e=>{if(active)setError(errText(e));}).finally(()=>{if(active)setLoading(false);});return()=>{active=false;};},[open,artifact.id]);
 function show(){setSelected((selectedIds.length?selectedIds:artifact.items.map(item=>String(item.id))).slice(0,5));setSaved('');setOpen(true);}
 async function save(){const profile=profiles.find(item=>item.id===profileId);if(!profile)return;setWorking(true);setError('');try{await api('/artifacts/'+artifact.id+'/pin-samples',{profile_id:profile.id,expected_version:profile.version,selected_ids:selected});setOpen(false);setSaved('已保存到 '+profile.name);onChanged();}catch(e){setError(errText(e));}finally{setWorking(false);}}
 return <><button disabled={!artifact.items.length} onClick={show}>保存为项目样例</button>{saved&&<span className="muted small-text" role="status">{saved}</span>}{open&&<Dialog title="保存为项目样例" onClose={()=>{if(!working)setOpen(false);}}>
  <p>将选中的用例保存到项目 Profile，供后续生成学习字段和写法。样例不会作为业务需求依据。</p>
  {loading?<Spinner/>:<><label>样例目标 Profile<select aria-label="样例目标 Profile" value={profileId} disabled={working} onChange={e=>setProfileId(e.target.value)}>{profiles.map(profile=><option key={profile.id} value={profile.id}>{profile.name} · v{profile.version}</option>)}</select></label>{!profiles.length&&<p className="empty-note">本项目暂无可用 Profile，请先在设置中创建。</p>}
  <fieldset><legend>选择样例（{selected.length}/5）</legend>{artifact.items.map(item=><label className="check-label" key={item.id}><input type="checkbox" aria-label={'样例 '+item.id} checked={selected.includes(String(item.id))} disabled={working||(!selected.includes(String(item.id))&&selected.length>=5)} onChange={e=>setSelected(old=>e.target.checked?[...old,String(item.id)]:old.filter(id=>id!==String(item.id)))}/>{item.id} · {item.title}</label>)}</fieldset>
  <p className="muted small-text">会替换目标 Profile 原有样例；最多 5 条、共 12,000 字符。可在 Profile 设置中查看和移除。</p></>}
  <div className="dialog-actions"><button disabled={working} onClick={()=>setOpen(false)}>取消</button><button className="primary" disabled={working||loading||!profileId||!selected.length} onClick={save}>{working?<Spinner/>:'保存样例'}</button></div><ErrorBox message={error}/>
 </Dialog>}</>;
}
