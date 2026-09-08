import {useState} from 'react';
import {FileText} from 'lucide-react';
import {api,errText} from './api';
import {Dialog,ErrorBox,Spinner,TextValue} from './ui';
import type {Source} from './types';

export function EvidenceRefs({refs}:{refs:string[]}){
 const [open,setOpen]=useState(false);
 const [sources,setSources]=useState<Source[]>([]);
 const [loading,setLoading]=useState(false);
 const [error,setError]=useState('');
 const valid=refs.filter(ref=>typeof ref==='string');
 async function inspect(){
  setOpen(true);setLoading(true);setError('');
  try{setSources(await Promise.all([...new Set(valid.map(ref=>ref.split('#')[0]))].map(id=>api<Source>('/sources/'+encodeURIComponent(id)))));}
  catch(e){setError(errText(e));}finally{setLoading(false);}
 }
 if(!valid.length)return null;
 return <><button className="evidence-link" onClick={inspect}><FileText size={13}/>查看依据 · {valid.length}</button>{open&&<Dialog title="分析依据" onClose={()=>setOpen(false)} wide><ErrorBox message={error}/>{loading?<Spinner/>:valid.map(ref=>{
  const source=sources.find(item=>item.id===ref.split('#')[0]);
  const chunk=source?.chunks?.find(item=>item.id===ref);
  return <section className="evidence" key={ref}><strong>{source?.name??'来源'}</strong><small>{ref}{chunk?.location&&<> · <TextValue value={chunk.location}/></>}</small><p>{chunk?.text??'未找到此段原文，请检查来源是否仍可读取。'}</p></section>;
 })}</Dialog>}</>;
}
