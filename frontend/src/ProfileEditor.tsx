import type {Json} from './types';

const defaultScenarioColumns:Json[]=[
 {field:'id',header:'Scenario ID'},
 {field:'title',header:'Title'},
 {field:'description',header:'Description'},
 {field:'priority',header:'Priority'},
 {field:'requirement_ids',header:'Requirement IDs'},
 {field:'refs',header:'Evidence Refs'},
];

function ExcelColumns({columns,onChange,scenario=false}:{columns:Json[];onChange:(columns:Json[])=>void;scenario?:boolean}){
 const prefix=scenario?'场景第':'第';
 const update=(index:number,key:string,value:unknown)=>onChange(columns.map((column,i)=>i===index?{...column,[key]:value}:column));
 const move=(index:number,offset:number)=>{const next=[...columns];[next[index],next[index+offset]]=[next[index+offset],next[index]];onChange(next);};
 return <><div className="mapping-table"><div className="mapping-row mapping-head"><span>{scenario?'Scenario':'Case'} 字段</span><span>Excel 列标题</span><span>顺序</span></div>{columns.map((column,index)=><div key={index} className="mapping-row">
  <input aria-label={`${prefix} ${index+1} 列字段`} value={column.field??''} onChange={e=>update(index,'field',e.target.value)}/>
  <div><input aria-label={`${prefix} ${index+1} 列标题`} value={column.header??''} onChange={e=>update(index,'header',e.target.value)}/><input aria-label={`${prefix} ${index+1} 列定义`} placeholder="字段定义，例如：字段=值" value={column.definition??''} onChange={e=>update(index,'definition',e.target.value)}/>
   {!scenario&&<><label>填写方式<select aria-label={`${prefix} ${index+1} 列填写方式`} value={column.value_source??''} onChange={e=>onChange(columns.map((c,i)=>i===index?{...c,value_source:e.target.value||undefined,...(e.target.value==='default'?{default_value:c.default_value??''}:{})}:c))}><option value="">自动识别</option><option value="ai">AI 根据需求生成</option><option value="manual">人工填写</option><option value="default">固定默认值</option>{['steps','expected'].includes(column.field)&&<option value="derived">从步骤自动汇总</option>}</select></label>
   {column.value_source==='default'?<label>默认值<input aria-label={`${prefix} ${index+1} 列默认值`} value={String(column.default_value??'')} onChange={e=>update(index,'default_value',e.target.value)}/></label>:(!column.value_source||column.value_source==='ai')&&<label className="check-label"><input type="checkbox" aria-label={`${prefix} ${index+1} 列必填`} checked={column.required??!['preconditions','scenario_id','steps','expected'].includes(column.field)} onChange={e=>update(index,'required',e.target.checked)}/>AI 字段导出前必填</label>}</>}
  </div><div><button aria-label={`上移${prefix} ${index+1} 列`} disabled={index===0} onClick={()=>move(index,-1)}>↑</button><button aria-label={`下移${prefix} ${index+1} 列`} disabled={index===columns.length-1} onClick={()=>move(index,1)}>↓</button><button aria-label={`删除${prefix} ${index+1} 列`} onClick={()=>onChange(columns.filter((_,i)=>i!==index))}>×</button></div>
 </div>)}</div><button onClick={()=>onChange([...columns,{field:'test_data',header:'测试数据'}])}>＋ 添加{scenario?'场景':''}导出列</button></>;
}

export function ProfileEditor({value,onChange}:{value:string;onChange:(value:string)=>void}){
 let config:Json;try{config=JSON.parse(value);}catch{return <p className="error-text">配置 JSON 尚未完成，请在高级编辑器中修正。</p>;}
 const set=(key:string,v:unknown)=>onChange(JSON.stringify({...config,[key]:v},null,2));
 const columns:Json[]=Array.isArray(config.excel_columns)?config.excel_columns:[];
 const scenarioColumns:Json[]=Array.isArray(config.scenario_excel_columns)?config.scenario_excel_columns:defaultScenarioColumns;
 return <div className="profile-visual-editor">
 <div className="field-row"><label>输出语言<input value={config.language??''} onChange={e=>set('language',e.target.value)} placeholder="跟随需求语言"/></label><label>默认范围<input value={config.scope??''} onChange={e=>set('scope',e.target.value)}/></label></div>
 <div className="field-row">{[['scenario_level','场景颗粒度'],['case_level','用例深度']].map(([key,label])=><label key={key}>{label}<select value={config[key]??'standard'} onChange={e=>set(key,e.target.value)}><option value="quick">Quick · 核心路径</option><option value="standard">Standard · 主要规则与边界</option><option value="deep">Deep · 状态、权限与组合</option></select></label>)}</div>
 <fieldset><legend>用例类型</legend><div className="field-row">{['Business','Negative','Boundary','Security'].map(t=><label className="check-label" key={t}><input type="checkbox" checked={(config.case_types??[]).includes(t)} onChange={e=>set('case_types',e.target.checked?[...(config.case_types??[]),t]:(config.case_types??[]).filter((x:string)=>x!==t))}/>{t}</label>)}</div></fieldset>
 <label>业务规则与关注点<textarea rows={3} value={config.additional_rules??''} onChange={e=>set('additional_rules',e.target.value)}/></label>
 <label>标题、步骤与预期的写作规范<textarea rows={3} value={config.template_rules??''} onChange={e=>set('template_rules',e.target.value)}/></label>
 <h3>用例 Excel 模板</h3><p className="muted small-text">列映射控制实际导出列及顺序。每列可设置定义和填写方式。AI 必填字段缺失时会集中补全；人工字段允许留空。步骤和预期分别映射到 steps / expected。</p>
 <div className="field-row"><label>布局<select value={config.excel_layout??'case'} onChange={e=>set('excel_layout',e.target.value)}><option value="case">一条用例一行</option><option value="step">一个步骤一行</option></select></label><label>Sheet 名称<input value={config.sheet_name??'Test Cases'} onChange={e=>set('sheet_name',e.target.value)}/></label></div>
 <label>文件名格式<input value={config.filename_pattern??'{project}_{date}.xlsx'} onChange={e=>set('filename_pattern',e.target.value)}/></label>
 <ExcelColumns columns={columns} onChange={next=>set('excel_columns',next)}/>
 <h3>场景 Excel 模板</h3><p className="muted small-text">一条场景一行，可单独设置工作表、文件名和列顺序。requirement_ids 和 refs 可用于需求追溯，导出当前已有内容。</p>
 <label>场景 Sheet 名称<input value={config.scenario_sheet_name??'Test Scenarios'} onChange={e=>set('scenario_sheet_name',e.target.value)}/></label>
 <label>场景文件名格式<input value={config.scenario_filename_pattern??'{project}_scenarios_{date}.xlsx'} onChange={e=>set('scenario_filename_pattern',e.target.value)}/></label>
 <ExcelColumns scenario columns={scenarioColumns} onChange={next=>set('scenario_excel_columns',next)}/>
 </div>;
}
