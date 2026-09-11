import {useState} from 'react';
import {AnalysisReport} from './AnalysisReport';
import {api,errText} from './api';
import {ErrorBox,Spinner} from './ui';
import type {Artifact} from './types';

export function StageArtifact({id,revision,stage,onOpenLatest}:{id:string;revision:number;stage:string;onOpenLatest?:(artifact:Artifact)=>void}){
 const [data,setData]=useState<Artifact>();const [error,setError]=useState('');const [loading,setLoading]=useState(false);
 const title=({understand:'需求理解与业务图',scenarios:'测试场景',cases:'生成时的用例草稿',review:'评审结果'} as Record<string,string>)[stage]??'阶段成果';
 async function load(){if(data||loading)return;setLoading(true);setError('');try{setData(await api<Artifact>(`/artifacts/${encodeURIComponent(id)}/revisions/${revision}`));}catch(e){setError(errText(e));}finally{setLoading(false);}}
 async function openLatest(){setLoading(true);setError('');try{onOpenLatest?.(await api<Artifact>('/artifacts/'+encodeURIComponent(id)));}catch(e){setError(errText(e));}finally{setLoading(false);}}
 return <details className="stage-artifact" onToggle={e=>{if(e.currentTarget.open)load();}}><summary>查看{title} · v{revision}</summary>{loading&&<p><Spinner/>正在读取已保存的阶段成果…</p>}<ErrorBox message={error}/>{error&&<button onClick={load}>重新读取</button>}{data&&<><p className="muted small-text">这是当前阶段保存的版本，历史内容保持不变。</p>{onOpenLatest&&<button disabled={loading} onClick={openLatest}>打开最新版本并编辑</button>}{data.report&&<AnalysisReport report={data.report}/>}<div className="table-scroll"><table><thead><tr><th>编号</th><th>内容</th></tr></thead><tbody>{data.items.map(item=><tr key={item.id}><td>{item.id}</td><td><strong>{item.title}</strong>{item.description&&<p>{item.description}</p>}{Array.isArray(item.steps)&&<ol>{item.steps.map((s:any,i:number)=><li key={i}>{s.action}<br/>预期：{s.expected}</li>)}</ol>}</td></tr>)}</tbody></table></div></>}</details>;
}
