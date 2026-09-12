"""Bounded routing metadata and authoritative target resolution for one turn."""
import copy
import json
from .schemas import DomainError
from .storage import now

BUSINESS_TYPES = {'analysis','scenarios','cases','review'}
RUN_SETTING_FIELDS = ('profile_id','mode','source_ids','depth','case_types','profile_override','experience','artifact_id')


def explicit_run_settings(body):
    # HTTP keeps its historical defaults separately from caller-supplied values.
    # Old persisted turns have no marker and retain their original precedence.
    return set(body.get('_explicit_run_settings', ()))

class NeedsInput(Exception):
    def __init__(self, message, candidates=None, field='artifact_id'):
        self.message, self.candidates, self.field = message, candidates or [], field
        super().__init__(message)


def get_state(store, chat):
    key='conversation:'+chat['id']
    try:return store.get('conversation_state',key)
    except DomainError:
        return {'id':key,'chat_id':chat['id'],'project_id':chat['project_id'],'focus':None,'continue_epoch':0}


def visible_artifacts(store, chat):
    return [a for a in store.list('artifact',project_id=chat['project_id'],chat_id=chat['id'])
            if a.get('_visible') and a.get('type') in BUSINESS_TYPES and a.get('chat_id')==chat['id'] and a.get('project_id')==chat['project_id']]


def artifact_brief(a):
    return {'id':a['id'],'type':a['type'],'title':a.get('title','')[:160], 'revision':a['revision'],
            'item_count':len(a.get('items',[])),
            'items':[{'id':r.get('id'),'title':str(r.get('title',''))[:100],'ordinal':i}
                     for i,r in enumerate(a.get('items',[])[:30],1)],
            'items_partial':len(a.get('items',[]))>30,
            'lineage':a.get('report',{}).get('lineage',{})}


