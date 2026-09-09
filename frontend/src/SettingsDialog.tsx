import {useEffect,useState} from 'react';
import {Check,PlugZap,Plus,Save} from 'lucide-react';
import {api,errText} from './api';
import {Dialog,ErrorBox,Spinner} from './ui';
import type {Json,Profile,Settings} from './types';

export function SettingsDialog({projectId,profiles,activeProfileId,proposal,onClose,onSaved,onSelectProfile}:{projectId:string;profiles:Profile[];activeProfileId?:string;proposal?:Json;onClose:()=>void;onSaved:()=>void;onSelectProfile?:(id:string)=>void}){
 const [tab,setTab]=useState(proposal?'profile':'model');
 const [settings,setSettings]=useState<Settings>();
 const [key,setKey]=useState('');const [clearKey,setClearKey]=useState(false);
 const [headers,setHeaders]=useState('');const [clearHeaders,setClearHeaders]=useState(false);
 const initialProfile=profiles.find(p=>p.id===activeProfileId)??profiles[0];
 const [profileId,setProfileId]=useState(initialProfile?.id??'');const [name,setName]=useState(initialProfile?.name??'默认偏好');
 const [config,setConfig]=useState(JSON.stringify(proposal??initialProfile?.config??{},null,2));
 const [error,setError]=useState('');const [notice,setNotice]=useState('');const [working,setWorking]=useState(false);
 useEffect(()=>{let active=true;api<Settings>('/settings').then(value=>active&&setSettings(value)).catch(e=>active&&setError(errText(e)));return()=>{active=false;};},[]);
 function changeEndpoint(provider:Settings['provider'],base_url:string){
  if(!settings)return;
  setSettings({...settings,provider,base_url,has_api_key:false,has_headers:false,header_names:[]});
  setKey('');setHeaders('');setClearKey(false);setClearHeaders(false);
 }
 async function saveModel(test=false){
  if(!settings)return;setWorking(true);setError('');setNotice('');
  try{
   if(!settings.environment_managed){
    let parsed:Record<string,string>|undefined;
    if(headers.trim()&&!clearHeaders){
     try{parsed=JSON.parse(headers);}catch{throw new Error('Header 格式错误，请填写 JSON 对象，例如 {"X-Tenant-ID":"your-tenant"}');}
     if(!parsed||Array.isArray(parsed)||typeof parsed!=='object'||Object.values(parsed).some(value=>typeof value!=='string'))throw new Error('每个 Header 的名称和值都必须是字符串。');
    }
    const updated=await api<Settings>('/settings',{
     provider:settings.provider,base_url:settings.base_url,model:settings.model,timeout_seconds:settings.timeout_seconds,
     auth_mode:settings.auth_mode??'bearer',...(key?{api_key:key}:{}),...(clearKey?{clear_api_key:true}:{}),
     ...(parsed?{headers:parsed}:{}),...(clearHeaders?{clear_headers:true}:{}),
    },'PUT');
    setSettings(updated);setKey('');setClearKey(false);setHeaders('');setClearHeaders(false);
   }
   if(test){const result=await api('/settings/test',{});if(!result.ok)throw new Error(result.message);setNotice(result.message||'连接成功');}
   else setNotice('模型配置已保存在本机');
   onSaved();
  }catch(e){setError(errText(e));}finally{setWorking(false);}
 }
 async function saveProfile(create=false){
  setWorking(true);setError('');setNotice('');
  try{const value=JSON.parse(config);if(!value||Array.isArray(value)||typeof value!=='object')throw new Error('项目偏好必须是 JSON 对象');
   const selected=profiles.find(p=>p.id===profileId);
   if(create){const p=await api<Profile>('/projects/'+projectId+'/profiles',{name:name||'新偏好',config:value});setProfileId(p.id);onSelectProfile?.(p.id);}
   else await api('/profiles/'+profileId,{name,config:value,expected_version:selected?.version},'PUT');
   setNotice('项目偏好已保存');onSaved();
  }catch(e){setError(errText(e));}finally{setWorking(false);}
 }
 const managed=settings?.environment_managed;
 return <Dialog title="模型与设置" onClose={onClose} wide>
  <div className="tabs"><button className={tab==='model'?'active':''} onClick={()=>setTab('model')}>模型连接</button><button className={tab==='profile'?'active':''} onClick={()=>setTab('profile')}>项目偏好</button></div>
  {tab==='model'&&settings?<div className="settings-form">
   <p className="muted">连接本机 Ollama 或指定的 Chat Completions 网关。配置保存一次，后续对话会继续使用。</p>
   {managed&&<p role="status" className="environment-notice">配置由 .env 或环境变量管理，修改后请重启服务。{settings.env_file&&<>配置文件：{settings.env_file}</>}</p>}
   <label>模型服务<select disabled={managed} value={settings.provider} onChange={e=>{const provider=e.target.value as Settings['provider'];changeEndpoint(provider,provider==='ollama'?'http://127.0.0.1:11434':'http://10.206.3.151:8000/api/v1');}}><option value="ollama">Ollama · 本地模型</option><option value="openai">Chat Completions · 私有网关</option></select></label>
   <label>服务地址<input disabled={managed} value={settings.base_url} onChange={e=>changeEndpoint(settings.provider,e.target.value)} spellCheck={false}/></label>
   <label>模型名称<input disabled={managed} value={settings.model} onChange={e=>setSettings({...settings,model:e.target.value})} placeholder={settings.provider==='ollama'?'填写 ollama list 中已安装的模型名称':'填写服务支持的模型 ID'} spellCheck={false}/></label>
   {settings.provider==='openai'&&(settings.auth_mode??'bearer')==='bearer'&&<><label>API Key（本地服务可留空）<input disabled={managed} type="password" autoComplete="new-password" value={key} onChange={e=>setKey(e.target.value)} placeholder={settings.has_api_key?'已保存，留空保留':'可选'}/></label>{settings.has_api_key&&<label className="check-label"><input disabled={managed} type="checkbox" checked={clearKey} onChange={e=>setClearKey(e.target.checked)}/>清除已保存的密钥</label>}</>}
   <details className="header-settings"><summary>自定义请求头</summary>
    <p className="muted small-text">适用于网关密钥、租户标识和自定义鉴权。保存后不回显请求头的值。</p>
    <label>鉴权方式<select disabled={managed} value={settings.auth_mode??'bearer'} onChange={e=>setSettings({...settings,auth_mode:e.target.value as Settings['auth_mode']})}><option value="bearer">{settings.provider==='openai'?'API Key · X-API-Key':'API Key · Bearer'}</option><option value="headers">仅使用自定义 Header</option></select></label>
    {!!settings.header_names?.length&&<p className="saved-headers">已保存：{settings.header_names.join('、')} <span className="muted">（值已隐藏）</span></p>}
    <label>自定义 Header JSON<textarea className="headers-editor" disabled={managed||clearHeaders} spellCheck={false} autoComplete="off" value={headers} onChange={e=>setHeaders(e.target.value)} placeholder={'{"X-API-Key":"your-key","X-Tenant-ID":"your-tenant"}'}/></label>
    <p className="muted small-text">留空保留原配置；填写对象会替换全部 Header。更换服务地址后，请重新填写。</p>
    {settings.has_headers&&<label className="check-label"><input disabled={managed} type="checkbox" checked={clearHeaders} onChange={e=>setClearHeaders(e.target.checked)}/>清除已保存的请求头</label>}
   </details>
   <details className="timeout-settings"><summary>请求等待时间 · {settings.timeout_seconds} 秒</summary><label>单次请求超时（秒）<input type="number" min={5} max={3600} disabled={managed||settings.timeout_policy==='fixed_60_minutes'} value={settings.timeout_seconds} onChange={e=>setSettings({...settings,timeout_seconds:Number(e.target.value)})}/></label>{settings.timeout_policy==='fixed_60_minutes'&&<p className="muted small-text">模型请求超时统一为 60 分钟（3600 秒）；旧配置中的短超时也按此值执行。</p>}</details>
   <div className="dialog-actions">{!managed&&<button disabled={working} onClick={()=>saveModel()}><Save size={16}/>保存</button>}<button className="primary" disabled={working||!settings.model.trim()} onClick={()=>saveModel(true)}>{working?<Spinner/>:<PlugZap size={16}/>} {managed?'测试连接':'保存并测试连接'}</button></div>
  </div>:tab==='model'?<Spinner/>:null}
  {tab==='profile'&&<div><p className="muted">{proposal?'AI 提出了下面的偏好建议。你可以保存为新的项目偏好。':'设置常用语言、测试关注范围和业务规则。测试深度在每次对话中由 AI 推荐。'}</p>
   <label>选择 Profile<select value={profileId} onChange={e=>{const p=profiles.find(x=>x.id===e.target.value);if(!p)return;setProfileId(p.id);setName(p.name);if(!proposal)setConfig(JSON.stringify(p.config,null,2));}}>{profiles.map(p=><option key={p.id} value={p.id}>{p.name} · v{p.version}</option>)}</select></label>
   <label>Profile 名称<input value={name} onChange={e=>setName(e.target.value)}/></label><label>配置 JSON<textarea className="code-editor profile-editor" aria-label="Profile 配置 JSON" value={config} onChange={e=>setConfig(e.target.value)} spellCheck={false}/></label>
   <div className="dialog-actions">{onSelectProfile&&<button disabled={working||!profileId} onClick={()=>{onSelectProfile(profileId);setNotice('后续任务将使用此项目偏好');}}>使用此项目偏好</button>}<button disabled={working} onClick={()=>saveProfile(true)}><Plus size={16}/>保存为新 Profile</button><button className="primary" disabled={working||!profileId} onClick={()=>saveProfile(false)}>{working?<Spinner/>:<Save size={16}/>}更新当前 Profile</button></div>
  </div>}
  <ErrorBox message={error}/>{notice&&<p role="status" className="success inline"><Check size={16}/>{notice}</p>}
 </Dialog>;
}
