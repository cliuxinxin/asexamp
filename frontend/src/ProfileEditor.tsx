import type {Json} from './types';
export function ProfileEditor({value,onChange}:{value:string;onChange:(value:string)=>void}){
 let config:Json;try{config=JSON.parse(value);}catch{return <p className="error-text">配置 JSON 尚未完成，请在高级编辑器中修正。</p>;}
 const set=(key:string,v:unknown)=>onChange(JSON.stringify({...config,[key]:v},null,2));
 const columns:Array<{field:string;header:string;definition?:string}>=Array.isArray(config.excel_columns)?config.excel_columns:[];
 const update=(index:number,key:string,v:string)=>set('excel_columns',columns.map((c,i)=>i===index?{...c,[key]:v}:c));
 const move=(index:number,offset:number)=>{const next=[...columns];[next[index],next[index+offset]]=[next[index+offset],next[index]];set('excel_columns',next);};
 return <div className="profile-visual-editor">
 <div className="field-row"><label>输出语言<input value={config.language??''} onChange={e=>set('language',e.target.value)} placeholder="跟随需求语言"/></label><label>默认范围<input value={config.scope??''} onChange={e=>set('scope',e.target.value)}/></label></div>
 <div className="field-row">{[['scenario_level','场景颗粒度'],['case_level','用例深度']].map(([key,label])=><label key={key}>{label}<select value={config[key]??'standard'} onChange={e=>set(key,e.target.value)}><option value="quick">Quick · 核心路径</option><option value="standard">Standard · 主要规则与边界</option><option value="deep">Deep · 状态、权限与组合</option></select></label>)}</div>
 <fieldset><legend>用例类型</legend><div className="field-row">{['Business','Negative','Boundary','Security'].map(t=><label className="check-label" key={t}><input type="checkbox" checked={(config.case_types??[]).includes(t)} onChange={e=>set('case_types',e.target.checked?[...(config.case_types??[]),t]:(config.case_types??[]).filter((x:string)=>x!==t))}/>{t}</label>)}</div></fieldset>
 <label>业务规则与关注点<textarea rows={3} value={config.additional_rules??''} onChange={e=>set('additional_rules',e.target.value)}/></label>
 <label>标题、步骤与预期的写作规范<textarea rows={3} value={config.template_rules??''} onChange={e=>set('template_rules',e.target.value)}/></label>
 <h3>Excel 导出格式</h3><p className="muted small-text">列映射控制实际导出列及顺序。步骤和预期分别映射到 steps / expected。</p>
 <div className="field-row"><label>布局<select value={config.excel_layout??'case'} onChange={e=>set('excel_layout',e.target.value)}><option value="case">一条用例一行</option><option value="step">一个步骤一行</option></select></label><label>Sheet 名称<input value={config.sheet_name??'Test Cases'} onChange={e=>set('sheet_name',e.target.value)}/></label></div>
 <label>文件名格式<input value={config.filename_pattern??'{project}_{date}.xlsx'} onChange={e=>set('filename_pattern',e.target.value)}/></label>
 <div className="mapping-table"><div className="mapping-row mapping-head"><span>Case 字段</span><span>Excel 列标题</span><span>顺序</span></div>{columns.map((column,index)=><div key={index} className="mapping-row"><input aria-label={`第 ${index+1} 列字段`} value={column.field} onChange={e=>update(index,'field',e.target.value)}/><div><input aria-label={`第 ${index+1} 列标题`} value={column.header} onChange={e=>update(index,'header',e.target.value)}/><input aria-label={`第 ${index+1} 列定义`} placeholder="字段定义，例如：字段=值" value={column.definition??''} onChange={e=>update(index,'definition',e.target.value)}/></div><div><button aria-label={`上移第 ${index+1} 列`} disabled={index===0} onClick={()=>move(index,-1)}>↑</button><button aria-label={`下移第 ${index+1} 列`} disabled={index===columns.length-1} onClick={()=>move(index,1)}>↓</button><button aria-label={`删除第 ${index+1} 列`} onClick={()=>set('excel_columns',columns.filter((_,i)=>i!==index))}>×</button></div></div>)}</div>
 <button onClick={()=>set('excel_columns',[...columns,{field:'test_data',header:'测试数据'}])}>＋ 添加导出列</button>
 </div>;
}