def build_context(store, engine, chat, body, registry):
    state=get_state(store,chat)
    artifacts=visible_artifacts(store,chat)
    anchors={body.get('artifact_id'),(state.get('focus') or {}).get('artifact_id')}
    runs=store.runs(chat_id=chat['id'],statuses=('queued','running','waiting','failed'))
    for run in runs:anchors.add(run.get('interrupt',{}).get('artifact_id'))
    artifacts.sort(key=lambda a:(a['id'] in anchors,a.get('created_at','')),reverse=True)
    pending=[{k:v for k,v in p.items() if k not in ('original_body','actions','result') and not k.startswith('_')}
             for p in store.list('conversation_pending',chat_id=chat['id']) if p.get('status')=='open']
    messages=sorted(store.list('message',chat_id=chat['id']),key=lambda m:m.get('created_at',''))[-6:]
    sources=[s for s in store.list('source',project_id=chat['project_id']) if s.get('_active',True)
             and (s.get('chat_id')==chat['id'] or s.get('_project_shared'))]
    explicit=explicit_run_settings(body)
    requested_settings={key:copy.deepcopy(body[key]) for key in ('mode','profile_id','depth','case_types')
                        if key in explicit and key in body}
    if 'profile_override' in explicit and isinstance(body.get('profile_override'),dict):
        requested_settings['profile_override_fields']=sorted(body['profile_override'])
    value={'content':body['content'],'intent_hint':body.get('intent_hint','auto'),
        'requested_settings':requested_settings,
        'focus':state.get('focus'),'reply_to':body.get('reply_to'),'pending':pending[-8:],
        'view':{k:body.get(k) for k in ('artifact_id','artifact_revision','selected_ids','view_order') if body.get(k) is not None},
        'runs':[{'id':r['id'],'status':r['status'],'stage':r.get('stage'),'intent':r.get('intent'),'mode':r.get('mode'),
                 'interrupt':r.get('interrupt'),'stop_after':r.get('stop_after'),'scope':r.get('scope'),
                 'control_version':r.get('control_version',0),'interrupt_id':r.get('_interrupt_id')} for r in runs],
        'artifacts':[artifact_brief(a) for a in artifacts[:12]],'artifacts_partial':len(artifacts)>12,
        'sources':[{'id':s['id'],'name':str(s.get('name',''))[:160],'role':chat.get('_source_roles',{}).get(s['id'],s.get('role')),'characters':s.get('characters'),
                    'project_shared':bool(s.get('_project_shared'))} for s in sources[:40]],
        'attached_source_ids':body.get('source_ids'),
        'profiles':[{'id':p['id'],'name':p['name'],'version':p['version']} for p in store.list('profile',project_id=chat['project_id'])],
        'profile_id':body.get('profile_id') or chat.get('profile_id'),
        'active_turns':[{'id':t['id'],'status':t['status'],'content':t.get('_body',{}).get('content','')[:240]} for t in store.list('conversation_turn',chat_id=chat['id']) if t.get('status') in ('running','recoverable','deferred')][-6:],
        'active_commands':[{**{k:c.get(k) for k in ('id','turn_id','name','effect','status')},
                            'arguments':{k:v for k,v in c.get('_resolved',c.get('arguments',{})).items() if k in ('artifact_id','expected_revision','selected_ids','proposal_id','run_id','source_ids')},
                            'instruction':str(c.get('arguments',{}).get('instruction',''))[:240]}
                           for c in store.list('conversation_command',chat_id=chat['id'])
                           if c.get('status') in ('running','deferred','pending','needs_input')][-8:],
        'recent_messages':[{'role':m.get('role'),'text':str(m.get('content',''))[:600]} for m in messages],
        'capabilities':[{k:v for k,v in definition.items() if k in ('name','effect','description','parameters','target_types','target_required','context_policy')}
                        | {'name':name} for name,definition in registry.items()]}
    value['catalog_totals']={
        'artifacts':len(artifacts),'sources':len(sources),'pending':len(pending),
        'recent_messages':len(store.list('message',chat_id=chat['id'])),
        'active_turns':sum(t.get('status') in ('running','recoverable','deferred') for t in store.list('conversation_turn',chat_id=chat['id'])),
        'active_commands':sum(c.get('status') in ('running','deferred','pending','needs_input') for c in store.list('conversation_command',chat_id=chat['id']))}
    from .workspace_changes import workspace_state
    focus_id = body.get('artifact_id') or (state.get('focus') or {}).get('artifact_id')
    if focus_id not in {a['id'] for a in artifacts if a['type'] != 'review'}:
        focus_id = None
    workspace = workspace_state(store, chat, focus_id)
    impact = workspace['impact']
    value['workspace'] = {
        'artifact_id': workspace['artifact_id'], 'stages': workspace['stages'],
        'impact': {'status': impact['status'], 'summary': impact['summary'],
            'source_ids': impact['source_ids'][:40], 'source_count': len(impact['source_ids']),
            'affected': [{k: a[k] for k in ('artifact_id','type','count','reason')} for a in impact['affected']]},
        'next_action': {**workspace['next_action'], 'arguments': {
            key: value for key, value in workspace['next_action']['arguments'].items()
            if not isinstance(value, list) or len(value) <= 40}},
        'pending_proposal': {k: workspace['pending_proposal'][k] for k in ('id','artifact_id','summary')}
            if workspace.get('pending_proposal') else None,
    }
    # Interrupt documents and whole reports never enter the interpreter.
    for run in value['runs']:
        interrupt=run.get('interrupt') or {}
        run['interrupt']={k:interrupt[k] for k in ('type','artifact_id','message') if k in interrupt}
        run['interrupt']['question_count']=len(interrupt.get('questions',[]))
    try:
        from .clarification import get_draft
        for run in runs:
            if run.get('interrupt',{}).get('type')=='clarification':
                draft=get_draft(store,run['id'])
                value['clarification_draft']={'id':draft['id'],'revision':draft['revision'],
                    'question_count':len(draft.get('questions',[])), 'questions_partial':len(draft.get('questions',[]))>30,
                    'questions':[{'id':q['id'],'question':q['question'],'answer':q.get('answer',''),
                                  'suggestion':q.get('suggestion')} for q in draft.get('questions',[])[:30]]}
    except ImportError:pass
    fits=lambda:len(json.dumps(value,ensure_ascii=False))<=85000 and (not hasattr(engine,'fits') or engine.fits('conversation_turn',value))
    if not fits():
        for artifact in value['artifacts']:
            artifact['items']=artifact['items'][:5];artifact['items_partial']=True
        value['recent_messages']=value['recent_messages'][-2:]
    if not fits():
        value['artifacts']=[a for a in value['artifacts'] if a['id'] in anchors]+[a for a in value['artifacts'] if a['id'] not in anchors][:2]
        value['artifacts_partial']=True;value['sources']=value['sources'][:10]
    if not fits():raise DomainError('本条指令与能力说明超过模型容量，请缩短消息或调整模型容量。')
    value['catalog_partial']={key:len(value[key])<total for key,total in value['catalog_totals'].items()}
    return value


