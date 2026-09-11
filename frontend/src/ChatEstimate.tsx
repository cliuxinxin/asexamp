type EstimateScenario={scenario_id:string;title:string;min_count:number;max_count:number;rationale:string;assumptions:string[]};
type CaseEstimate={artifact_id:string;artifact_revision:number;title:string;scenarios:EstimateScenario[];min_count:number;max_count:number};

const range=(minimum:number,maximum:number)=>minimum===maximum?String(minimum):`${minimum}–${maximum}`;

export function ChatEstimate({estimate}:{estimate:CaseEstimate}){
 if(!Array.isArray(estimate.scenarios))return null;
 return <section className="chat-estimate" aria-label="场景用例数量估算">
  <div className="chat-estimate-heading"><strong>预计 {range(estimate.min_count,estimate.max_count)} 条用例</strong><span>未生成用例</span></div>
  <p className="chat-estimate-source">来源：{estimate.title} · v{estimate.artifact_revision}</p>
  <p className="chat-estimate-scope">本次范围：{estimate.scenarios.length} 个场景 · {estimate.scenarios.map(item=>item.scenario_id).join('、')}</p>
  <div className="chat-estimate-scroll"><table aria-label="用例数量估算"><thead><tr><th>场景</th><th>预计用例数</th><th>估算理由与假设</th></tr></thead><tbody>{estimate.scenarios.map(item=><tr key={item.scenario_id}><td><span className="chat-estimate-id">{item.scenario_id}</span><strong>{item.title}</strong></td><td className="chat-estimate-count">{range(item.min_count,item.max_count)}</td><td><p>{item.rationale}</p>{item.assumptions?.length>0&&<div className="chat-estimate-assumptions"><span>估算假设</span><ul>{item.assumptions.map((assumption,index)=><li key={index}>{assumption}</li>)}</ul></div>}</td></tr>)}</tbody></table></div>
 </section>;
}
