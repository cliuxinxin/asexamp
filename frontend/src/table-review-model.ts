import {equalConfig} from './profile-diff';
import type {Json} from './types';

export type ReviewColumn = Json & {field:string;header:string;canonical_field?:string;editable?:boolean};
export type ReviewRow = {item_id:string;step_index:number|null;cells:string[]};
export type ReviewChange = {key:string;itemId:string;field:string;stepIndex?:number;operation:'add'|'delete'|'update'};
export type ReviewDecision = 'accept'|'reject'|'manual';
export type TableReviewData = {
 artifact_id:string;title:string;artifact_revision:number;profile_id?:string;profile_revision?:number;
 layout:'case'|'step';columns:ReviewColumn[];original_items:Json[];proposed_items:Json[];
 original_rows:ReviewRow[];proposed_rows:ReviewRow[];issues:unknown[];read_only:boolean;
 run_id?:string;proposal_id?:string;prompt_id?:string;stale?:boolean;message?:string;
};
export const cloneItems=(items:Json[]):Json[]=>structuredClone(items);
export const rowKey=(row:ReviewRow)=>JSON.stringify([row.item_id,row.step_index]);
export const changeKey=(itemId:string,field:string,stepIndex?:number)=>JSON.stringify([itemId,field,stepIndex??null]);
export function canonicalField(column:ReviewColumn):string {
 return column.canonical_field??({case_description:'description',test_description:'description',case_desc:'description',test_case_description:'description'} as Record<string,string>)[column.field]??column.field;
}
export function reviewChanges(original:Json[],proposed:Json[]):ReviewChange[]{
 const before=new Map(original.map(item=>[String(item.id),item])),after=new Map(proposed.map(item=>[String(item.id),item]));
 const result:ReviewChange[]=[];
 for(const id of new Set([...before.keys(),...after.keys()])){
  const left=before.get(id),right=after.get(id);
  if(!left||!right){result.push({key:changeKey(id,'$row'),itemId:id,field:'$row',operation:left?'delete':'add'});continue;}
  for(const field of new Set([...Object.keys(left),...Object.keys(right)])){
   if(field==='id'||field.startsWith('_')||equalConfig(left[field],right[field]))continue;
   if(field==='steps'&&Array.isArray(left.steps)&&Array.isArray(right.steps)&&left.steps.length===right.steps.length){
    // Keep action and expected attached to their stable step index.
    left.steps.forEach((step:Json,index:number)=>{
     for(const part of ['action','expected'])if(!equalConfig(step[part],right.steps[index][part]))result.push({key:changeKey(id,'steps.'+part,index),itemId:id,field:'steps.'+part,stepIndex:index,operation:'update'});
    });
    if(left.steps.some((step:Json,index:number)=>!equalConfig(Object.fromEntries(Object.entries(step).filter(([key])=>!['action','expected'].includes(key))),Object.fromEntries(Object.entries(right.steps[index]).filter(([key])=>!['action','expected'].includes(key))))))result.push({key:changeKey(id,'steps'),itemId:id,field:'steps',operation:'update'});
   }else result.push({key:changeKey(id,field),itemId:id,field,operation:'update'});
  }
 }
 return result;
}
export function cellChanges(changes:ReviewChange[],itemId:string,column:ReviewColumn,stepIndex:number|null):ReviewChange[]{
 const field=canonicalField(column);
 return changes.filter(change=>change.itemId===itemId&&(change.field==='$row'||change.field===field||(field==='steps'||field==='expected')&&(change.field==='steps'||change.field==='steps.'+(field==='steps'?'action':'expected')&&(stepIndex===null||change.stepIndex===stepIndex))));
}
export function applyDecisions(draft:Json[],original:Json[],proposed:Json[],changes:ReviewChange[],decision:'accept'|'reject'):Json[]{
 const source=new Map((decision==='accept'?proposed:original).map(item=>[String(item.id),item]));
 const rows=new Map(cloneItems(draft).map(item=>[String(item.id),item]));
 for(const change of changes){
  const next=source.get(change.itemId);
  if(change.field==='$row'){if(next)rows.set(change.itemId,structuredClone(next));else rows.delete(change.itemId);continue;}
  const item=rows.get(change.itemId);if(!item||!next)continue;
  if(change.field.startsWith('steps.')&&change.stepIndex!==undefined){const part=change.field.slice(6);item.steps[change.stepIndex][part]=structuredClone(next.steps[change.stepIndex][part]);}
  else if(Object.hasOwn(next,change.field))item[change.field]=structuredClone(next[change.field]);else delete item[change.field];
 }
 const order=[...new Set([...proposed.map(item=>String(item.id)),...original.map(item=>String(item.id))])];
 return order.flatMap(id=>rows.has(id)?[rows.get(id)!]:[]);
}
function issueIds(issue:Json):string[]{
 return [...new Set([...(Array.isArray(issue.case_ids)?issue.case_ids:[]),...(Array.isArray(issue.item_ids)?issue.item_ids:[]),...(Array.isArray(issue.ids)?issue.ids:[]),issue.case_id,issue.item_id].filter(value=>typeof value==='string'))];
}
export function issueText(issue:unknown):string{
 if(typeof issue==='string')return issue;
 if(!issue||typeof issue!=='object')return String(issue??'');
 const value=issue as Json;
 return [value.title,value.detail??value.message??value.reason??value.description].filter(Boolean).join('：')||JSON.stringify(value);
}
export function issuesForCell(issues:unknown[],itemId:string,column:ReviewColumn,isAnchor:boolean):unknown[]{
 return issues.filter(issue=>{
  if(!issue||typeof issue!=='object'||!issueIds(issue as Json).includes(itemId))return false;
  const value=issue as Json;const fields=[...(Array.isArray(value.fields)?value.fields:[]),value.field,value.field_name].filter(field=>typeof field==='string');
  return fields.length?fields.some(field=>field===column.field||field===canonicalField(column)||(canonicalField(column)==='expected'&&field==='steps')||field.startsWith(canonicalField(column)+'.')):isAnchor;
 });
}
export function globalIssues(issues:unknown[],items:Json[],columns:ReviewColumn[]):unknown[]{
 const ids=new Set(items.map(item=>String(item.id)));
 return issues.filter(issue=>{
  if(!issue||typeof issue!=='object')return true;
  const value=issue as Json,targets=issueIds(value);
  if(!targets.some(id=>ids.has(id)))return true;
  const fields=[...(Array.isArray(value.fields)?value.fields:[]),value.field,value.field_name].filter(field=>typeof field==='string');
  return fields.length>0&&!fields.some(field=>columns.some(column=>field===column.field||field===canonicalField(column)||field.startsWith(canonicalField(column)+'.')));
 });
}
