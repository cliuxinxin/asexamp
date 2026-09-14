import {useMemo} from 'react';
import type {SourceProvenance} from './types';

const sourceLabels:Record<string,string>={current_document:'当前文档',project_knowledge:'项目历史记忆',chat_supplement:'对话补充',format_sample:'格式样例',knowledge_document:'知识文档',unknown:'来源待查看'};

export function SourceBadges({refs,sources=[]}:{refs:string[];sources?:SourceProvenance[]}){
 const byRef=useMemo(()=>{const map=new Map<string,SourceProvenance>();for(const source of sources)for(const ref of source.refs??[])map.set(ref,source);return map;},[sources]);
 const groups=new Map<string,{label:string;details:Set<string>}>();
 for(const ref of refs.filter(value=>typeof value==='string')){
  const source=byRef.get(ref),kind=source?.classification&&sourceLabels[source.classification]?source.classification:'unknown';
  if(!groups.has(kind))groups.set(kind,{label:sourceLabels[kind],details:new Set()});
  const origin=source?.origin_chat_title?` · 来自《${source.origin_chat_title}》`:'';
  const version=source?.source_version!==undefined?` · 来源 v${source.source_version}`:'';
  const date=source?.origin_created_at?` · ${source.origin_created_at.slice(0,10)}`:'';
  groups.get(kind)!.details.add(`${source?.name??ref}${version}${origin}${date}`);
 }
 if(!groups.size)return null;
 return <span className="source-badges" aria-label="依据来源类型">{[...groups].map(([kind,value])=><span key={kind} className={'source-badge source-badge-'+kind} title={[...value.details].join('\n')}>{value.label}</span>)}</span>;
}
