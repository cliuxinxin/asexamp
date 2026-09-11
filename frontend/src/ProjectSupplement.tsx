import {useState} from 'react';
import {api,errText} from './api';
import {useConversationCommand} from './conversation';
import {Dialog,ErrorBox,Spinner} from './ui';
import type {Run,Source} from './types';

export function ProjectSupplement({run,sources=[],onClose,onChanged,onBusy,disabled=false}:{run:Run;disabled?:boolean;sources?:Source[];onClose:()=>void;onChanged:()=>void;onBusy:(busy:boolean)=>void}){
 const command=useConversationCommand();
 const [content,setContent]=useState(''),[selected,setSelected]=useState<string[]>([]),[uploads,setUploads]=useState<Source[]>([]),[working,setWorking]=useState(false),[error,setError]=useState('');
 const choices=[...sources,...uploads.filter(source=>!sources.some(existing=>existing.id===source.id))];
 const setBusy=(value:boolean)=>{setWorking(value);onBusy(value);};
 async function upload(files:FileList|null){
  if(!files?.length||!run.chat_id||disabled)return;
  setBusy(true);setError('');
  let uploaded=false;
  try{for(const file of Array.from(files)){const form=new FormData();form.append('file',file);form.append('role','supplement');const source=await api<Source>('/chats/'+run.chat_id+'/sources',form);uploaded=true;setUploads(old=>[...old,source]);setSelected(old=>[...new Set([...old,source.id])]);}}
  catch(e){setError(errText(e));}finally{if(uploaded)onChanged();setBusy(false);}
 }
 async function submit(){
  if(disabled)return;
  setBusy(true);setError('');
  try{const result=await command({name:'project.update_from_sources',arguments:{run_id:run.id,source_ids:selected,content:content.trim(),targets:['analysis','scenarios'],instruction:'结合补充资料更新受影响需求与场景，保持当前任务停止条件'}},'更新受影响需求与场景',undefined,run.chat_id);if(result.status!=='succeeded'&&result.status!=='deferred')throw new Error(result.message);onChanged();onClose();}
  catch(e){setError(errText(e));}finally{setBusy(false);}
 }
 return <Dialog closeDisabled={working} title="补充资料并更新" onClose={()=>{if(!working)onClose();}}>
  <p>结合补充资料分析影响，更新相关需求理解与场景，保留其他成果和当前停止条件。</p>
  <label>补充需求内容<textarea aria-label="补充需求内容" rows={4} value={content} disabled={working||disabled} onChange={e=>setContent(e.target.value)} placeholder="例如：新增管理员批量解锁的业务规则…"/></label>
  {choices.length>0&&<fieldset><legend>选择要应用的附件</legend>{choices.map(source=><label className="check-label" key={source.id}><input type="checkbox" disabled={working||disabled} checked={selected.includes(source.id)} onChange={e=>setSelected(old=>e.target.checked?[...old,source.id]:old.filter(id=>id!==source.id))}/>{source.name}</label>)}</fieldset>}
  {run.chat_id&&<label>添加补充附件<input aria-label="添加补充附件" type="file" multiple disabled={working||disabled} onChange={e=>{void upload(e.target.files);e.target.value='';}}/></label>}
  <p className="muted small-text">上传的附件保留在本会话；点击应用后才用于更新需求。</p>
  <div className="dialog-actions"><button disabled={working||disabled} onClick={onClose}>取消</button><button className="primary" disabled={working||disabled||(!content.trim()&&!selected.length)} onClick={submit}>{working?<Spinner/>:'更新受影响需求与场景'}</button></div><ErrorBox message={error}/>
 </Dialog>;
}
