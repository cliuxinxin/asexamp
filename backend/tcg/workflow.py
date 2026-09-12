"""Version 7 authoring graph: each durable transition has one responsibility."""
import hashlib
import copy
import json
from typing import TypedDict
from langgraph.graph import StateGraph, START, END
from langgraph.types import interrupt
from langgraph.errors import GraphInterrupt
from .flow import FlowEngine, valid_question_suggestions
from .graph import State
from .schemas import DomainError, INTENTS, profile_config
from .documents import parse_text
from .storage import public, uid, now
from .case_fields import (template_columns, template_check, field_value, filled,
    materialize_fields, protect_non_ai_fields, column_signature)


class WorkflowState(State, total=False):
    clarification_answer: str
    clarification_questions: list[str]
    summary: str
    boundary_again: bool


class WorkflowEngine(FlowEngine):
    def reliable(self, run_id):
        return self.store.run(run_id).get('graph_version') in (4, 5, 6, 7)

    def direct(self, run_id):
        return self.store.run(run_id).get('graph_version') in (5, 6, 7)

    def full_flow(self, run_id):
        return self.store.run(run_id).get('graph_version') in (6, 7)

    def build_workflow(self, saver):
        graph = StateGraph(WorkflowState)
        nodes = ('dispatch','inputs','understand','clarification_gate','apply_answer',
                 'understanding_gate','scenarios','scenario_gate','cases','case_draft_gate','review','review_result_gate',
                 'conversation','single','summarize','publish')
        for name in nodes:
            boundary='boundary_'+name
            graph.add_node(boundary, self.workflow_boundary(name))
            graph.add_node(name, self.observed_node(name))
            graph.add_conditional_edges(boundary,lambda s:bool(s.get('boundary_again')),{True:boundary,False:name})
        def edge(source,target):graph.add_edge(source,'boundary_'+target)
        def branch(source,choose,targets):
            graph.add_conditional_edges(source,choose,{target:'boundary_'+target for target in targets})
        edge(START,'dispatch')
        branch('dispatch',lambda s:'conversation' if s['intent']=='query' else 'single' if s['intent'] in ('modify','learn_template') else 'inputs',('conversation','single','inputs'))
        branch('inputs',self.next_input,('scenario_gate','understanding_gate','review','cases','understand'))
        branch('understand',self.next_understanding,('clarification_gate','understanding_gate'))
        edge('clarification_gate','apply_answer')
        edge('apply_answer','understanding_gate')
        branch('understanding_gate',lambda s:'summarize' if s['intent']=='review_requirement' else 'scenarios',('summarize','scenarios'))
        branch('scenarios',lambda s:'scenario_gate' if s.get('scenario_ref') else 'scenarios',('scenario_gate','scenarios'))
        branch('scenario_gate',lambda s:'summarize' if s['intent']=='generate_scenario' else 'cases',('summarize','cases'))
        branch('cases',lambda s:'case_draft_gate' if s.get('cases_ref') else 'cases',('case_draft_gate','cases'))
        edge('case_draft_gate','review')
        edge('review','review_result_gate')
        for name in ('review_result_gate','conversation','single'):
            edge(name,'summarize')
        edge('summarize','publish')
        graph.add_edge('publish',END)
        return graph.compile(checkpointer=saver)

    def workflow_boundary(self,name):
        """A separate fixed node owns the only control interrupt (index zero).

        Natural clarification/confirmation nodes keep their original interrupt
        order. Existing v7 checkpoints still refer to unchanged business names.
        """
        async def boundary(state):
            rid=state['run_id'];run=self.store.run(rid);marker='boundary_'+name
            request=run.get('_boundary_requested')
            input_key={'scenarios':'analysis_ref','cases':'scenario_ref','review':'cases_ref'}.get(name)
            guarded_input=state.get(input_key) if input_key else None
            change_message=None
            if input_key:
                from .workspace_changes import assert_current_inputs
                try:
                    assert_current_inputs(self.store,{**run,'interrupt':{
                        'type':'workflow_paused','node':name,
                        **({'artifact_id':guarded_input} if guarded_input else {})}})
                except DomainError as exc:
                    if exc.status!=409:raise
                    change_message=str(exc)
                    request={'reason':'reconcile'}
                    self.store.update_run(rid,_boundary_requested=request)
            if not request and run.get('_boundary_node')!=marker:
                return {'boundary_again':False}
            if run.get('_boundary_node')!=marker:
                run=self.store.update_run(rid,_boundary_node=marker,
                    _boundary_reason=(request or {}).get('reason','write'))
            response=interrupt({'type':'workflow_paused','node':name,'reason':run.get('_boundary_reason','write'),
                **({'artifact_id':guarded_input} if guarded_input else {}),
                'message':change_message or '已在安全步骤边界暂停，已保存成果保留。'})
            current=self.store.run(rid)
            if current.get('_control_hold') and current.get('control_version',0)>response.get('_control_version',-1):
                # A hold accepted after scheduling must reach a fresh guard
                # invocation, not consume an earlier approval and enter work.
                self.store.update_run(rid,_boundary_node=None)
                return {'boundary_again':True}
            self.store.update_run(rid,_boundary_node=None,_boundary_requested=None)
            return {'boundary_again':False}
        return boundary

    def stage(self, run_id, stage):
        super().stage(run_id,stage)
        names={'routing':'识别本次任务','input_check':'检查资料用途','requirement_analysis':'理解需求与业务图',
            'applying_clarification':'采用澄清并更新需求理解','strategy_review':'等待确认理解与方案',
            'scenario_generation':'生成测试场景','case_generation':'生成测试用例','case_review':'评审并优化用例',
            'scenario_review':'等待确认测试场景','case_draft_review':'等待确认用例草稿','case_result_review':'等待确认评审结果',
            'learn_template':'读取 Excel 格式建议','modify':'修改选定结果','query':'回答你的问题','summarizing':'整理本轮总结'}
        if stage in names:self.store.update_run(run_id,progress={'phase':stage,'completed':0,'total':1,'label':names[stage]})

    def observed_node(self, name):
        observed=super().observed_node(name)
        async def wrapped(state):
            rid=state['run_id']
            try:
                result=await observed(state)
            except GraphInterrupt:raise
            except Exception as exc:
                self.store.update_run(rid,failed_node=name,validation_errors=getattr(exc,'errors',[]))
                raise
            if self.store.run(rid).get('graph_version')==7:
                refs={k:v for k,v in result.items() if k.endswith('_ref') and isinstance(v,str)}
                counts={k:len(self.store.get('artifact',v)['items']) for k,v in refs.items()}
                self.trace('workflow.step_saved',rid,step=name,artifact_refs=refs,item_counts=counts)
                self.publish_stage(rid,name,refs)
            return result
        return wrapped

    def publish_stage(self,rid,step,refs):
        run=self.store.run(rid)
        if run['mode']!='auto' or step not in ('understand','scenarios','cases','review') or not refs:return
        key='v7:stage_message:'+step
        with self.store.transaction():
            if self.store.cache_get(rid,key):return
            artifact=self.store.get('artifact',next(iter(refs.values())))
            if artifact['type'] not in ('analysis','scenarios','cases'):return
            artifact['_visible']=True;self.store.put('artifact',artifact)
            label={'understand':'需求理解与业务图','scenarios':'测试场景','cases':'用例草稿','review':'用例评审'}[step]
            report=artifact.get('report',{})
            narrative=report.get('summary') or '、'.join(str(i['title']) for i in artifact['items'][:3])
            if step=='review':
                narrative='\n'.join(str(r.get('summary','')) for r in (self.store.cache_get(rid,'v4:review_reports') or []))
            content=f'{label}已完成 · {len(artifact["items"])} 条。\n'+str(narrative)
            self.store.put('message',{'id':uid('msg_'),'project_id':run['project_id'],'chat_id':run['chat_id'],
                'role':'assistant','content':content,'created_at':now(),'metadata':{'run_id':rid,'stage':step,
                'stage_artifact_id':artifact['id'],'stage_revision':artifact['revision']}})
            self.store.cache_set(rid,key,{'done':True})
            self.store.update_run(rid)

    def small_context(self,run_id,**extra):
        context=super().small_context(run_id,**extra)
        run=self.store.run(run_id)
        if run.get('scope'):
            context['current_scope']=copy.deepcopy(run['scope'])
            context['scope_instruction']='本轮后续生成遵循 current_scope；它是用户指定的测试范围，不是新增业务事实或可引用原文。已保存且不受影响的阶段保持不变。'
        # Formatting examples are separate from business evidence and only sent to authoring/review.
        if self.store.run(run_id).get('graph_version')==7 and any(key in extra for key in ('analysis','scenarios','cases')):
            context['format_references']=[e for e in self.all_evidence(run_id) if e['role']=='example']
            samples=self.store.run(run_id)['_profile'].get('sample_cases',[])
            if samples:
                context['format_samples']=samples
                context['sample_instruction']='固定样例仅用于字段格式与步骤写法；其业务规则、数据与结果不能作为当前需求事实，也不能作为 refs。'
            if 'analysis' in extra and not any(key in extra for key in ('scenarios','cases')):
                context['format_instruction']='采用 profile.scenario_excel_columns 的场景字段定义与写作规范，填写有需求依据的自定义场景字段；保留原始 refs 和 requirement_ids 数组。format_references 仅补充格式定义，不作为业务事实或 refs；Excel 列顺序、标题与换行由导出器处理。'
            else:
                context['format_instruction']='采用当前 Profile 的字段定义、template_rules 和写作规范；format_references 仅补充格式定义，不作为业务事实或 refs。内部 steps 始终是 action/expected 对象数组；Excel 列顺序、标题与单元格换行由导出器处理。'
        return context

    async def node_cases(self,state):
        result=await super().node_cases(state)
        if result.get('cases_ref'):
            rid=state['run_id'];run=self.store.run(rid)
            artifact=self.store.get('artifact',result['cases_ref'])
            usage={'profile_name':self.store.get('profile',run['_profile_id'])['name'],
                   'columns':run['_profile'].get('excel_columns',[]),'field_contract':template_columns(run['_profile']),'rules':run['_profile'].get('template_rules',''),
                   'sheet_name':run['_profile'].get('sheet_name'),'layout':run['_profile'].get('excel_layout'),
                   'references':[{'id':sid,'name':self.store.get('source',sid)['name']} for sid in run['_source_ids'] if run.get('_source_roles',{}).get(sid)=='example'],
                   'note':'本轮已将上述定义及参考模板发送给生成与评审。导出采用本轮 Profile 快照；模板正文仅参考格式，上传文件不会自动覆盖列映射。'}
            self.save_report(artifact,{'template_usage':usage})
        return result

    def save_report(self,artifact,fields):
        # Reports are part of a consumed snapshot, so annotations get a new revision.
        current = self.store.get('artifact', artifact['id'])
        if all(current.get('report', {}).get(k) == v for k, v in fields.items()):
            return current
        saved = self.store.annotate_artifact(current['id'], current['revision'], {'report': {**current.get('report', {}), **fields}})
        artifact.update(saved)
        return saved

    async def prepare_result(self, rid, task, context, result):
        profile=context.get('profile',{})
        if task in ('generate_cases','import_cases') and isinstance(result.get('items'),list):
            rows=result['items']
            if all(isinstance(r,dict) for r in rows):rows=protect_non_ai_fields(rows,profile)
            return {**result,'items':await self.complete_template_fields(rid,rows,context)}
        if task in ('review_cases','modify') and template_columns(profile):
            from .schemas import apply_operations
            original=context.get('cases',[]) if task=='review_cases' else (context.get('artifact') or {}).get('items',[])
            try:revised=apply_operations(original,result.get('operations'),context.get('selected_ids'))
            except DomainError:return result
            selected=context.get('selected_ids')
            eligible=[r for r in revised if selected is None or r['id'] in selected or r['id'] not in {x['id'] for x in original}]
            completed=await self.complete_template_fields(rid,protect_non_ai_fields(eligible,profile,original),context)
            mapping={r['id']:r for r in completed}
            operations=copy.deepcopy(result['operations']);handled=set()
            for op in operations:
                target=op.get('id') or (op.get('item') or {}).get('id')
                if op.get('op') in ('add','update') and target in mapping:
                    op['item']=mapping[target];handled.add(target)
            for row in revised:
                updated=mapping.get(row['id'])
                if updated and updated!=row and row['id'] not in handled:
                    operations.append({'op':'update','id':row['id'],'item':updated})
            result={**result,'operations':operations}
        if task!='generate_scenarios' or not isinstance(result.get('items'),list):return result
        requirements={r['id'] for r in context.get('analysis',[])}
        rows=result['items']
        if not rows or not all(isinstance(r,dict) and isinstance(r.get('id'),str) for r in rows):return result
        def linked(row):
            refs=row.get('requirement_ids')
            return isinstance(refs,list) and bool(refs) and all(isinstance(ref,str) and ref in requirements for ref in refs)
        if all(linked(r) for r in rows):return result
        candidate_ids={r['id'] for r in rows}
        if len(candidate_ids)!=len(rows):return result
        link_context={'analysis':context['analysis'],'scenarios':rows,'evidence':context.get('evidence',[]),
            'instruction':'只补场景到需求的关联，使用输入中的精确 ID；保留场景正文，不重新生成场景。根据业务含义判断，不可仅因引用同一段原文就关联所有需求。'}
        def validate(value):
            links=value.get('links')
            if not isinstance(links,list):return [{'path':'links','code':'type','expected':'array'}]
            if len(links)!=len(rows) or any(not isinstance(link,dict) or not isinstance(link.get('id'),str) or not linked(link) for link in links) or {link['id'] for link in links}!=candidate_ids:
                return [{'path':'links','code':'trace_links','expected':{'scenario_ids':sorted(candidate_ids),'requirement_ids':sorted(requirements)}}]
            return []
        digest=hashlib.sha256(json.dumps(link_context,sort_keys=True,ensure_ascii=False).encode()).hexdigest()
        self.trace('scenarios.linking_started',rid,scenario_count=len(rows),requirement_count=len(requirements))
        links=await self.validated(rid,'v7:links:'+digest,'link_scenarios',link_context,validate)
        mapping={link['id']:link['requirement_ids'] for link in links['links']}
        self.trace('scenarios.linking_complete',rid,scenario_count=len(rows))
        return {**result,'items':[{**row,'requirement_ids':row['requirement_ids'] if linked(row) else mapping[row['id']]} for row in rows]}

    async def complete_template_fields(self,rid,rows,context,retry_unresolved=False):
        profile=context.get('profile',{})
        columns=template_columns(profile)
        if not columns:return rows
        from .schemas import validate_items
        try:validate_items('cases',rows,{e['id']:e for e in context.get('evidence',[])})
        except DomainError:
            if retry_unresolved:raise
            return rows  # Repair core case structure before any field completion.
        rows=materialize_fields(rows,profile)
        gaps=template_check(profile,rows)['missing']
        requested={}
        for gap in gaps:
            # A known missing business fact is not a formatting failure or another review pass.
            if retry_unresolved or not gap['reason']:requested.setdefault(gap['id'],[]).append(gap['field'])
        if not requested:return rows
        missing=[r for r in rows if r['id'] in requested]
        fields={f for values in requested.values() for f in values}
        definitions=list({c['field']:c for c in columns if c['field'] in fields}.values())
        refs={ref for r in missing for ref in r.get('refs',[])}
        evidence=[e for e in context.get('evidence',[]) if e['role']!='example' and e['id'] in refs]
        def build(group):
            ids={r['id'] for r in group};own_refs={ref for r in group for ref in r.get('refs',[])}
            needed={f for cid in ids for f in requested[cid]}
            return {'language':profile.get('language','中文'),'template_rules':profile.get('template_rules',''),
                    'columns':[c for c in definitions if c['field'] in needed],
                    'cases':[{k:v for k,v in r.items() if not k.startswith('_')} for r in group],
                    'missing_fields':{cid:requested[cid] for cid in requested if cid in ids},
                    'evidence':[e for e in evidence if e['id'] in own_refs]}
        replacements={}
        self.trace('case_fields.completion_started',rid,fields=sorted(fields),missing_count=len(missing),field_count=sum(map(len,requested.values())))
        for group in self.capacity_groups('complete_case_fields',missing,build):
            payload=build(group);wanted=payload['missing_fields']
            def validate(value):
                values=value.get('items')
                expected={'case_ids':list(wanted),'allowed_fields':wanted,'instruction':'每个缺失字段只能在 fields 或 unresolved 中出现一次；后者填写缺少什么业务依据。不得覆盖其他字段。'}
                bad=[{'path':'items','code':'template_fields','expected':expected}]
                if not isinstance(values,list) or len(values)!=len(wanted):return bad
                seen=set()
                for item in values:
                    if not isinstance(item,dict) or not isinstance(item.get('id'),str) or item['id'] not in wanted or item['id'] in seen:return bad
                    seen.add(item['id']);values_map=item.get('fields',{});unresolved=item.get('unresolved',{})
                    if not isinstance(values_map,dict) or not isinstance(unresolved,dict):return bad
                    if set(values_map)&set(unresolved) or set(values_map)|set(unresolved)!=set(wanted[item['id']]):return bad
                    if any(not filled(v) for v in values_map.values()):return bad
                    if any(not isinstance(v,str) or not v.strip() for v in unresolved.values()):return bad
                    for field,value in values_map.items():
                        if field in ('id','title','description','type','priority','scenario_id','preconditions') and not isinstance(value,str):return bad
                return []
            key='v7:template_fields:'+hashlib.sha256(json.dumps(payload,ensure_ascii=False,sort_keys=True).encode()).hexdigest()
            result=await self.validated(rid,key,'complete_case_fields',payload,validate)
            replacements.update({v['id']:v for v in result['items']})
        policies={c['field']:c for c in definitions}
        for row in rows:
            if row['id'] not in replacements:continue
            result=replacements[row['id']];row.update(result.get('fields',{}))
            notes=row.setdefault('_template_field_notes',{})
            for field in result.get('fields',{}):notes.pop(field,None)
            for field,reason in result.get('unresolved',{}).items():
                notes[field]={'reason':reason,'contract':column_signature(policies[field])}
            if not notes:row.pop('_template_field_notes',None)
        remaining=template_check(profile,rows)['missing']
        self.trace('case_fields.completion_complete',rid,completed_count=sum(len(v.get('fields',{})) for v in replacements.values()),unresolved_count=len(remaining))
        return rows

    def understanding_signature(self, source_ids, roles, profile=None, scope=None):
        from .context_service import analysis_signature
        return analysis_signature(self.store, source_ids, roles, profile or {}, scope)

    async def invoke_model(self, task, context, run_id=None):
        if run_id and self.store.run(run_id).get('graph_version')==7:
            # The stage contract, not the eventual user goal, owns this response.
            names={'analyze_requirement':'review_requirement','generate_scenarios':'generate_scenario','generate_cases':'generate_case','review_cases':'review_case'}
            if task in names:
                context={**context,'user_goal':context.get('request',{}),
                    'request':{**context.get('request',{}),'intent':names[task]},
                    'current_stage':task,'stage_instruction':'只执行当前阶段的输出契约。最终用户目标不表示现在就生成最终用例或回答。'}
            if task=='analyze_requirement':
                context['analysis_focus']={'guidance':context.get('profile',{}).get('additional_rules',''),
                    'instruction':'本阶段仅采用其中的业务范围与关注点；字段、编号和步骤写作规范留给用例阶段。'}
                context['profile']={k:v for k,v in context.get('profile',{}).items() if k in ('language','scope','scenario_level')}
                context['request']={**context['request'],'content':'提取需求中的业务规则、范围和歧义，绘制业务图。本阶段不编写测试用例。'}
                context.pop('format_references',None)
        if task in ('generate_cases','import_cases','review_cases','modify','direct_cases'):
            context={**context,'template_contract':template_columns(context.get('profile',{}))}
        if task=='complete_question_suggestions':
            # This optional completion has one request; its caller supplies the safe fallback.
            return await super().invoke_model(task,context,run_id)
        original=context
        hint_key='json_hint:'+hashlib.sha256(json.dumps(context,sort_keys=True,ensure_ascii=False).encode()).hexdigest()
        hint=self.store.cache_get(run_id,hint_key) if run_id else None
        if hint:context={**original,'json_repair':hint}
        for attempt in range(3):
            try:
                return await super().invoke_model(task,context,run_id)
            except DomainError as exc:
                if not hasattr(exc,'raw_response'):raise
                hint={'previous_response_text':exc.raw_response,'parse_error':exc.parse_error,
                    'instruction':'上次返回无法解析。保留业务内容，纠正 JSON 语法，只返回一个完整 JSON 对象；不要 Markdown。当前阶段不变，不重新分析需求。'}
                if run_id:
                    self.store.cache_set(run_id,hint_key,hint)
                    self.trace('json.repair_started' if attempt<2 else 'json.repair_exhausted',run_id,repair_attempt=attempt+1,parse_error=exc.parse_error,response_characters=len(exc.raw_response))
                if attempt==2:raise
                context={**original,'json_repair':hint}


    async def paused_dialogue(self, run_id, content):
        run=self.store.run(run_id)
        if run['status']!='waiting':raise DomainError('当前任务未等待确认',409)
        from .dialogue_context import answer_dialogue
        result=await answer_dialogue(self,run_id,content=content,pending=run.get('interrupt',{}))
        refs=result.get('refs',[])
        with self.store.transaction():
            for role,text in [('user',content),('assistant',result['answer'])]:
                self.store.put('message',{'id':uid('msg_'),'project_id':run['project_id'],'chat_id':run['chat_id'],'role':role,'content':text,'created_at':now(),'metadata':{'dialogue_run_id':run_id,'refs':refs if role=='assistant' else [],'coverage':result.get('coverage',{}) if role=='assistant' else {}}})
        return result

    async def node_dispatch(self,state):
        rid=state['run_id'];self.stage(rid,'routing')
        run=self.store.run(rid);intent=run['intent']
        if intent=='auto' and run['_request']['content'].strip() in ('验证','检查','看看'):
            intent='review_case' if (run.get('_artifact_snapshot') or {}).get('type')=='cases' else 'query'
        if intent=='auto':
            result=await self.validated(rid,'v7:route','route',self.routing_context(rid),
                lambda r: [] if r.get('intent') in INTENTS and r['intent']!='auto' else [{'path':'intent','code':'intent','expected':sorted(set(INTENTS)-{'auto'})}])
            intent=result['intent']
        snapshot=run.get('_artifact_snapshot')
        if intent in ('modify','review_case') and not run['_request'].get('_fresh_after_supplement'):
            wanted=('cases',) if intent=='review_case' else ('analysis','scenarios','cases')
            if not snapshot or snapshot['type'] not in wanted:
                available=[a for a in self.store.list('artifact',chat_id=run['chat_id']) if a.get('_visible') and a['type'] in wanted]
                snapshot=max(available,key=lambda a:a['created_at']) if available else None
        source_ids=list(dict.fromkeys(run['_source_ids']+(snapshot.get('_source_ids',[]) if snapshot and intent in ('query','modify','review_case') else [])))
        roles={sid:run.get('_source_roles',{}).get(sid,self.store.get('source',sid)['role']) for sid in source_ids}
        if snapshot and intent in ('modify','review_case','generate_case','generate_scenario','query'):
            roles.update(snapshot.get('_source_roles',{}))
        self.store.update_run(rid,intent=intent,_artifact_snapshot=snapshot,_source_ids=source_ids,_source_roles=roles)
        return {'intent':intent}

    async def node_inputs(self,state):
        rid=state['run_id'];self.stage(rid,'input_check');run=self.store.run(rid)
        evidence=self.all_evidence(rid)
        if not any(e['role']!='example' for e in evidence):
            sources=[self.store.get('source',sid) for sid in run['_source_ids']]
            response=interrupt({'type':'source_review','suggested_text':run['_request']['content'] if not sources else '', 'sources':[{'id':s['id'],'name':s['name'],'role':s['role']} for s in sources],
                'message':'这些文件目前仅作为格式示例。请选择作为本轮需求的文件，或补充需求正文。' if sources else '请先补充需求正文，再继续当前任务。'})
            selected=response.get('source_ids') or []
            if not set(selected).issubset(set(run['_source_ids'])):raise DomainError('选择的来源不属于本轮任务')
            roles={**run.get('_source_roles',{}),**{sid:'primary' for sid in selected}}
            ids=list(run['_source_ids'])
            if (response.get('answer') or '').strip():
                key='v7:input_source';saved=self.store.cache_get(rid,key)
                if not saved:
                    text,chunks=parse_text(response['answer'])
                    with self.store.transaction():
                        source=self.store.add_source(run['chat_id'],'本轮需求正文','primary',text,chunks)
                        saved=self.store.cache_set(rid,key,{'id':source['id']})
                ids=list(dict.fromkeys(ids+[saved['id']]))
                roles[saved['id']]='primary'
            if not selected and ids==run['_source_ids']:raise DomainError('请选择需求文件或输入需求正文')
            self.store.update_run(rid,_source_ids=ids,_source_roles=roles)
        snapshot=run.get('_artifact_snapshot')
        if snapshot and state['intent']=='generate_case' and snapshot['type']=='scenarios':
            return {'scenario_ref':snapshot['id'],'output_ref':snapshot['id']}
        if snapshot and state['intent']=='generate_scenario' and snapshot['type']=='analysis':
            return {'analysis_ref':snapshot['id'],'output_ref':snapshot['id']}
        return {}

    def next_input(self,state):
        if state.get('scenario_ref'):return 'scenario_gate'
        if state.get('analysis_ref'):return 'understanding_gate'
        if state['intent']=='review_case':
            return 'review' if self.store.run(state['run_id']).get('_artifact_snapshot') else 'cases'
        return 'understand'

    async def node_understand(self,state):
        rid=state['run_id'];run=self.store.run(rid)
        signature=self.understanding_signature(run['_source_ids'],run['_source_roles'],run['_profile'],run.get('scope'))
        reusable=[a for a in self.store.list('artifact',chat_id=run['chat_id']) if a['type']=='analysis'
            and a.get('report', {}).get('analysis_signature') == signature]
        if reusable:
            artifact=max(reusable,key=lambda a:(a['created_at'],a['revision']))
            self.trace('analysis.reused',rid,artifact_id=artifact['id'],revision=artifact['revision'],requirement_count=len(artifact['items']))
            self.store.cache_set(rid,'v6:requirement_map',artifact.get('report',{}))
        else:
            artifact=await self.analyze(rid,'v7:analysis')
            artifact=self.save_report(artifact, {'analysis_signature':signature})
        return {'analysis_ref':artifact['id'],'output_ref':artifact['id']}

    def next_understanding(self,state):
        run=self.store.run(state['run_id']);artifact=self.store.get('artifact',state['analysis_ref'])
        return 'clarification_gate' if run['mode']=='hitp' and artifact.get('report',{}).get('questions') else 'understanding_gate'

    async def node_clarification_gate(self,state):
        rid=state['run_id'];self.stage(rid,'clarification')
        artifact=self.store.get('artifact',state['analysis_ref']);questions=artifact['report']['questions']
        evidence={item['id']:item for item in self.all_evidence(rid)}
        suggestions=await self.ensure_question_suggestions(rid,
            f"{artifact['id']}:{artifact['revision']}:question_suggestions",artifact['report'],evidence)
        self.store.publish(rid,[artifact['id']],'已整理需求理解和业务图。请回答会影响测试设计的问题。',waiting=True)
        response=interrupt({'type':'clarification','artifact_id':artifact['id'],'questions':questions,'question_suggestions':suggestions})
        return {'clarification_answer':response['answer'],'clarification_questions':questions}

    async def node_apply_answer(self,state):
        rid=state['run_id'];run=self.store.run(rid);self.stage(rid,'applying_clarification')
        # Questions travel with answers so terse replies retain their meaning.
        answer='问题：\n'+'\n'.join(state['clarification_questions'])+'\n用户回答：\n'+state['clarification_answer']
        saved=self.store.cache_get(rid,'v7:clarification_source')
        if not saved:
            from .clarification import _stored
            draft = _stored(self.store, rid)
            with self.store.transaction():
                if draft and draft.get('submitted') and draft.get('source_id'):
                    source = self.store.get('source', draft['source_id'])
                else:
                    text,chunks=parse_text(answer)
                    source=self.store.add_source(run['chat_id'],'用户澄清','clarification',text,chunks)
                saved=self.store.cache_set(rid,'v7:clarification_source',{'id':source['id']})
        if run.get('_save_clarification_to_project',True):
            from .project_context import share_clarification
            share_clarification(self.store,saved['id'],run['project_id'])
        self.store.update_run(rid,_source_ids=list(dict.fromkeys(run['_source_ids']+[saved['id']])),_source_roles={**run['_source_roles'],saved['id']:'clarification'})
        artifact=self.store.get('artifact',state['analysis_ref'])
        from .requirement_refresh import refresh_clarification
        artifact=await refresh_clarification(self,rid,artifact,saved['id'],answer)
        self.trace('clarification.applied',rid,artifact_id=artifact['id'],revision=artifact['revision'],source_id=saved['id'])
        return {'analysis_ref':artifact['id'],'output_ref':artifact['id']}

    async def node_understanding_gate(self,state):
        rid=state['run_id'];run=self.store.run(rid)
        if run['mode']=='hitp' or run.get('stop_after')=='analysis':
            self.stage(rid,'strategy_review')
            artifact=self.store.get('artifact',state['analysis_ref'])
            self.store.publish(rid,[artifact['id']],'请确认需求理解、业务图和测试方案。剩余不确定项可在此修订，不会自动反复追问。',waiting=True)
            response=interrupt({'type':'strategy_review','artifact_id':artifact['id'],
                'title':'确认需求理解','message':'检查需求、业务图和测试方案。可通过聊天提问、修订或补充资料；明确确认后才生成场景。',
                'confirm_label':'确认需求理解，生成场景','next_stage':'scenarios',
                **({'stop_after':'analysis'} if run.get('stop_after')=='analysis' else {}),
                'recommended_depth':artifact.get('report',{}).get('strategy',{}).get('depth','standard')})
            if response.get('approved') is not True:raise DomainError('请确认理解与方案')
            if response.get('depth'):
                self.store.update_run(rid,_profile=profile_config({**run['_profile'],'scenario_level':response['depth'],'case_level':response['depth']}))
        artifact=self.store.get('artifact',state['analysis_ref'])
        self.store.cache_set(rid,'v6:requirement_map',{**artifact.get('report',{}),'confirmed_requirements':artifact['items']})
        self.store.cache_set(rid,'workspace:analysis_parent',{'id':artifact['id'],'revision':artifact['revision']})
        return {'output_ref':artifact['id']}

    async def node_scenario_gate(self,state):
        rid=state['run_id'];run=self.store.run(rid)
        if run.get('stop_after') == 'scenarios':
            self.stage(rid,'scenario_review')
            artifact=self.store.get('artifact',state['scenario_ref'])
            self.store.publish(rid,[artifact['id']],'已生成场景并达到本任务停止位置。明确允许生成用例后再继续。',waiting=True)
            response=interrupt({'type':'scenario_review','artifact_id':artifact['id'],'items':artifact['items'],
                'title':'确认测试场景','confirm_label':'确认场景，生成用例草稿','next_stage':'cases',
                'stop_after':'scenarios','message':'本任务暂时停止在场景，请明确允许生成用例后继续。'})
            if response.get('approved') is not True:
                raise DomainError('请确认场景后继续')
            result={}
        else:
            result=await super().node_scenario_gate(state)
        artifact=self.store.get('artifact',state['scenario_ref'])
        self.store.cache_set(state['run_id'],'workspace:scenario_parent',{'id':artifact['id'],'revision':artifact['revision']})
        return result

    async def node_review(self,state):
        rid=state['run_id'];run=self.store.run(rid)
        if run.get('pause_contract',1)<2 and run.get('graph_version')==7 and run.get('stop_after')=='cases' and state.get('cases_ref'):
            artifact=self.store.get('artifact',state['cases_ref'])
            self.store.publish(rid,[artifact['id']],'用例已生成并达到本任务停止位置。明确允许评审后再继续。',waiting=True)
            interrupt({'type':'workflow_paused','node':'review','reason':'stop_after',
                'artifact_id':artifact['id'],'message':'用例已保存，等待明确允许评审。'})
        if self.store.run(state['run_id']).get('graph_version')==7 and not state.get('cases_ref'):
            snapshot=self.store.run(state['run_id']).get('_artifact_snapshot')
            state={**state,'cases_ref':snapshot['id']}
        return await super().node_review(state)

    async def node_case_draft_gate(self,state):
        rid=state['run_id'];run=self.store.run(rid)
        if run.get('pause_contract',1)<2 or not state.get('cases_ref'):
            return {}
        artifact=self.store.get('artifact',state['cases_ref'])
        if run['mode']=='hitp' or run.get('stop_after')=='cases':
            self.stage(rid,'case_draft_review')
            message='请检查用例草稿的步骤、预期和覆盖。可通过聊天微调或联动修改；确认后才开始 AI 评审。'
            self.store.publish(rid,[artifact['id']],message,waiting=True)
            response=interrupt({'type':'case_draft_review','artifact_id':artifact['id'],
                'items':artifact['items'],'title':'确认用例草稿','message':message,
                'confirm_label':'确认用例草稿，开始评审','next_stage':'review',
                **({'stop_after':'cases'} if run.get('stop_after')=='cases' else {})})
            if response.get('approved') is not True:
                raise DomainError('请确认当前用例草稿后再评审')
        return {'cases_ref':artifact['id'],'output_ref':artifact['id']}

    async def node_review_result_gate(self,state):
        rid=state['run_id'];run=self.store.run(rid)
        # This node replays on confirmation. Publish the original review once;
        # edits made while waiting are part of the version being approved.
        with self.store.transaction():
            artifact=self.store.get('artifact',state['output_ref'])
            if not self.store.cache_get(rid,'v7:review_report_attached'):
                reports=self.store.cache_get(rid,'v4:review_reports') or []
                if reports:
                    artifact=self.save_report(artifact,{'review_reports':reports})
                self.store.cache_set(rid,'v7:review_report_attached',
                    {'artifact_id':artifact['id'],'revision':artifact['revision']})
        if run.get('pause_contract',1)>=2 and run['mode']=='hitp' and artifact['type']=='cases':
            self.stage(rid,'case_result_review')
            message='请检查评审结论和当前用例。可以继续提问、微调、查看覆盖或保存格式样例；确认后完成本轮任务。'
            self.store.publish(rid,[artifact['id']],message,waiting=True)
            response=interrupt({'type':'case_result_review','artifact_id':artifact['id'],
                'items':artifact['items'],'title':'确认评审结果','message':message,
                'confirm_label':'确认评审结果，完成任务','next_stage':'publish'})
            if response.get('approved') is not True:
                raise DomainError('请确认当前评审结果后完成任务')
        return {'output_ref':artifact['id']}

    async def node_conversation(self,state):
        from .dialogue_context import answer_dialogue
        rid=state['run_id'];self.stage(rid,'query')
        result=await answer_dialogue(self,rid)
        artifact=self.answer(rid,'v7:answer',result['answer'],result.get('refs',[]))
        return {'output_ref':artifact['id']}

    async def node_single(self,state):
        rid=state['run_id'];run=self.store.run(rid)
        if run['_request'].get('complete_fields_only') or run['_request'].get('complete_descriptions_only'):
            from .generation_guards import commit_arguments
            self.stage(rid,'modify')
            artifact=run['_artifact_snapshot'];selected=set(run['_request']['selected_ids'])
            context=self.context(rid)
            rows=await self.complete_template_fields(rid,[r for r in artifact['items'] if r['id'] in selected],context,retry_unresolved=True)
            mapping={r['id']:r for r in rows}
            updated=self.store.revise_artifact(artifact['id'],artifact['revision'],[mapping.get(r['id'],r) for r in artifact['items']],
                'complete_template_fields',rid,'v7:template_fields_applied',
                **commit_arguments(self.store,rid,'cases'))
            return {'output_ref':updated['id']}
        if run.get('graph_version')==7 and state['intent']=='learn_template':
            self.stage(rid,'learn_template')
            context=self.context(rid)
            if not context['evidence'] and not context.get('artifact'):
                artifact=self.answer(rid,'v7:no_template','请上传 Excel 示例或选择当前场景、用例后再学习格式。')
                return {'output_ref':artifact['id']}
            def validate(result):
                try:
                    if not isinstance(result.get('config'),dict):raise DomainError('config 必须为对象')
                    kinds=self.template_kinds(result)
                    self.template_config(run['_profile'],result['config'],kinds)
                    return []
                except DomainError as exc:
                    return [{'path':'config','code':'profile','expected':str(exc)}]
            result=await self.validated(rid,'v7:template','learn_template',context,validate)
            kinds=self.template_kinds(result)
            config,notes=self.template_config(run['_profile'],result['config'],kinds)
            summary=str(result.get('summary','已整理模板建议'))
            if notes:summary+='\n'+ '；'.join(notes)
            artifact=self.store.artifact(rid,'v7:template_proposal','proposal','Excel 格式建议',
                [{'id':'template-proposal','title':'模板学习结果','description':summary,'refs':[]}],{'config':config,'notes':notes,'template_kinds':kinds})
            return {'output_ref':artifact['id']}
        if run.get('graph_version')==7 and state['intent']=='modify' and not run.get('_artifact_snapshot'):
            artifact=self.answer(rid,'v7:no_target','当前还没有可修改的结果。请先生成或上传用例。')
            return {'output_ref':artifact['id']}
        if run.get('graph_version')==7 and state['intent']=='modify' and run['_artifact_snapshot']['type']=='cases':
            from .schemas import apply_operations, validate_items
            self.stage(rid,'modify');snapshot=run['_artifact_snapshot'];context=self.context(rid)
            if not run['_request'].get('profile_override') and not run['_request'].get('profile_id'):
                context['profile']=snapshot.get('_profile',context['profile'])
            evidence={e['id']:e for e in context['evidence']}
            def validate(result):
                try:
                    rows=apply_operations(snapshot['items'],result.get('operations'),context.get('selected_ids'))
                    validate_items('cases',rows,evidence)
                    return []
                except DomainError as exc:return [getattr(exc,'issue',{'path':'operations','code':'case_edit','expected':str(exc)})]
            result=await self.validated(rid,'v7:case_edit','modify',context,validate)
            rows=apply_operations(snapshot['items'],result['operations'],context.get('selected_ids'))
            from .generation_guards import commit_arguments
            artifact=self.store.revise_artifact(snapshot['id'],snapshot['revision'],rows,'ai_modify',rid,'v7:case_edit_applied',
                **commit_arguments(self.store,rid,'cases'))
            return {'output_ref':artifact['id']}
        return await super().node_single(state)

    def template_kinds(self,result):
        kinds=result.get('template_kinds')
        if kinds is None:
            config=result.get('config',{})
            if not isinstance(config,dict):raise DomainError('config 必须为对象')
            kinds=[]
            if any(key in config for key in ('scenario_excel_columns','scenario_sheet_name','scenario_filename_pattern')):kinds.append('scenarios')
            if any(key in config for key in ('excel_columns','excel_layout','sheet_name','filename_pattern','template_rules','case_level','case_types','additional_rules')):kinds.append('cases')
        if not isinstance(kinds,list) or not kinds or any(kind not in ('scenarios','cases') for kind in kinds):
            raise DomainError('template_kinds 必须包含 scenarios 或 cases')
        return list(dict.fromkeys(kinds))

    def template_config(self, current, proposal, kinds=None):
        from .schemas import DEFAULT_PROFILE
        if not isinstance(proposal,dict):raise DomainError('config 必须为对象')
        kinds=self.template_kinds({'config':proposal} if kinds is None else {'template_kinds':kinds})
        config=dict(current);notes=[]
        scenario_keys={'scenario_excel_columns','scenario_sheet_name','scenario_filename_pattern'}
        case_keys={'excel_columns','excel_layout','sheet_name','filename_pattern','template_rules','case_level','case_types','additional_rules'}
        allowed=(scenario_keys if 'scenarios' in kinds else set()) | (case_keys if 'cases' in kinds else set())
        for key,value in proposal.items():
            if key not in allowed:continue
            if value is None or value==[] or (isinstance(value,str) and not value.strip()):
                notes.append(f'{key} 未识别到有效值，保留当前设置')
                continue
            columns_key='scenario_excel_columns' if key in scenario_keys else 'excel_columns'
            if proposal.get(columns_key)==[] and key in DEFAULT_PROFILE and value==DEFAULT_PROFILE[key]:
                notes.append(f'{key} 仅为默认值，保留当前设置')
                continue
            if key=='template_rules' and isinstance(value,list):value='\n'.join(str(item) for item in value)
            config[key]=value
        return profile_config(config),notes

    async def node_summarize(self,state):
        rid=state['run_id'];self.stage(rid,'summarizing');artifact=self.store.get('artifact',state['output_ref'])
        request=self.store.run(rid)['_request']
        if request.get('complete_fields_only') or request.get('complete_descriptions_only'):
            rows=[r for r in artifact['items'] if r['id'] in request['selected_ids']]
            missing=template_check(self.store.run(rid)['_profile'],rows)['missing']
            note=f'仍有 {len(missing)} 项缺少依据，请在导出窗口查看原因并补充。' if missing else '可以按所选模板导出。'
            return {'summary':f'已检查 {len(rows)} 条用例的模板字段，保留原用例与已有内容。'+note}
        if artifact['type'] in ('answer','proposal'):
            return {'summary':'\n'.join(i.get('description','') for i in artifact['items'])}
        reports=artifact.get('report',{}).get('review_reports',[])
        context={'intent':state['intent'],'artifact_type':artifact['type'],'count':len(artifact['items']),
                 'items':[{k:i.get(k) for k in ('id','title','priority')} for i in artifact['items']],
                 'report':artifact.get('report',{}),'review_reports':reports,
                 'instruction':'总结已保存产物的业务内容、尚存风险和下一步。只谈实际完成的阶段，不宣称测试执行通过。'}
        try:
            result=await self.validated(rid,'v7:summary','summarize',context,
                lambda r: [] if isinstance(r.get('summary'),str) and r['summary'].strip() else [{'path':'summary','code':'text','expected':'nonempty string'}])
            narrative=result['summary']
        except DomainError:
            # A presentation failure never discards already validated business results.
            narrative='AI 总结暂不可用，已保存的结果可继续查看、修改和导出。'
        labels={'analysis':'需求分析','scenarios':'测试场景','cases':'测试用例'}
        missing=template_check(self.store.run(rid)['_profile'],artifact['items'])['missing'] if artifact['type']=='cases' else []
        if missing:narrative+=f'\n模板中有 {len(missing)} 项字段缺少业务依据，导出窗口可查看具体原因并补充。'
        title=f'已保存{labels.get(artifact["type"],artifact["type"])}，共 {len(artifact["items"])} 条。'
        if artifact['type']=='cases':title+='已完成一轮 AI 评审。用例尚未实际执行。' if reports else '用例尚未实际执行。'
        self.store.cache_set(rid,'v7:summary',title+'\n'+narrative)
        return {'summary':title+'\n'+narrative}

    async def node_publish(self,state):
        rid=state['run_id'];artifact=self.store.get('artifact',state['output_ref'])
        content=state.get('summary') or self.store.cache_get(rid,'v7:summary') or '已保存当前结果。'
        reports=self.store.cache_get(rid,'v4:review_reports') or []
        if (reports and 'review_reports' not in artifact.get('report',{})
                and not self.store.cache_get(rid,'v7:review_report_attached')):
            artifact = self.save_report(artifact, {'review_reports': reports})
        proposal=None
        if artifact['type']=='proposal':
            report=artifact.get('report',{})
            proposal={'config':report.get('config'),'template_kinds':report['template_kinds']} if 'template_kinds' in report else report.get('config')
        self.store.publish(rid,[artifact['id']],content,proposal=proposal)
        self.store.update_run(rid,progress={'phase':'completed','completed':1,'total':1,'label':'结果已保存'})
        return {}

    async def node_finish(self,state):
        if self.store.run(state['run_id']).get('graph_version')==7:
            if self.store.run(state['run_id'])['status']!='completed':return await self.node_publish(state)
            return {}
        return await super().node_finish(state)
