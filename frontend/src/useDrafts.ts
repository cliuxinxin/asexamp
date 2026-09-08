import {useRef,useState} from 'react';
import type {Artifact} from './types';
export type EditTarget={artifact:Artifact;ids:string[]};
type Draft={text:string;version:number;asRequirement?:boolean;target?:EditTarget};
export function useDrafts(key:string){
 const [drafts,setDrafts]=useState<Record<string,Draft>>({});
 const ref=useRef(drafts);ref.current=drafts;
 function update(fn:(old:Record<string,Draft>)=>Record<string,Draft>){setDrafts(old=>{const next=fn(old);ref.current=next;return next;});}
 function setContent(text:string){update(old=>({...old,[key]:{...old[key],text,version:(old[key]?.version??0)+1}}));}
 function setTarget(target:EditTarget|undefined){update(old=>({...old,[key]:{...old[key],text:old[key]?.text??'',target,version:(old[key]?.version??0)+1}}));}
 function setAsRequirement(asRequirement:boolean){update(old=>({...old,[key]:{...old[key],text:old[key]?.text??'',asRequirement,version:(old[key]?.version??0)+1}}));}
 function move(from:string,to:string){update(old=>{if(!old[from])return old;const next={...old,[to]:old[from]};delete next[from];return next;});}
 function clearSubmitted(sentKey:string,version:number){update(old=>old[sentKey]?.version===version?{...old,[sentKey]:{text:'',version:version+1}}:old);}
 return {content:drafts[key]?.text??'',target:drafts[key]?.target,asRequirement:drafts[key]?.asRequirement??false,setContent,setTarget,setAsRequirement,move,clearSubmitted,version:ref.current[key]?.version??0};
}
