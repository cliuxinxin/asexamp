import {FileText,Upload,CheckCircle2,Table2,Sparkles} from 'lucide-react';
import {ArtifactCard} from './ArtifactCard';
import type {Artifact} from './types';

export function ArtifactWorkspace({artifact,refreshKey,onTarget,onChanged,onUpload,onGenerate,sourceCount,running}:{artifact?:Artifact;refreshKey:string;onTarget:(artifact:Artifact,ids:string[])=>void;onChanged:()=>void;onUpload:()=>void;onGenerate:()=>void;sourceCount:number;running:boolean}){
 return <section className="results-workspace" aria-label="用例工作区">
  <header className="results-header"><div><p className="eyebrow">GENERATION / TEST CASES</p><h1>测试用例工作台</h1><p className="muted">从需求到用例，在同一个工作区持续完善。</p></div><span className="version-pill">TCG 2.3</span></header>
  <div className="workspace-tabs"><span className="active"><Table2 size={16}/>用例工作区</span><span><FileText size={15}/>{sourceCount} 份需求资料</span><span className="workspace-status">{running?'正在处理':artifact?'结果已保存':'准备就绪'}</span></div>
  <div className="results-body">{artifact?<ArtifactCard key={artifact.id} id={artifact.id} refreshKey={refreshKey} onTarget={onTarget} onChanged={onChanged}/>:<div className="intake-workspace">
    <div className="intake-heading"><span className="intake-icon"><Sparkles size={26}/></span><p className="eyebrow">AI TEST DESIGN</p><h2>把需求变成可执行的测试用例</h2><p>上传文档或在右侧粘贴需求。生成后直接编辑表格，<br/>也可以让 AI 为选中的用例补充边界、调整步骤。</p></div>
    <button className="upload-zone" disabled={running} onClick={onUpload}><Upload size={28}/><strong>{sourceCount?`已添加 ${sourceCount} 份资料，继续添加`:'选择需求文档'}</strong><span>DOCX · PDF · Markdown · TXT · Excel</span><small>原文与来源引用会一并保留</small></button>
    <div className="intake-bottom"><span><CheckCircle2 size={16}/>整份理解 · 按需分组</span><button className="primary" disabled={running||!sourceCount} onClick={onGenerate}><Sparkles size={16}/>生成测试用例</button></div>
    <div className="workflow-cards"><div><b>01</b><strong>添加需求</strong><p>文档、表格或直接粘贴</p></div><div><b>02</b><strong>生成用例</strong><p>关键歧义集中向你确认</p></div><div><b>03</b><strong>对话完善</strong><p>选中、修改、导出 Excel</p></div></div>
  </div>}</div>
 </section>;
}
