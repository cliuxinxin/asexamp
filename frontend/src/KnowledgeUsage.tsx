import {BookOpen} from 'lucide-react';
import type {KnowledgeFact} from './types';

export function knowledgeOrigin(item:{origin_chat_title?:string;origin_created_at?:string;created_at?:string}){
 const date=(item.origin_created_at??item.created_at??'').slice(0,10);
 return `${date?date+' · ':''}${item.origin_chat_title?`来自对话《${item.origin_chat_title}》`:'来源对话未记录'}`;
}

export function KnowledgeUsage({facts,onOpenKnowledge,summaryShown=false}:{facts:KnowledgeFact[];onOpenKnowledge?:()=>void;summaryShown?:boolean}){
 if(!facts.length)return null;
 return <section className="knowledge-usage" aria-label="本次项目知识引用">
  {!summaryShown&&<p className="knowledge-usage-title"><BookOpen size={15}/><span>本次模型上下文引入了 {facts.length} 条项目历史规则。</span></p>}
  <details><summary>查看本次引入的规则</summary><ul>{facts.map((fact,index)=><li key={(fact.source_id??fact.id??'fact')+':'+index}><strong>{fact.name??'项目规则'}</strong>{fact.text&&<p className="preserve">{fact.text}</p>}<small className="muted">{knowledgeOrigin(fact)}{fact.source_version!==undefined?` · 来源 v${fact.source_version}`:''}</small></li>)}</ul><p className="muted small-text">此清单记录本次送入模型的历史规则；具体条目是否采用，以成果中的依据为准。</p></details>
  {onOpenKnowledge&&<button className="text-accent" onClick={onOpenKnowledge}>查看 / 管理项目知识库</button>}
 </section>;
}
