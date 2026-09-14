import {useState} from 'react';
import {Plus,Trash2} from 'lucide-react';
import {TextValue} from './ui';
import type {Json} from './types';

export type ColumnChanges={added:{field:string;header:string}[];removed:string[]};
export const EMPTY_COLUMN_CHANGES:ColumnChanges={added:[],removed:[]};
const CORE_FIELDS=new Set(['id','title','description','type','priority','preconditions','steps','expected','expected_result','scenario_id','requirement_ids','refs','branch_ids']);

// Keep all existing data keys, including custom columns absent from the export profile.
export function tableCustomColumns(items:Json[],columns:Json[]=[],changes:ColumnChanges=EMPTY_COLUMN_CHANGES):Json[]{
 const candidates=[...changes.added,...columns,...items.flatMap(item=>Object.keys(item).map(field=>({field,header:field})))];
 return candidates.filter((column,index)=>typeof column.field==='string'&&!CORE_FIELDS.has(column.field)&&!column.field.startsWith('_')&&!changes.removed.includes(column.field)&&candidates.findIndex(other=>other.field===column.field)===index).map(column=>({...column,header:column.header||column.field}));
}

export function newTableItem(kind:string,items:Json[]):Json{
 const prefix=kind==='cases'?'TC-NEW-':'SC-NEW-';let index=1;
 while(items.some(item=>item.id===prefix+index))index++;
 return {id:prefix+index,title:'新条目',description:'',type:'Business',priority:'P2',refs:[],...(kind==='cases'?{scenario_id:'',preconditions:'',steps:[{action:'',expected:''}]}:{requirement_ids:[]})};
}

