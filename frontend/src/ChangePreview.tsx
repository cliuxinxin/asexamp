import {useState} from 'react';
import {diffText,equalConfig} from './profile-diff';
import {TextValue} from './ui';
import type {Artifact,Json} from './types';

const labels:Record<string,string>={title:'标题',description:'描述',preconditions:'前置条件',steps:'步骤 / 预期结果',priority:'优先级',type:'类型',refs:'需求依据',scenario_id:'关联场景',requirement_ids:'关联需求'};
const relation=(field:string,value:unknown)=>(field==='scenario_id'&&value==='')||(field==='requirement_ids'&&Array.isArray(value)&&!value.length);

function FieldValue({field,value}:{field:string;value:unknown}){
 if(relation(field,value))return <span className="muted">N/A</span>;
 if(value===undefined)return <span className="muted">—</span>;
 if(field==='steps'&&Array.isArray(value))return <ol className="action-step-list">{value.map((step:Json,i:number)=><li key={i}><p><strong>操作：</strong><TextValue value={step?.action}/></p><p><strong>预期：</strong><TextValue value={step?.expected}/></p></li>)}</ol>;
 return <TextValue value={value}/>;
}
function ChangedValue({field,before,after,side}:{field:string;before:unknown;after:unknown;side:'before'|'after'}){
 const value=side==='before'?before:after;
 if(typeof before==='string'&&typeof after==='string'&&!relation(field,before)&&!relation(field,after))return <span className="preserve">{diffText(before,after).filter(part=>part.kind!==(side==='before'?'add':'remove')).map((part,index)=>part.kind==='same'?<span key={index}>{part.text}</span>:part.kind==='remove'?<del key={index}>{part.text}</del>:<ins key={index}>{part.text}</ins>)}</span>;
 return side==='before'?<del className="artifact-diff-value"><FieldValue field={field} value={value}/></del>:<ins className="artifact-diff-value"><FieldValue field={field} value={value}/></ins>;
}

// Keep the preview scoped to the supplied version, including older operations-only receipts.
export function ChangePreview({change,before}:{change:Json;before?:Artifact}){
 const [visible,setVisible]=useState(25);
 const prior=new Map<string,Json>((before?.items??change.before_items??[]).map((item:Json)=>[String(item.id),item]));
 const next=new Map<string,Json>(Array.isArray(change.items)?change.items.map((item:Json)=>[String(item.id),item]):prior);
 if(!Array.isArray(change.items))for(const operation of change.operations??[]){const id=String(operation.id??operation.item?.id??'');if(operation.op==='delete')next.delete(id);else if(operation.op==='add'||operation.op==='update')next.set(id,{...(next.get(id)??{}),...operation.item});}
 const ids=[...new Set([...prior.keys(),...next.keys()])];const altered=ids.filter(id=>!equalConfig(prior.get(id),next.get(id)));
 const added=altered.filter(id=>!prior.has(id)).length,removed=altered.filter(id=>!next.has(id)).length;
 return <section className="artifact-change"><h4>{change.title??before?.title??change.artifact_id} · v{change.expected_revision} → 新版本</h4><p className="muted small-text">新增 {added} · 更新 {altered.length-added-removed} · 删除 {removed}</p>{altered.slice(0,visible).map(id=>{
  const old=prior.get(id),item=next.get(id);const fields=[...new Set([...Object.keys(old??{}),...Object.keys(item??{})])].filter(key=>!key.startsWith('_')&&!equalConfig(old?.[key],item?.[key]));const operation=(change.operations??[]).find((op:Json)=>op.id===id&&op.op==='delete');
  return <div className="item-change" key={id}><strong>{id} · {item?.title??old?.title} · {!old?'新增':!item?'删除':'更新'}</strong>{operation?.reason&&<p className="action-deletion-reason">删除原因：{operation.reason}</p>}<div className="table-scroll"><table aria-label={id+' 修改对比'} className="artifact-change-table"><thead><tr><th scope="col">字段</th><th scope="col">修改前</th><th scope="col">修改后</th></tr></thead><tbody>{fields.map(field=><tr key={field}><th scope="row">{labels[field]??field}</th><td><ChangedValue field={field} before={old?.[field]} after={item?.[field]} side="before"/></td><td><ChangedValue field={field} before={old?.[field]} after={item?.[field]} side="after"/></td></tr>)}</tbody></table></div></div>;
 })}{altered.length>visible&&<button onClick={()=>setVisible(old=>old+25)}>再查看 {Math.min(25,altered.length-visible)} 条修改</button>}{!altered.length&&<p className="muted">条目内容没有变化。</p>}{change.report&&!equalConfig(change.report,before?.report??change.before_report)&&<details><summary>查看报告更改</summary><div className="action-report-diff"><div><strong>修改前</strong><TextValue value={before?.report??change.before_report??{}}/></div><div><strong>修改后</strong><TextValue value={change.report}/></div></div></details>}</section>;
}
