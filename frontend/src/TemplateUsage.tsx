import type {Json} from './types';
export function TemplateUsage({usage}:{usage:Json}){
 return <details className="template-usage"><summary>本轮模板与字段定义 · {usage.profile_name}</summary><p>{usage.note}</p><p>工作表：{usage.sheet_name} · {usage.layout==='step'?'每步一行':'每条用例一行'}</p>{usage.rules&&<p className="preserve">写作规范：{usage.rules}</p>}<p>参考模板：{usage.references?.length?usage.references.map((r:Json)=>r.name).join('、'):'未附加；使用 Profile 定义'}</p><div className="table-scroll"><table><thead><tr><th>列名</th><th>字段</th><th>定义</th></tr></thead><tbody>{usage.columns?.map((c:Json,i:number)=><tr key={i}><td>{c.header}</td><td>{c.field}</td><td>{c.definition||'沿用字段默认含义'}</td></tr>)}</tbody></table></div></details>;
}