export function TableItemEditor({items,kind,columns=[],columnChanges=EMPTY_COLUMN_CHANGES,onColumnChanges,onChange}:{items:Json[];kind:string;columns?:Json[];columnChanges?:ColumnChanges;onColumnChanges?:(changes:ColumnChanges)=>void;onChange:(items:Json[])=>void}){
 const [field,setField]=useState('');const [header,setHeader]=useState('');const [error,setError]=useState('');
 const customColumns=tableCustomColumns(items,columns,columnChanges);const isCase=kind==='cases';const canChangeColumns=isCase&&!!onColumnChanges;
 function update(index:number,patch:Json){onChange(items.map((item,i)=>i===index?{...item,...patch}:item));}
 function addColumn(){
  const key=field.trim();if(!key||CORE_FIELDS.has(key)||key.startsWith('_')||['__proto__','constructor','prototype'].includes(key)){setError('请输入新的自定义字段名，编号、关联和步骤等基础字段已由表格管理。');return;}
  if(customColumns.some(column=>column.field===key)){setError('该字段已经存在，可直接在表格中填写。');return;}
  const added={field:key,header:header.trim()||key};
  onColumnChanges?.({added:[...columnChanges.added.filter(column=>column.field!==key),added],removed:columnChanges.removed.filter(value=>value!==key)});
  onChange(items.map(item=>({...item,[key]:''})));setField('');setHeader('');setError('');
 }
 function removeColumn(key:string){
  const wasAdded=columnChanges.added.some(column=>column.field===key);
  onColumnChanges?.({added:columnChanges.added.filter(column=>column.field!==key),removed:wasAdded?columnChanges.removed:[...new Set([...columnChanges.removed,key])]});
  onChange(items.map(item=>{const next={...item};delete next[key];return next;}));
 }
 return <div className="table-item-editor">
  <div className="table-edit-toolbar"><p className="muted small-text">直接编辑单元格。关联留空表示 N/A；修改仅保存当前成果的新版本。</p>{canChangeColumns&&<div className="table-add-column"><label>字段名<input aria-label="新列字段名" placeholder="如 test_data" value={field} onChange={e=>setField(e.target.value)}/></label><label>列名称<input aria-label="新列名称" placeholder="如 测试数据" value={header} onChange={e=>setHeader(e.target.value)}/></label><button onClick={addColumn}><Plus size={15}/>添加列</button></div>}</div>
  {error&&<p role="alert" className="error-box">{error}</p>}
  {(columnChanges.added.length>0||columnChanges.removed.length>0)&&<p className="table-column-note" role="status">列的更改将与内容一起保存，并提出 Profile 同步建议供你确认。{columnChanges.removed.length>0&&' 删除的列仅在保存后生效，取消编辑可恢复。'}</p>}
  <div className="table-scroll table-edit-scroll"><table className="artifact-edit-table" aria-label={isCase?'用例表格编辑':'场景表格编辑'}><thead><tr><th>编号</th><th>标题</th><th>描述</th><th>类型</th><th>优先级</th><th>{isCase?'对应场景':'对应需求'}</th>{isCase&&<><th>前置条件</th><th>步骤与预期结果</th></>}{customColumns.map(column=><th key={column.field}><span>{column.header}</span>{canChangeColumns&&<button className="icon-button" aria-label={'删除列 '+column.header} title="删除此自定义列" onClick={()=>removeColumn(column.field)}><Trash2 size={14}/></button>}</th>)}<th>操作</th></tr></thead><tbody>{items.map((item,index)=><tr key={item.id}>
   <td className="table-edit-id"><strong>{item.id}</strong></td>
   <td><input aria-label={'条目 '+item.id+' 标题'} value={item.title??''} onChange={e=>update(index,{title:e.target.value})}/></td>
   <td><textarea aria-label={'条目 '+item.id+' 描述'} rows={3} value={item.description??''} onChange={e=>update(index,{description:e.target.value})}/></td>
   <td className="table-edit-short"><input aria-label={'条目 '+item.id+' 类型'} value={item.type??''} onChange={e=>update(index,{type:e.target.value})}/></td>
   <td className="table-edit-priority"><input aria-label={'条目 '+item.id+' 优先级'} value={item.priority??''} onChange={e=>update(index,{priority:e.target.value})}/></td>
   <td className="table-edit-parent"><ParentIds item={item} isCase={isCase} onChange={patch=>update(index,patch)}/><small className="muted">{isCase?'可清空关联':'多个编号用逗号分隔'}</small></td>
   {isCase&&<><td><textarea aria-label={'条目 '+item.id+' 前置条件'} rows={3} value={item.preconditions??''} onChange={e=>update(index,{preconditions:e.target.value})}/></td><td className="table-edit-steps"><table aria-label={item.id+' 步骤编辑'}><thead><tr><th>序号</th><th>操作 / Step</th><th>预期结果 / Expected result</th><th>操作</th></tr></thead><tbody>{(item.steps??[]).map((step:Json,stepIndex:number)=><tr key={stepIndex}><td>{stepIndex+1}</td><td><textarea aria-label={item.id+' 操作 '+(stepIndex+1)} rows={3} value={step.action??''} onChange={e=>update(index,{steps:item.steps.map((current:Json,i:number)=>i===stepIndex?{...current,action:e.target.value}:current)})}/></td><td><textarea aria-label={item.id+' 预期结果 '+(stepIndex+1)} rows={3} value={step.expected??''} onChange={e=>update(index,{steps:item.steps.map((current:Json,i:number)=>i===stepIndex?{...current,expected:e.target.value}:current)})}/></td><td><button className="icon-button" aria-label={'删除 '+item.id+' 步骤 '+(stepIndex+1)} disabled={item.steps.length<=1} onClick={()=>update(index,{steps:item.steps.filter((_:unknown,i:number)=>i!==stepIndex)})}><Trash2 size={14}/></button></td></tr>)}</tbody></table><button className="text-accent" aria-label={'为 '+item.id+' 添加步骤'} onClick={()=>update(index,{steps:[...(item.steps??[]),{action:'',expected:''}]})}><Plus size={14}/>添加步骤</button></td></>}
   {customColumns.map(column=><td key={column.field}><CustomValue value={item[column.field]} label={'条目 '+item.id+' '+column.header} onChange={value=>update(index,{[column.field]:value})}/></td>)}
   <td><button className="icon-button" aria-label={'删除条目 '+item.id} onClick={()=>onChange(items.filter((_,i)=>i!==index))}><Trash2 size={16}/></button></td>
  </tr>)}</tbody></table>{items.length===0&&<p className="muted">尚无条目，可点击“添加条目”开始填写。</p>}</div>
 </div>;
}

function ParentIds({item,isCase,onChange}:{item:Json;isCase:boolean;onChange:(patch:Json)=>void}){
 // Preserve partially typed separators while emitting canonical associations.
 const [text,setText]=useState(isCase?item.scenario_id??'':(item.requirement_ids??[]).join(', '));
 return <input aria-label={'条目 '+item.id+' '+(isCase?'对应场景':'对应需求')} placeholder="N/A" value={text} onChange={e=>{const raw=e.target.value;setText(raw);const value=/^(?:n\/?a)$/i.test(raw.trim())?'':raw.trim();onChange(isCase?{scenario_id:value}:{requirement_ids:value.split(/[,，;；\n]/).map(part=>part.trim()).filter(Boolean)});}}/>;
}

function CustomValue({value,label,onChange}:{value:unknown;label:string;onChange:(value:unknown)=>void}){
 if(value!==null&&typeof value==='object')return <div className="table-structured-cell"><TextValue value={value}/><small className="muted">结构化内容可在“JSON · 全部字段”中编辑。</small></div>;
 if(typeof value==='boolean')return <select aria-label={label} value={String(value)} onChange={e=>onChange(e.target.value==='true')}><option value="true">true</option><option value="false">false</option></select>;
 if(typeof value==='number')return <input aria-label={label} type="number" value={value} onChange={e=>onChange(e.target.value===''?'':Number(e.target.value))}/>;
 return <textarea aria-label={label} rows={3} value={value==null?'':String(value)} onChange={e=>onChange(e.target.value)}/>;
}
