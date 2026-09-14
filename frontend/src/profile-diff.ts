export type TextChange={kind:'same'|'add'|'remove';text:string};
type Column=Record<string,unknown>&{field:string};
export type ColumnChange={field:string;before?:Column;after?:Column;beforeIndex:number;afterIndex:number;kind:'add'|'remove'|'update'|'move';changedKeys:string[];moved:boolean};

// Compare JSON configuration semantically, without depending on object key order.
export function equalConfig(left:unknown,right:unknown):boolean{
 if(Object.is(left,right))return true;
 if(!left||!right||typeof left!=='object'||typeof right!=='object')return false;
 if(Array.isArray(left)||Array.isArray(right))return Array.isArray(left)&&Array.isArray(right)&&left.length===right.length&&left.every((value,index)=>equalConfig(value,right[index]));
 const a=left as Record<string,unknown>,b=right as Record<string,unknown>;
 const keys=Object.keys(a);
 return keys.length===Object.keys(b).length&&keys.every(key=>Object.hasOwn(b,key)&&equalConfig(a[key],b[key]));
}

// Keep all text intact. Only the matching computation is bounded; large unrelated
// middles are marked as a replacement instead of allocating a quadratic matrix.
export function diffText(before:string,after:string):TextChange[]{
 if(before===after)return before?[{kind:'same',text:before}]:[];
 const left=Array.from(before),right=Array.from(after),result:TextChange[]=[];
 const append=(kind:TextChange['kind'],text:string)=>{
  if(!text)return;
  const last=result[result.length-1];
  if(last?.kind===kind)last.text+=text;else result.push({kind,text});
 };
 let start=0,endLeft=left.length,endRight=right.length;
 while(start<endLeft&&start<endRight&&left[start]===right[start])start++;
 while(endLeft>start&&endRight>start&&left[endLeft-1]===right[endRight-1]){endLeft--;endRight--;}
 append('same',left.slice(0,start).join(''));
 const a=left.slice(start,endLeft),b=right.slice(start,endRight);
 if(!a.length||!b.length||(a.length+1)*(b.length+1)>250_000){
  append('remove',a.join(''));append('add',b.join(''));
 }else{
  const width=b.length+1,dp=new Uint32Array((a.length+1)*width);
  for(let i=a.length-1;i>=0;i--)for(let j=b.length-1;j>=0;j--)dp[i*width+j]=a[i]===b[j]?1+dp[(i+1)*width+j+1]:Math.max(dp[(i+1)*width+j],dp[i*width+j+1]);
  let i=0,j=0;
  while(i<a.length||j<b.length){
   if(i<a.length&&j<b.length&&a[i]===b[j]){append('same',a[i]);i++;j++;}
   else if(i<a.length&&(j===b.length||dp[(i+1)*width+j]>=dp[i*width+j+1]))append('remove',a[i++]);
   else append('add',b[j++]);
  }
 }
 append('same',left.slice(endLeft).join(''));
 return result;
}

export function isColumnList(value:unknown):value is Column[]{
 if(!Array.isArray(value))return false;
 const fields=new Set<string>();
 return value.every(column=>{
  if(!column||typeof column!=='object'||typeof column.field!=='string'||!column.field||fields.has(column.field))return false;
  fields.add(column.field);return true;
 });
}

export function diffColumns(before:Column[],after:Column[]):{changes:ColumnChange[];unchanged:number}{
 const oldByField=new Map(before.map((column,index)=>[column.field,{column,index}]));
 const newByField=new Map(after.map((column,index)=>[column.field,{column,index}]));
 const oldRanks=new Map(before.filter(column=>newByField.has(column.field)).map((column,index)=>[column.field,index]));
 const newRanks=new Map(after.filter(column=>oldByField.has(column.field)).map((column,index)=>[column.field,index]));
 const changes:ColumnChange[]=[];let unchanged=0;
 for(const [index,column] of after.entries()){
  const old=oldByField.get(column.field);
  if(!old){changes.push({field:column.field,after:column,beforeIndex:-1,afterIndex:index,kind:'add',changedKeys:Object.keys(column),moved:false});continue;}
  const changedKeys=[...new Set([...Object.keys(old.column),...Object.keys(column)])].filter(key=>Object.hasOwn(old.column,key)!==Object.hasOwn(column,key)||!equalConfig(old.column[key],column[key]));
  const moved=oldRanks.get(column.field)!==newRanks.get(column.field);
  if(changedKeys.length||moved)changes.push({field:column.field,before:old.column,after:column,beforeIndex:old.index,afterIndex:index,kind:changedKeys.length?'update':'move',changedKeys,moved});
  else unchanged++;
 }
 for(const [index,column] of before.entries())if(!newByField.has(column.field))changes.push({field:column.field,before:column,beforeIndex:index,afterIndex:-1,kind:'remove',changedKeys:Object.keys(column),moved:false});
 return {changes,unchanged};
}
