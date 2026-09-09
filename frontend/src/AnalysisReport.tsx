import {BusinessDiagram} from './BusinessDiagram';
import {CoveragePanel} from './AgentPanel';
import {EvidenceRefs} from './EvidenceRefs';
import {depthNames} from './types';
import type {Depth,Json} from './types';

function describe(value:unknown):string{
 if(typeof value==='string')return value;
 if(value&&typeof value==='object'){const item=value as Json;return item.summary??item.title??item.description??item.question??JSON.stringify(value);}
 return String(value??'');
}

export function AnalysisReport({report}:{report:Json}){
 const diagrams=Array.isArray(report.diagrams)?report.diagrams.filter((d:Json)=>d&&typeof d.mermaid==='string'):[];
 const strategy=report.strategy;
 const traceability:Array<Json>=Array.isArray(report.traceability)?report.traceability:[];
 const groups=[['questions','需要确认'],['assumptions','尚未确认的假设'],['issues','检查发现'],['limitations','范围限制'],['coverage','覆盖说明']] as const;
 return <div className="analysis-report">
  {report.summary&&<p className="report-summary preserve">{describe(report.summary)}</p>}
  {strategy&&<div className="strategy-overview"><span className="depth-badge">{depthNames[strategy.depth as Depth]??'测试方案'}</span>{strategy.rationale&&<p>{strategy.rationale}</p>}{Array.isArray(strategy.scope)&&strategy.scope.length>0&&<p><strong>测试范围：</strong>{strategy.scope.map(describe).join('、')}</p>}{Array.isArray(strategy.techniques)&&strategy.techniques.length>0&&<div className="technique-tags">{strategy.techniques.map((method:unknown,index:number)=><span key={index}>{describe(method)}</span>)}</div>}</div>}
  {diagrams.map((diagram:Json,index:number)=><BusinessDiagram key={diagram.id??index} title={diagram.title??'业务流程图'} source={diagram.mermaid}/>)}
  {groups.map(([key,label])=>Array.isArray(report[key])&&report[key].length>0?<div className={'report-findings '+key} key={key}><strong>{label}</strong><ul>{report[key].map((item:unknown,index:number)=><li key={index}>{describe(item)}{item!=null&&typeof item==='object'&&Array.isArray((item as Json).refs)&&<EvidenceRefs refs={(item as Json).refs}/>}</li>)}</ul></div>:null)}
  {report.coverage&&typeof report.coverage==='object'&&!Array.isArray(report.coverage)&&'requirements_total' in report.coverage&&<CoveragePanel coverage={report.coverage}/>}
  {report.business_model?.edges?.length>0&&<details className="branch-evidence"><summary>核查业务分支与依据 · {report.business_model.edges.length} 条</summary><ol>{report.business_model.edges.map((edge:Json)=><li key={edge.id}><span className="branch-id">{edge.id}</span><span>{edge.label||`${edge.from} → ${edge.to}`}</span>{edge.refs&&<EvidenceRefs refs={edge.refs}/>}<small className={traceability.some(item=>item.branch_ids?.includes(edge.id))?'branch-covered':'branch-gap'}>{(()=>{const cases=traceability.filter(item=>item.case_id&&item.branch_ids?.includes(edge.id)).map(item=>item.case_id);const scenarios=traceability.filter(item=>item.branch_ids?.includes(edge.id)).map(item=>item.scenario_id);return cases.length?`关联用例：${[...new Set(cases)].join('、')}`:scenarios.length?`关联场景：${[...new Set(scenarios)].join('、')}`:'尚无关联用例';})()}</small></li>)}</ol></details>}
  {traceability.length>0&&<details className="traceability-report"><summary>需求 → 分支 → 场景 → 用例关联</summary><div className="table-scroll"><table><thead><tr><th>需求</th><th>业务分支</th><th>场景</th><th>用例</th></tr></thead><tbody>{traceability.map((item,index)=><tr key={item.id??index}><td>{item.requirement_ids?.join('、')}</td><td>{item.branch_ids?.join('、')}</td><td>{item.scenario_id}</td><td>{item.case_id??'尚未生成'}</td></tr>)}</tbody></table></div></details>}
  {Array.isArray(report.review_reports)&&report.review_reports.map((review:Json,index:number)=><div className="report-findings" key={'review-'+index}><strong>AI 审核结论</strong><p>{describe(review)}</p></div>)}
  {Array.isArray(report.segments)&&report.segments.map((segment:Json,index:number)=><div key={index}><AnalysisReport report={segment}/></div>)}
  {Array.isArray(report.unresolved_rows)&&report.unresolved_rows.length>0&&<div className="report-findings"><strong>待修复条目（未计入有效用例）</strong>{report.unresolved_rows.map((row:Json,index:number)=><details key={index}><summary>{row.item?.title??`条目 ${index+1}`} · {row.error}</summary><pre>{JSON.stringify(row.item,null,2)}</pre></details>)}</div>}
  <details className="report raw-report"><summary>完整分析记录</summary><pre>{JSON.stringify(report,null,2)}</pre></details>
 </div>;
}
