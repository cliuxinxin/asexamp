import {memo,useState} from 'react';
import {diffColumns,diffText,equalConfig,isColumnList} from './profile-diff';

const fieldLabels:Record<string,string>={header:'列名',field:'字段',definition:'字段定义',value_source:'填写方式',required:'必填',default_value:'默认值',title:'标题',description:'描述',action:'步骤',expected:'预期结果',steps:'步骤与预期',refs:'引用',type:'类型',priority:'优先级',preconditions:'前置条件'};
const sourceLabels:Record<string,string>={ai:'AI 生成',manual:'人工填写',derived:'自动汇总',default:'固定默认值'};
type DiffProps={before:unknown;after:unknown;beforePresent?:boolean;afterPresent?:boolean;field?:string};

function ConfigValue({value,present=true,field}:{value:unknown;present?:boolean;field?:string}){
 if(!present)return <span className="muted">未设置</span>;
 if(value===null)return <span className="muted">空值（null）</span>;
 if(value==='')return <span className="muted">空字符串</span>;
 if(Array.isArray(value))return value.length?<ol className="profile-change-values">{value.map((item,index)=><li key={index}><ConfigValue value={item}/></li>)}</ol>:<span className="muted">空列表（0 项）</span>;
 if(value&&typeof value==='object'){
  const entries=Object.entries(value);
  return entries.length?<dl className="profile-change-fields">{entries.map(([key,entry])=><div key={key}><dt>{fieldLabels[key]??key}</dt><dd><ConfigValue value={entry} field={key}/></dd></div>)}</dl>:<span className="muted">空对象</span>;
 }
 return <span className={'preserve'+(field==='field'?' profile-field-key':'')}>{field==='value_source'&&typeof value==='string'?sourceLabels[value]??value:field==='required'&&typeof value==='boolean'?(value?'是':'否'):String(value)}</span>;
}

function ValueDiff({before,after,beforePresent=true,afterPresent=true,field}:DiffProps){
 const strings=beforePresent&&afterPresent&&typeof before==='string'&&typeof after==='string'&&before.length>0&&after.length>0&&field!=='value_source';
 const tokens=strings?diffText(before as string,after as string):[];
 const unchanged=beforePresent===afterPresent&&equalConfig(before,after);
 return <div className="profile-diff-pair">
  <div className="profile-diff-before"><small>更改前</small>{strings?<span className="preserve">{tokens.filter(part=>part.kind!=='add').map((part,index)=>part.kind==='remove'?<del key={index}>{part.text}</del>:<span key={index}>{part.text}</span>)}</span>:unchanged||!beforePresent?<ConfigValue value={before} present={beforePresent} field={field}/>:<del><ConfigValue value={before} field={field}/></del>}</div>
  <div className="profile-diff-after"><small>建议更改后</small>{strings?<span className="preserve">{tokens.filter(part=>part.kind!=='remove').map((part,index)=>part.kind==='add'?<ins key={index}>{part.text}</ins>:<span key={index}>{part.text}</span>)}</span>:unchanged||!afterPresent?<ConfigValue value={after} present={afterPresent} field={field}/>:<ins><ConfigValue value={after} field={field}/></ins>}</div>
 </div>;
}

function FullConfig({before,after,beforePresent=true,afterPresent=true}:DiffProps){
 const [open,setOpen]=useState(false);
 return <details className="profile-full-config" onToggle={event=>setOpen(event.currentTarget.open)}><summary>查看完整前后配置及列顺序</summary>{open&&<div className="profile-diff-pair"><div><small>更改前</small><ConfigValue value={before} present={beforePresent}/></div><div><small>建议更改后</small><ConfigValue value={after} present={afterPresent}/></div></div>}</details>;
}

export const ProfileConfigDiff=memo(function ProfileConfigDiff({before,after,beforePresent=true,afterPresent=true,field}:DiffProps){
 if((field==='excel_columns'||field==='scenario_excel_columns')&&(!beforePresent||isColumnList(before))&&(!afterPresent||isColumnList(after))){
  const {changes,unchanged}=diffColumns(beforePresent&&isColumnList(before)?before:[],afterPresent&&isColumnList(after)?after:[]);
  return <div className="profile-columns-diff">
   {changes.map(change=><section key={change.field} className={'profile-column-change '+change.kind} aria-label={'列更改 '+change.field}>
    <header><strong>{change.kind==='add'?'+ 新增列：':change.kind==='remove'?'- 删除列：':change.kind==='move'?'↕ 调整顺序：':'修改列：'}{String((change.after??change.before)?.header??change.field)}</strong><code>{change.field}</code></header>
    {(change.kind==='add'||change.kind==='remove')?<><p className="muted small-text">{change.kind==='add'?'新增后位于第 '+(change.afterIndex+1)+' 列':'原第 '+(change.beforeIndex+1)+' 列'}</p>{change.kind==='add'?<ins className="profile-column-values"><ConfigValue value={change.after}/></ins>:<del className="profile-column-values"><ConfigValue value={change.before}/></del>}</>:<>
     {change.moved&&<p className="profile-column-order"><span>顺序：</span><del>第 {change.beforeIndex+1} 列</del><span aria-hidden="true"> → </span><ins>第 {change.afterIndex+1} 列</ins></p>}
     {change.changedKeys.map(key=><div className="profile-property-diff" key={key}><strong>{fieldLabels[key]??key}</strong><ValueDiff before={change.before?.[key]} after={change.after?.[key]} beforePresent={Object.hasOwn(change.before??{},key)} afterPresent={Object.hasOwn(change.after??{},key)} field={key}/></div>)}
    </>}
   </section>)}
   {unchanged>0&&<p className="muted small-text">其余 {unchanged} 列配置未变。</p>}
   {!changes.length&&<ValueDiff before={before} after={after} beforePresent={beforePresent} afterPresent={afterPresent}/>}
   <FullConfig before={before} after={after} beforePresent={beforePresent} afterPresent={afterPresent}/>
  </div>;
 }
 return <ValueDiff before={before} after={after} beforePresent={beforePresent} afterPresent={afterPresent} field={field}/>;
});
