import {Plus,Trash2} from 'lucide-react';
import type {Json} from './types';

export function canEditItems(value:unknown,kind:string):value is Json[]{
 const isObject=(item:unknown):item is Json=>!!item&&typeof item==='object'&&!Array.isArray(item);
 const stringFields=(item:Json,fields:string[])=>fields.every(field=>item[field]==null||typeof item[field]==='string');
 return Array.isArray(value)&&value.every(item=>
  isObject(item)&&typeof item.id==='string'&&
  stringFields(item,['title','type','priority','preconditions','description','content'])&&
  (item.refs==null||(Array.isArray(item.refs)&&item.refs.every((ref:unknown)=>typeof ref==='string')))&&
  (kind!=='cases'||(Array.isArray(item.steps)&&item.steps.every((step:unknown)=>isObject(step)&&stringFields(step,['action','expected']))))
 );
}

export function ItemEditor({items,kind,onChange}:{items:Json[];kind:string;onChange:(items:Json[])=>void}){
 function update(index:number,patch:Json){onChange(items.map((item,i)=>i===index?{...item,...patch}:item));}
 return <div className="item-editors">{items.map((item,index)=><section className="item-editor" key={item.id??index}><div className="item-editor-head"><strong>{item.id}</strong><button className="icon-button" aria-label={'删除条目 '+item.id} onClick={()=>onChange(items.filter((_,i)=>i!==index))}><Trash2 size={16}/></button></div><label>标题<input aria-label={'条目 '+item.id+' 标题'} value={item.title??''} onChange={e=>update(index,{title:e.target.value})}/></label>{kind==='cases'?<><div className="field-row"><label>类型<input aria-label={'条目 '+item.id+' 类型'} value={item.type??''} onChange={e=>update(index,{type:e.target.value})}/></label><label>优先级<input aria-label={'条目 '+item.id+' 优先级'} value={item.priority??''} onChange={e=>update(index,{priority:e.target.value})}/></label></div><label>前置条件<textarea aria-label={'条目 '+item.id+' 前置条件'} rows={2} value={item.preconditions??''} onChange={e=>update(index,{preconditions:e.target.value})}/></label>{(item.steps??[]).map((step:Json,stepIndex:number)=><div className="step-editor" key={stepIndex}><div className="item-editor-head"><span>步骤 {stepIndex+1}</span><button className="icon-button" aria-label={'删除 '+item.id+' 步骤 '+(stepIndex+1)} onClick={()=>update(index,{steps:item.steps.filter((_:unknown,i:number)=>i!==stepIndex)})}><Trash2 size={14}/></button></div><label>操作<textarea aria-label={item.id+' 操作 '+(stepIndex+1)} rows={2} value={step.action??''} onChange={e=>update(index,{steps:item.steps.map((s:Json,i:number)=>i===stepIndex?{...s,action:e.target.value}:s)})}/></label><label>预期结果<textarea aria-label={item.id+' 预期结果 '+(stepIndex+1)} rows={2} value={step.expected??''} onChange={e=>update(index,{steps:item.steps.map((s:Json,i:number)=>i===stepIndex?{...s,expected:e.target.value}:s)})}/></label></div>)}<button className="text-accent" onClick={()=>update(index,{steps:[...(item.steps??[]),{action:'',expected:''}]})}><Plus size={14}/>添加步骤</button></>:<><label>描述<textarea aria-label={'条目 '+item.id+' 描述'} rows={3} value={item.description??item.content??''} onChange={e=>update(index,{description:e.target.value})}/></label>{kind==='scenarios'&&<label>优先级<input aria-label={'条目 '+item.id+' 优先级'} value={item.priority??''} onChange={e=>update(index,{priority:e.target.value})}/></label>}</>}<small className="muted">依据：{(item.refs??[]).join(' · ')||'无引用'}。其他字段保留，可在 JSON 中修改。</small></section>)}</div>;
}
