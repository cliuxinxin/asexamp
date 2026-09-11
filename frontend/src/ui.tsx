import {useEffect,useRef} from 'react';
import type {ReactNode} from 'react';
import {X,Code2,LoaderCircle} from 'lucide-react';
export function Logo({small=false}:{small?:boolean}){return <span className={'logo '+(small?'small':'')}><Code2 aria-hidden="true" size={small?19:25}/></span>;}
export function Spinner(){return <LoaderCircle size={16} className="spin" aria-label="处理中"/>;}
export function Dialog({title,onClose,children,wide=false,closeDisabled=false}:{title:string;onClose:()=>void;children:ReactNode;wide?:boolean;closeDisabled?:boolean}){
 const ref=useRef<HTMLDialogElement>(null);
 useEffect(()=>{ref.current?.showModal();return()=>ref.current?.close();},[]);
 return <dialog ref={ref} className={wide?'dialog wide':'dialog'} onCancel={e=>{e.preventDefault();if(!closeDisabled)onClose();}} onClick={e=>{if(e.target===ref.current&&!closeDisabled)onClose();}} aria-label={title}><div className="dialog-head"><h2>{title}</h2><button className="icon-button" aria-label="关闭" disabled={closeDisabled} onClick={onClose}><X size={20}/></button></div><div className="dialog-body">{children}</div></dialog>;
}
export function ErrorBox({message}:{message:string}){return message?<p role="alert" className="error-box">{message}</p>:null;}
export function TextValue({value}:{value:unknown}){return <span className="preserve">{typeof value==='string'?value:value==null?'—':JSON.stringify(value,null,2)}</span>;}
