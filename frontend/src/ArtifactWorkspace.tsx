import {useEffect,useRef,useState} from 'react';
import {X} from 'lucide-react';
import {Dialog} from './ui';
import {ArtifactCard} from './ArtifactCard';
import type {Artifact} from './types';

export function ArtifactWorkspace({artifact,refreshKey,onClose,onTarget,onChanged}:{artifact:Artifact;refreshKey:string;onClose:()=>void;onTarget:(artifact:Artifact,ids:string[])=>void;onChanged:()=>void}){
 const [mobile,setMobile]=useState(()=>window.matchMedia?.('(max-width: 1050px)').matches??false);
 useEffect(()=>{const query=window.matchMedia?.('(max-width: 1050px)');if(!query)return;const update=()=>setMobile(query.matches);query.addEventListener('change',update);return()=>query.removeEventListener('change',update);},[]);
 const heading=useRef<HTMLHeadingElement>(null);
 useEffect(()=>{const previous=document.activeElement as HTMLElement|null;heading.current?.focus();return()=>{if(previous?.isConnected)previous.focus();};},[artifact.id]);
 if(mobile)return <Dialog title={artifact.title} onClose={onClose} wide><div className="workspace-content mobile-workspace"><ArtifactCard key={artifact.id} id={artifact.id} refreshKey={refreshKey} onTarget={(item,ids)=>{onClose();onTarget(item,ids);}} onChanged={onChanged}/></div></Dialog>;
 return <aside className="artifact-workspace" aria-label="成果工作区" onKeyDown={event=>{if(event.key==='Escape'&&!event.defaultPrevented)onClose();}}><header><div><p>成果工作区</p><h2 ref={heading} tabIndex={-1}>{artifact.title}</h2></div><button aria-label="关闭成果工作区" onClick={onClose}><X size={20}/></button></header><div className="workspace-content"><ArtifactCard key={artifact.id} id={artifact.id} refreshKey={refreshKey} onTarget={onTarget} onChanged={onChanged}/></div></aside>;
}
