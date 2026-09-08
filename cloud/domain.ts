import contracts from './contracts.json';
export type Json = Record<string, any>;
export const now = () => new Date().toISOString();
export const uid = (prefix = '') => prefix + crypto.randomUUID().replaceAll('-', '');
export const copy = <T>(value: T): T => structuredClone(value);
export const publicValue = (value: Json): Json => Object.fromEntries(Object.entries(value).filter(([key]) => !key.startsWith('_')));
export function canonical(value:any):string {
  const sort=(v:any):any=>Array.isArray(v)?v.map(sort):v&&typeof v==='object'?Object.fromEntries(Object.keys(v).sort().map(k=>[k,sort(v[k])])):v;
  return JSON.stringify(sort(value));
}
export const INTENTS = ['review_requirement','generate_scenario','generate_case','review_case','query','learn_template','modify'];
export const ROLES = ['primary','change','supplement','clarification','example','knowledge'];
export const ACTIVE = ['queued','running','waiting'];
export class DomainError extends Error { constructor(message: string, public status = 400) { super(message); } }
export class LeaseLost extends DomainError { constructor() { super('执行连接已变化，未接受过期结果',409); } }
export class ValidationError extends DomainError {
  issue: Json;
  constructor(path: string, expected: string, value: any, code='type_mismatch') {
    const actual = value === undefined ? 'missing' : value === null ? 'null' : Array.isArray(value) ? 'array' : typeof value;
    super(`${path}：期望 ${expected}，实际 ${actual}`);
    this.issue = {path, expected, actual, code};
  }
}
export function profileConfig(config: any): Json {
  if (!config || typeof config !== 'object' || Array.isArray(config)) throw new DomainError('Profile 配置必须为对象');
  const p = {...contracts.profile,...config};
  for (const key of ['language','scenario_level','case_level','additional_rules','scope','sheet_name']) if(typeof p[key] !== 'string') throw new DomainError(`${key} 必须为字符串`);
  if(!Array.isArray(p.case_types)||p.case_types.some((v:any)=>typeof v!=='string'||!v)) throw new DomainError('case_types 必须为字符串数组');
  if(!['case','step'].includes(p.excel_layout)||JSON.stringify(p).length>100000) throw new DomainError('Profile 配置无效或过大');
  return p;
}
export function validateItems(kind:string, items:any, evidence:Json, scenarioIds?:Set<string>) {
  if(!Array.isArray(items)) throw new ValidationError('items','array',items);
  const seen = new Set();
  items.forEach((item,index)=>{
    const path=`items[${index}]`;
    if(!item||typeof item!=='object'||Array.isArray(item))throw new ValidationError(path,'object',item);
    if(typeof item.id!=='string'||!item.id||item.id.length>200)throw new ValidationError(path+'.id','string:1..200',item.id);
    if(seen.has(item.id))throw new ValidationError(path+'.id','unique_id',item.id,'duplicate_id');seen.add(item.id);
    if(typeof item.title!=='string'||!item.title.trim())throw new ValidationError(path+'.title','nonempty_string',item.title);
    const refs=item.refs??[];
    if(!Array.isArray(refs)||refs.some((ref:any)=>typeof ref!=='string'))throw new ValidationError(path+'.refs','array_of_strings',refs);
    refs.forEach((ref:string,i:number)=>{
      if(!Object.hasOwn(evidence,ref))throw new ValidationError(`${path}.refs[${i}]`,'provided_evidence_id',ref,'invalid_reference');
      if(evidence[ref].role==='example'&&kind!=='proposal')throw new ValidationError(`${path}.refs[${i}]`,'non_example_evidence_id',ref,'example_reference');
    });
    if(['analysis','scenarios','cases','review'].includes(kind)&&!refs.length&&!(kind==='analysis'&&item.assumption===true))throw new ValidationError(path+'.refs','nonempty_evidence_refs',refs,'missing_reference');
    const strings=kind==='cases'?['type','priority','preconditions','scenario_id']:kind==='scenarios'?['description','priority']:kind==='analysis'?['description']:[];
    for(const key of strings)if(typeof item[key]!=='string')throw new ValidationError(`${path}.${key}`,'string',item[key]);
    if(kind==='cases'){
      if(scenarioIds&&!scenarioIds.has(item.scenario_id))throw new ValidationError(path+'.scenario_id','provided_scenario_id',item.scenario_id,'invalid_reference');
      if(!Array.isArray(item.steps)||!item.steps.length)throw new ValidationError(path+'.steps','nonempty_array',item.steps);
      item.steps.forEach((step:any,i:number)=>{
        if(!step||typeof step!=='object'||Array.isArray(step))throw new ValidationError(`${path}.steps[${i}]`,'object',step);
        for(const key of ['action','expected'])if(typeof step[key]!=='string')throw new ValidationError(`${path}.steps[${i}].${key}`,'string',step[key]);
      });
    }
  });
  return items;
}
export function applyOperations(items:Json[], operations:any, selectedIds?:string[]|null):Json[] {
  if(!Array.isArray(operations))throw new ValidationError('operations','array',operations);
  const result = new Map(items.map(item=>[item.id,copy(item)]));
  operations.forEach((op,index)=>{
    const path=`operations[${index}]`;
    if(!op||typeof op!=='object'||!['add','update','delete'].includes(op.op))throw new ValidationError(path+'.op','add|update|delete',op?.op);
    if(op.op==='add'){
      if(!op.item||typeof op.item!=='object'||Array.isArray(op.item)||typeof op.item.id!=='string'||!op.item.id||result.has(op.item.id))throw new ValidationError(path+'.item','object_with_new_id',op.item);
      result.set(op.item.id,copy(op.item));
    }else{
      if(!result.has(op.id))throw new ValidationError(path+'.id','existing_item_id',op.id);
      if(selectedIds&&!selectedIds.includes(op.id))throw new ValidationError(path+'.id','selected_item_id',op.id);
      if(op.op==='delete')result.delete(op.id);
      else{
        if(!op.item||typeof op.item!=='object'||Array.isArray(op.item)||op.item.id!==undefined&&op.item.id!==op.id)throw new ValidationError(path+'.item','object_with_unchanged_id',op.item);
        result.set(op.id,{...result.get(op.id),...op.item,id:op.id});
      }
    }
  });
  return [...result.values()];
}
export function identified(items:any,prefix:string):Json[]{
  if(!Array.isArray(items))throw new ValidationError('items','array',items);
  return items.map(item=>item&&typeof item==='object'&&!Array.isArray(item)?{...item,id:item.id||uid(prefix)}:item);
}
export function batches(items:Json[], budget=18000):Json[][] {
  const result:Json[][]=[];let group:Json[]=[];let size=0;
  for(const item of items){const length=JSON.stringify(item).length;if(group.length&&size+length>budget){result.push(group);group=[];size=0;}group.push(item);size+=length;}
  if(group.length)result.push(group);return result.length?result:[[]];
}
export function excerpt(value:string,budget:number){if(JSON.stringify(value).length<=budget)return value;let left=Math.floor((budget-80)/2);while(left>0){const text=value.slice(0,left)+'\n[Routing preview: middle omitted]\n'+value.slice(-left);if(JSON.stringify(text).length<=budget)return text;left=Math.floor(left*.8);}return '';}
export function checkPage(result:Json, previous:Json[], evidence:Json, kind:string, cursor:any, used:string[], scenarioIds?:Set<string>):Json[]{
  if(typeof result.has_more!=='boolean')throw new ValidationError('has_more','boolean',result.has_more);
  const items=identified(result.items,kind==='cases'?'tc_':'sc_');validateItems(kind,items,evidence,scenarioIds);
  if(!items.length)throw new DomainError('模型返回空分页，未跳过需求；请重试当前阶段');
  const oldIds=new Set(previous.map(i=>i.id));
  const signature=(i:Json)=>canonical(Object.fromEntries(Object.entries(i).filter(([k])=>k!=='id')));
  const signatures=new Set(previous.map(signature));
  for(const item of items){if(oldIds.has(item.id)||signatures.has(signature(item)))throw new DomainError('分页包含重复条目，未提交重复结果');oldIds.add(item.id);signatures.add(signature(item));}
  if(result.has_more&&(typeof result.next_cursor!=='string'||!result.next_cursor||result.next_cursor===cursor||used.includes(result.next_cursor)))throw new DomainError('分页游标没有前进，未提交当前页');
  return [...previous,...items];
}
