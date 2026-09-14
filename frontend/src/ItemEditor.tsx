import {Trash2} from 'lucide-react';
import {TableItemEditor} from './TableItemEditor';
import type {ColumnChanges} from './TableItemEditor';
import type {Json} from './types';

export function canEditItems(value:unknown,kind:string):value is Json[]{
 const isObject=(item:unknown):item is Json=>!!item&&typeof item==='object'&&!Array.isArray(item);
 const stringFields=(item:Json,fields:string[])=>fields.every(field=>item[field]==null||typeof item[field]==='string');
 return Array.isArray(value)&&value.every(item=>
  isObject(item)&&typeof item.id==='string'&&
  stringFields(item,['title','type','priority','preconditions','description','content','scenario_id'])&&
  (item.refs==null||(Array.isArray(item.refs)&&item.refs.every((ref:unknown)=>typeof ref==='string')))&&
  (kind!=='scenarios'||item.requirement_ids==null||(Array.isArray(item.requirement_ids)&&item.requirement_ids.every((id:unknown)=>typeof id==='string')))&&
  (kind!=='cases'||(Array.isArray(item.steps)&&item.steps.every((step:unknown)=>isObject(step)&&stringFields(step,['action','expected']))))
 );
}

export function ItemEditor({items,kind,columns=[],columnChanges,onColumnChanges,onChange}:{items:Json[];kind:string;columns?:Json[];columnChanges?:ColumnChanges;onColumnChanges?:(changes:ColumnChanges)=>void;onChange:(items:Json[])=>void}){
 function update(index:number,patch:Json){onChange(items.map((item,i)=>i===index?{...item,...patch}:item));}
 if(['cases','scenarios'].includes(kind))return <TableItemEditor items={items} kind={kind} columns={columns} columnChanges={columnChanges} onColumnChanges={onColumnChanges} onChange={onChange}/>;
 return <div className="item-editors">{items.map((item,index)=><section className="item-editor" key={item.id??index}>
  <div className="item-editor-head"><strong>{item.id}</strong><button className="icon-button" aria-label={'删除条目 '+item.id} onClick={()=>onChange(items.filter((_,i)=>i!==index))}><Trash2 size={16}/></button></div>
  <label>标题<input aria-label={'条目 '+item.id+' 标题'} value={item.title??''} onChange={e=>update(index,{title:e.target.value})}/></label>
  <label>描述<textarea aria-label={'条目 '+item.id+' 描述'} rows={3} value={item.description??item.content??''} onChange={e=>update(index,{description:e.target.value})}/></label>
  <small className="muted">依据：{(item.refs??[]).join(' · ')||'无引用'}。其他字段保留，可在 JSON 中修改。</small>
 </section>)}</div>;
}
