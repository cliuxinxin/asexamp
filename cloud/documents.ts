import {unzipSync,strFromU8} from 'fflate';
import {XMLParser} from 'fast-xml-parser';
import * as XLSX from 'xlsx';
import {DomainError,type Json} from './domain';
export const MAX_UPLOAD=15*1024*1024;
export function splitParts(parts:[string,string][]):{text:string;chunks:Json[]}{
 const chunks:Json[]=[];
 for(const [location,raw] of parts){const text=raw.trim();if(!text)continue;for(let p=0;p<text.length;p+=2500)chunks.push({text:text.slice(p,p+2500),location:location+(p?` · 字符 ${p+1}`:'')});}
 if(!chunks.length)throw new DomainError('文件未提取到可用文本；扫描 PDF 请先转换为文本 PDF');
 const text=chunks.map(c=>c.text).join('\n\n');if(text.length>2_000_000)throw new DomainError('解析文本超过 200 万字符，请拆分文档；内容未截断');return {text,chunks};
}
export function parseText(text:string){if(typeof text!=='string'||!text.trim())throw new DomainError('来源文本不能为空');return splitParts(text.split(/\n\s*\n/).map((s,i)=>[`段落 ${i+1}`,s]));}
function xmlText(nodes:any[]):string{return (nodes??[]).map(n=>typeof n['#text']==='string'?n['#text']:Object.entries(n).filter(([key])=>key!==':@').map(([key,value])=>key==='tab'?'\t':key==='br'?'\n':Array.isArray(value)?xmlText(value):'').join('')).join('');}
export async function parseDocument(name:string,data:Uint8Array){
 const suffix=name.toLowerCase().split('.').at(-1);if(!['txt','md','csv','docx','pdf','xlsx','xls'].includes(suffix??''))throw new DomainError('请上传 DOCX、文本 PDF、XLSX、XLS、CSV、TXT 或 MD');if(data.byteLength>MAX_UPLOAD)throw new DomainError('文件超过 15 MB 限制',413);
 try{
  let office:Record<string,Uint8Array>={};if(['docx','xlsx'].includes(suffix!)){let size=0;office=unzipSync(data,{filter:file=>{size+=file.originalSize;if(size>32*1024*1024)throw new DomainError('压缩文档展开后超过云端 32 MB 处理预算，请拆分文件');return suffix==='docx'&&file.name==='word/document.xml';}});}
  if(['txt','md','csv'].includes(suffix!)){
   let text:string;try{text=new TextDecoder('utf-8',{fatal:true}).decode(data);}catch{text=new TextDecoder('gb18030',{fatal:true}).decode(data);}if(text.includes('\0'))throw new DomainError('文本文件包含二进制内容');
   if(suffix==='csv'){const book=XLSX.read(text,{type:'string',raw:true});const rows=XLSX.utils.sheet_to_json<any[]>(book.Sheets[book.SheetNames[0]],{header:1,raw:true,blankrows:true});return splitParts(rows.map((row,i)=>[`CSV 行 ${i+1}`,row.map(v=>String(v??'')).join(' | ')]));}return parseText(text);
  }
  if(suffix==='docx'){
   if(!office['word/document.xml'])throw new DomainError('文件不是有效的 DOCX 文档');
   const tree=new XMLParser({preserveOrder:true,ignoreAttributes:true,removeNSPrefix:true,parseTagValue:false,processEntities:true}).parse(strFromU8(office['word/document.xml']));
   const parts:[string,string][]=[];let index=0;
   function walk(nodes:any[]){for(const node of nodes){for(const [key,value] of Object.entries(node)){if(key==='p'){parts.push([`DOCX 段落 ${++index}`,xmlText(value as any[])]);}else if(key!=='#text'&&Array.isArray(value))walk(value);}}}walk(tree);return splitParts(parts);
  }
  if(suffix==='pdf'){
   const {getDocumentProxy}=await import('unpdf');const pdf=await getDocumentProxy(data,{useSystemFonts:false});const parts:[string,string][]=[];
   try{for(let i=1;i<=pdf.numPages;i++){const page=await pdf.getPage(i),content=await page.getTextContent();const text=content.items.map((item:any)=>typeof item.str==='string'?item.str+(item.hasEOL?'\n':' '):'').join('');if(!text.trim())throw new DomainError(`PDF 第 ${i} 页没有可提取文本，请先转换扫描页后上传`);parts.push([`PDF 第 ${i} 页`,text]);}}finally{await pdf.loadingTask.destroy();}return splitParts(parts);
  }
  const workbook=XLSX.read(data,{type:'array',cellFormula:true,cellDates:false});const parts:[string,string][]=[];
  for(const name of workbook.SheetNames){const sheet=workbook.Sheets[name];if(!sheet['!ref'])continue;const range=XLSX.utils.decode_range(sheet['!ref']);if((range.e.r-range.s.r+1)*(range.e.c-range.s.c+1)>2_000_000)throw new DomainError('工作表范围超过云端处理预算，请拆分文件');for(let row=range.s.r;row<=range.e.r;row++){const values=[];for(let col=range.s.c;col<=range.e.c;col++){const cell=XLSX.utils.encode_cell({r:row,c:col}),v=sheet[cell];if(v)values.push(`${cell}=${v.f?'='+v.f:v.v??''}`);}if(values.length)parts.push([`${name} · 行 ${row+1}`,values.join(' | ')]);}}
  return splitParts(parts);
 }catch(e){if(e instanceof DomainError)throw e;throw new DomainError('无法解析文件，请检查文件完整性或转换为 TXT/CSV');}
}
export function cellSafe(value:any){let text=String(value??'').replace(/[\x00-\x08\x0b\x0c\x0e-\x1f]/g,'');if(/^[=+@-]/.test(text.trimStart())||/^[\t\r\n]/.test(text))text="'"+text;return text;}
export function exportCases(artifact:Json,layout='case',selected?:string[]){
 if(artifact.type!=='cases'||!['case','step'].includes(layout))throw new DomainError('导出类型或布局无效');let items=artifact.items;
 if(selected){if(!selected.length||selected.some(id=>!items.some((i:Json)=>i.id===id)))throw new DomainError('所选导出条目无效');items=items.filter((i:Json)=>selected.includes(i.id));}
 const rows:any[][]=[['Case ID','Title','Type','Priority','Preconditions','Steps','Expected Result']];
 for(const item of items){const base=[item.id,item.title,item.type,item.priority,item.preconditions];if(layout==='step')item.steps.forEach((step:Json,i:number)=>rows.push([...base,`${i+1}. ${step.action}`,step.expected].map(cellSafe)));else rows.push([...base,item.steps.map((s:Json,i:number)=>`${i+1}. ${s.action}`).join('\n'),item.steps.map((s:Json,i:number)=>`${i+1}. ${s.expected}`).join('\n')].map(cellSafe));}
 const sheet=XLSX.utils.aoa_to_sheet(rows);sheet['!cols']=[20,42,18,12,38,64,64].map(wch=>({wch}));sheet['!autofilter']={ref:sheet['!ref']!};
 const book=XLSX.utils.book_new();XLSX.utils.book_append_sheet(book,sheet,(artifact._profile?.sheet_name??'Test Cases').replace(/[\\/*?:\[\]]/g,'_').slice(0,31)||'Test Cases');return new Uint8Array(XLSX.write(book,{type:'array',bookType:'xlsx'}));
}
