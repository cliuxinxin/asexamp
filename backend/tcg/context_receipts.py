"""Persist bounded context provenance, never model prompts, source text or secrets."""
import copy
import hashlib
import json

from .schemas import DomainError
from .storage import now, public


BUDGET_FIELDS = {'fits','window','output_tokens','input_tokens','count_method','margin','output_limit_mode','request_digest'}
USAGE_FIELDS = {'input_tokens','output_tokens','prompt_tokens','completion_tokens','total_tokens','cached_tokens','reasoning_tokens'}
REF_FIELDS = {'id','artifact_id','source_id','project_id','revision','version','source_version','digest','role'}


def _digest(value):
    return hashlib.sha256(json.dumps(value,ensure_ascii=False,sort_keys=True,default=str).encode()).hexdigest()


def _ids(values):
    return list(dict.fromkeys(value for value in values if isinstance(value,str) and len(value)<=300))


def _manifest(value):
    if not isinstance(value,dict):return {}
    result={key:value[key] for key in ('version','digest') if key in value}
    for key in ('artifacts','sources','profiles'):
        if isinstance(value.get(key),list):
            result[key]=[{field:row[field] for field in REF_FIELDS if field in row}
                         for row in value[key] if isinstance(row,dict)]
    if isinstance(value.get('run'),dict):
        result['run']={key:value['run'][key] for key in ('id','project_id','digest') if key in value['run']}
    return result


def _coverage(value):
    """Copy only counts, flags and reference arrays; omit explanatory free text."""
    if not isinstance(value,dict):return {}
    result={}
    for key,item in value.items():
        if key.endswith('_ids') and isinstance(item,list):result[key]=_ids(item)
        elif key=='missing_historical_source_versions' and isinstance(item,list):
            result[key]=[{field:ref[field] for field in ('source_id','version','source_version') if field in ref} for ref in item if isinstance(ref,dict)]
        elif isinstance(item,bool) or (type(item) in (int,float) and item>=0):result[key]=item
        elif isinstance(item,dict):
            safe=_coverage(item)
            if safe:result[key]=safe
    return result


def start(store, task, context, *, binding, call_id, budget=None):
    """Record exactly supplied context metadata before a model request begins."""
    binding=binding or {}
    if not isinstance(call_id,str) or not call_id or len(call_id)>200:raise DomainError('模型调用编号无效')
    turn_id=binding.get('turn_id');run_id=binding.get('run_id')
    if not turn_id and not run_id:return None
    with store.transaction():
        owner=store.get('conversation_turn',turn_id) if turn_id else store.run(run_id)
        chat_id=owner['chat_id'];project_id=owner['project_id']
        if any(binding.get(key) and binding[key]!=value for key,value in (('chat_id',chat_id),('project_id',project_id))):
            raise DomainError('模型上下文不属于当前对话',403)
        if run_id:
            run=store.run(run_id)
            if run['chat_id']!=chat_id or run['project_id']!=project_id:raise DomainError('运行上下文不属于当前对话',403)
        command_id=binding.get('command_id')
        if command_id:
            command=store.get('conversation_command',command_id)
            if command.get('turn_id')!=turn_id or command.get('chat_id')!=chat_id:
                raise DomainError('命令上下文不属于当前对话操作',403)
        receipt_id='context:'+str(call_id)
        try:
            existing=store.get('context_receipt',receipt_id)
        except DomainError:existing=None
        fingerprint=_digest({'task':task,'context':context,'turn_id':turn_id,'run_id':run_id,'command_id':command_id})
        if existing:
            if existing.get('_fingerprint')!=fingerprint:raise DomainError('同一模型调用编号不能用于不同上下文',409)
            return receipt_id
        evidence=[e for key in ('evidence','new_evidence') for e in context.get(key,[]) if isinstance(e,dict)]
        formats=[e for e in context.get('format_references',[]) if isinstance(e,dict)]
        manifest=_manifest(context.get('dependency_manifest') or context.get('input_versions') or context.get('context_manifest'))
        references=[]
        for item in [context.get('artifact'),context.get('analysis'),*context.get('artifacts',[])]:
            if isinstance(item,dict) and item.get('id'):
                references.append({key:item[key] for key in ('id','type','revision') if key in item})
        value={'id':receipt_id,'call_id':call_id,'task':task,'project_id':project_id,'chat_id':chat_id,
            'turn_id':turn_id,'command_id':command_id,'run_id':run_id,'created_at':now(),'status':'running',
            'context_manifest':manifest,'artifact_refs':references,
            'coverage':_coverage(context.get('coverage',{})),
            'rule_coverage':_coverage(context.get('global_rules',{}).get('coverage',{})) if isinstance(context.get('global_rules'),dict) else {},
            'catalog_coverage':{'totals':_coverage(context.get('catalog_totals',{})),
                'partial':_coverage(context.get('catalog_partial',{}))},
            'evidence_ids':_ids(e.get('id') for e in evidence),
            'evidence_versions':[{key:e[key] for key in ('id','source_id','source_version','role') if key in e} for e in evidence],
            'format_reference_ids':_ids(e.get('id') for e in formats),
            'profile_digest':_digest(context['profile']) if 'profile' in context else None,
            'request_budget':{key:copy.deepcopy(item) for key,item in (budget or {}).items() if key in BUDGET_FIELDS},
            '_fingerprint':fingerprint}
        store.put('context_receipt',value)
        return receipt_id


def complete(store, receipt_id, status, *, error=None, usage=None):
    if receipt_id is None:return None
    if status not in ('succeeded','failed','cancelled'):raise DomainError('模型上下文回执状态无效')
    with store.transaction():
        value=store.get('context_receipt',receipt_id)
        if value['status']!='running':return public(value)
        value.update(status=status,completed_at=now())
        if error is not None:
            value['error']={'type':type(error).__name__}
            code=getattr(error,'status_code',None) or getattr(error,'status',None)
            if isinstance(code,int):value['error']['status']=code
        if isinstance(usage,dict):
            value['usage']={key:item for key,item in usage.items() if key in USAGE_FIELDS and type(item) in (int,float) and item>=0}
        store.put('context_receipt',value)
        return public(value)


def list_turn_contexts(store, chat_id, turn_id):
    chat=store.get('chat',chat_id);turn=store.get('conversation_turn',turn_id)
    if turn['chat_id']!=chat_id or turn['project_id']!=chat['project_id']:
        raise DomainError('对话操作不属于当前对话',404)
    return [public(value) for value in sorted(store.list('context_receipt',chat_id=chat_id),key=lambda v:(v.get('created_at',''),v['id']))
            if value.get('turn_id')==turn_id and value.get('project_id')==chat['project_id']]
