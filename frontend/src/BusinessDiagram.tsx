import {useEffect,useId,useState} from 'react';
import {Download,Maximize2,ZoomIn,ZoomOut} from 'lucide-react';
import {Dialog,Spinner} from './ui';

let rendering:Promise<unknown>=Promise.resolve();
let renderer:Promise<typeof import('mermaid')>|undefined;

export function checkDiagram(source:string){
 const clean=source.trim().replace(/^```mermaid\s*\n/i,'').replace(/\n```$/,'');
 if(clean.length>30000)throw new Error('图表过大，请按业务模块拆分后再展示。');
 if(!/^(flowchart|graph|stateDiagram-v2|mindmap)\b/.test(clean))throw new Error('当前支持流程图、状态图和 mindmap 思维导图，请调整 Mermaid 图表类型。');
 if(/%%\s*\{|^\s*(click|style|classDef|linkStyle)\b|<\/?[a-z][^>]*>|javascript\s*:|@\{/im.test(clean))throw new Error('图表包含不支持的交互或配置，请让 AI 仅保留业务节点和连线。');
 return clean;
}

async function renderDiagram(source:string,id:string){
 // Mermaid has shared mutable configuration; serialize renders across cards.
 const task=rendering.then(async()=>{
  renderer??=import('mermaid');
  const [{default:mermaid},{default:purify}]=await Promise.all([renderer,import('dompurify')]);
  mermaid.initialize({startOnLoad:false,securityLevel:'strict',suppressErrorRendering:true,theme:'base',
   themeVariables:{primaryColor:'#edf7f3',primaryTextColor:'#25443d',primaryBorderColor:'#78a99b',lineColor:'#78928a',fontFamily:'system-ui, sans-serif',fontSize:'14px'},
   htmlLabels:false,flowchart:{htmlLabels:false,useMaxWidth:true},maxTextSize:30000});
  await mermaid.parse(source);
  const {svg}=await mermaid.render(id,source);
  const safe=purify.sanitize(svg,{USE_PROFILES:{svg:true,svgFilters:true},FORBID_TAGS:['foreignObject','script','a','image']});
  const viewBox=new DOMParser().parseFromString(safe,'image/svg+xml').documentElement.getAttribute('viewBox')?.trim().split(/[\s,]+/).map(Number);
  const width=viewBox&&Number.isFinite(viewBox[2])&&viewBox[2]>0?viewBox[2]:600;
  return {svg:safe,width};
 });
 rendering=task.catch(()=>{});
 return task;
}

function saveFile(name:string,content:string,type:string){
 const url=URL.createObjectURL(new Blob([content],{type}));
 const link=document.createElement('a');link.href=url;link.download=name;link.click();
 setTimeout(()=>URL.revokeObjectURL(url),1000);
}

export function BusinessDiagram({title,source}:{title:string;source:string}){
 const id='business-'+useId().replace(/[^a-z0-9]/gi,'');
 const [result,setResult]=useState<{source:string;svg:string;width:number}>();
 const [failure,setFailure]=useState<{source:string;message:string}>();
 const [expanded,setExpanded]=useState(false);const [zoom,setZoom]=useState(1);
 useEffect(()=>{
  let active=true;setZoom(1);
  async function render(){
   try{const clean=checkDiagram(source);const rendered=await renderDiagram(clean,id);if(active){setResult({source,...rendered});setFailure(undefined);}}
   catch(e){if(active)setFailure({source,message:e instanceof Error&&e.message.startsWith('图表')||e instanceof Error&&e.message.startsWith('当前支持')?(e as Error).message:'图表暂时无法绘制。分析结果已保留，可以查看源码并让 AI 修正这张图。'});}
  }
  void render();return()=>{active=false;};
 },[source,id]);
 const svg=result?.source===source?result.svg:'';
 const error=failure?.source===source?failure.message:'';
 const drawing=<div className="diagram-viewport"><div className="diagram-svg" style={{width:result?.source===source?`${result.width*zoom}px`:'100%',maxWidth:zoom<=1?'100%':undefined}} dangerouslySetInnerHTML={{__html:svg}}/></div>;
 return <section className="business-diagram" aria-label={title}>
  <div className="diagram-header"><strong>{title}</strong><div className="actions">
   <button disabled={!svg} aria-label={'放大 '+title} onClick={()=>setZoom(value=>Math.min(3,value+.25))}><ZoomIn size={15}/></button>
   <button disabled={!svg} aria-label={'缩小 '+title} onClick={()=>setZoom(value=>Math.max(.5,value-.25))}><ZoomOut size={15}/></button>
   <button disabled={!svg} onClick={()=>setExpanded(true)}><Maximize2 size={14}/>全屏</button>
   <button disabled={!svg} onClick={()=>saveFile('business-diagram.svg',svg,'image/svg+xml')}><Download size={14}/>SVG</button>
  </div></div>
  {error?<p role="alert" className="diagram-error">{error}</p>:svg?(expanded?<p className="muted">业务图已在放大视图中打开。</p>:drawing):<p className="diagram-loading"><Spinner/>正在绘制 Mermaid 图表…</p>}
  <details className="diagram-source"><summary>Mermaid 源码</summary><pre>{source}</pre><button onClick={()=>saveFile('business-diagram.mmd',source,'text/plain')}>下载源码</button></details>
  {expanded&&<Dialog title={title} onClose={()=>setExpanded(false)} wide>{drawing}</Dialog>}
 </section>;
}