def resolve_artifact(store,chat,body,state,name,args,definition=None):
    args=copy.deepcopy(args)
    artifacts=visible_artifacts(store,chat);known={a['id']:a for a in artifacts}
    if definition is None:
        from .conversation import default_registry
        definition=default_registry().get(name,{})
    allowed=definition.get('target_types',())
    if not allowed:return args
    required_type=args.get('artifact_type')
    if required_type is not None and required_type not in allowed:raise DomainError('成果类型与本次操作不匹配')
    if len(allowed)==1:required_type=allowed[0]
    artifacts=[a for a in artifacts if a['type'] in allowed]
    aid=args.get('artifact_id');focus=state.get('focus') or {};chosen=None
    # A selected row is an explicit target. A merely rendered result may be stale,
    # so without a selection the established conversational focus still wins.
    ui_selected=bool(body.get('artifact_id') and body.get('selected_ids'))
    context_ids=(body.get('artifact_id'),focus.get('artifact_id')) if ui_selected else (
        focus.get('artifact_id'),body.get('artifact_id'))
    if aid:
        chosen=known.get(aid)
        if chosen is None:raise DomainError('指定成果不属于当前对话的已保存结果',404)
    title=args.get('artifact_title')
    if not chosen and title:
        matches=[a for a in artifacts if a.get('title')==title]
        if len(matches)==1:chosen=matches[0]
        elif len(matches)>1:raise NeedsInput('这几份成果名称相同，请选择要操作的一份。',[artifact_brief(a) for a in matches])
    if args.get('from_case') or args.get('related_to_case'):
        case=chosen or next((known[candidate_id] for candidate_id in context_ids if candidate_id in known),None)
        if case and case['type']=='cases':
            lineage=case.get('report',{}).get('lineage',{})
            chosen=known.get(lineage.get('scenario_artifact_id'))
            ids=args.pop('case_ids',None) or (body.get('selected_ids') if ui_selected and case['id']==body.get('artifact_id') else None) or focus.get('selected_ids') or body.get('selected_ids')
            if chosen and ids:
                args['selected_ids']=list(dict.fromkeys(r.get('scenario_id') for r in case['items'] if r['id'] in ids))
    if not chosen:
        candidates=[a for a in artifacts if not required_type or a['type']==required_type]
        for candidate_id in context_ids:
            candidate=known.get(candidate_id)
            if candidate and (not required_type or candidate['type']==required_type):
                chosen=candidate;break
            if candidate and candidate['type']=='cases' and required_type=='scenarios':
                chosen=known.get(candidate.get('report',{}).get('lineage',{}).get('scenario_artifact_id'))
                if chosen:break
        if not chosen:
            for run in store.runs(chat_id=chat['id'],statuses=('waiting',)):
                candidate=known.get(run.get('interrupt',{}).get('artifact_id'))
                if candidate and (not required_type or candidate['type']==required_type):chosen=candidate;break
        if not chosen and len(candidates)==1:chosen=candidates[0]
        if not chosen:
            if not definition.get('target_required') and not required_type and not candidates:return args
            raise NeedsInput('请说明要使用哪份成果。' if candidates else '当前还没有可用的成果，请先生成或导入。',
                             [artifact_brief(a) for a in candidates])
    if chosen['type'] not in allowed or required_type and chosen['type']!=required_type:raise DomainError('指定成果的类型与本次操作不匹配')
    args['artifact_id']=chosen['id']
    revision=args.get('expected_revision',args.get('artifact_revision',args.get('revision')))
    if revision is None and body.get('artifact_id')==chosen['id']:revision=body.get('artifact_revision')
    if revision is None:revision=chosen['revision']
    if type(revision) is not int or revision<1:raise DomainError('成果版本必须为正整数')
    args['expected_revision']=revision
    snapshot=chosen if revision==chosen['revision'] else store.revision(chosen['id'],revision)
    known_ids=[r['id'] for r in snapshot.get('items',[])]
    selected=args.get('selected_ids')
    ordinals=args.pop('ordinals',args.pop('scenario_ordinals',None))
    if selected is None and args.get('scenario_ids'):selected=args['scenario_ids']
    if args.get('scope')=='inherit_previous' or args.pop('inherit_previous',False):
        if focus.get('artifact_id')!=chosen['id']:raise NeedsInput('本次对象与上一轮不同，请说明需要哪些条目。')
        selected=focus.get('selected_ids')
    if args.get('scope')=='selected':selected=body.get('selected_ids')
    if ordinals:
        order=known_ids
        if body.get('artifact_id')==chosen['id'] and body.get('view_order') is not None:
            order=body['view_order']
            if len(order)!=len(set(order)) or not set(order)<=set(known_ids):raise DomainError('页面条目顺序已过期，请刷新后重试',409)
        if any(type(i) is not int or i<1 or i>len(order) for i in ordinals):raise NeedsInput('条目序号超出当前显示范围，请说明有效编号。')
        selected=list(dict.fromkeys([*(selected or []),*(order[i-1] for i in ordinals)]))
    if selected is None and body.get('artifact_id')==chosen['id'] and body.get('selected_ids') and args.get('scope')!='all':selected=body['selected_ids']
    if selected is not None:
        if not isinstance(selected,list) or not selected or any(not isinstance(i,str) for i in selected) or not set(selected)<=set(known_ids):
            raise NeedsInput('请选择当前成果中存在的条目编号。')
        args['selected_ids']=list(dict.fromkeys(selected))
    return args


async def expand_catalog(store, engine, chat, name, args, turn_id=None):
    """Page metadata only; omitted objects still require authoritative scope binding."""
    kind=args.get('catalog','artifacts')
    offset=args.get('offset',0);limit=args.get('limit',20)
    if type(offset) is not int or offset<0 or type(limit) is not int or not 1<=limit<=50:
        raise DomainError('目录分页必须使用非负 offset 与 1–50 的 limit')
    if kind=='artifacts':
        rows=[artifact_brief(a) for a in visible_artifacts(store,chat)]
        for row in rows:row.pop('items',None)
    elif kind=='items':
        artifact=next((a for a in visible_artifacts(store,chat) if a['id']==args.get('artifact_id')),None)
        if artifact is None:raise DomainError('指定成果不属于当前对话',404)
        rows=[{'id':r['id'],'title':str(r.get('title',''))[:160],'ordinal':i} for i,r in enumerate(artifact.get('items',[]),1)]
    elif kind=='sources':
        rows=[{'id':s['id'],'name':str(s.get('name',''))[:160],'role':s.get('role')} for s in store.list('source',project_id=chat['project_id'])
              if s.get('_active',True) and (s.get('chat_id')==chat['id'] or s.get('_project_shared'))]
    elif kind=='pending':
        rows=[{k:p.get(k) for k in ('id','kind','message','command_id','proposal_id')} for p in store.list('conversation_pending',chat_id=chat['id']) if p.get('status')=='open']
    elif kind=='active_commands':
        rows=[{k:c.get(k) for k in ('id','turn_id','name','effect','status')} for c in store.list('conversation_command',chat_id=chat['id'])
              if c.get('status') in ('running','deferred','pending','needs_input')]
    elif kind=='active_turns':
        rows=[{'id':t['id'],'status':t['status'],'content':t.get('_body',{}).get('content','')[:240]}
              for t in store.list('conversation_turn',chat_id=chat['id']) if t.get('status') in ('running','recoverable','deferred')]
    elif kind=='pending_candidates':
        pending=store.get('conversation_pending',args.get('pending_id',''))
        if pending.get('chat_id')!=chat['id'] or pending.get('project_id')!=chat['project_id']:
            raise DomainError('待答事项不属于当前对话',404)
        rows=[{k:c.get(k) for k in ('id','title','type','revision')} for c in pending.get('candidates',[])]
    elif kind=='clarification_questions':
        from .clarification import get_draft
        run=store.run(args.get('run_id',''))
        if run['chat_id']!=chat['id'] or run['project_id']!=chat['project_id']:
            raise DomainError('任务不属于当前对话',404)
        rows=[{'id':q['id'],'question':str(q.get('question',''))[:300]} for q in get_draft(store,run['id']).get('questions',[])]
    else:raise DomainError('不支持的目录类型')
    page={'catalog':kind,'total':len(rows),'offset':offset,'items':rows[offset:offset+limit],
          'partial':offset>0 or offset+limit<len(rows),'next_offset':offset+limit if offset+limit<len(rows) else None}
    return {'status':'succeeded','message':'已读取目录。','parts':[{'type':'answer','text':json.dumps(page,ensure_ascii=False),'catalog':page}]}


CATALOG_CAPABILITY={
    'effect':'read','target_types':[],'target_required':False,'context_policy':'metadata',
    'description':'分页扩展省略的成果、条目、来源或待答目录，仅返回真实 ID 与简短元数据；使用 offset/limit，后续按真实 ID 选择操作。',
    'parameters':{'type':'object','properties':{'catalog':{'type':'string','enum':['artifacts','items','sources','pending','active_commands','active_turns','pending_candidates','clarification_questions']},
        'artifact_id':{'type':'string'},'pending_id':{'type':'string'},'run_id':{'type':'string'},'offset':{'type':'integer'},'limit':{'type':'integer'}}},
    'execute':expand_catalog}
