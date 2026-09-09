"""Small durable work items; the model requests evidence only when it needs it.

The graph holds cursors, not transcripts/documents. Accepted work is immutable,
so retry resumes the current item and never replays a completed model request.
"""
import copy
import json
from itertools import count
from typing import TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

from .agent import Agent, StaleInstruction, IncompleteCoverage
from . import agent_contracts as contract
from .incremental_contracts import analysis_patch, generated_patch, operation_patch, stable_id
from .incremental_workspace import Workspace, fingerprint
from .document_workspace import DocumentWorkspace
from .schemas import DomainError, OutputValidationError, INTENTS, profile_config, validate_items, apply_operations
from .storage import public


class WorkState(TypedDict, total=False):
    run_id: str
    epoch: int
    intent: str
    key: str
    action: str
    stale: bool
    done: bool


def size(value):
    return len(json.dumps(value, ensure_ascii=False))


def evidence_view(items, text=True):
    fields = ('id', 'source_id', 'role', 'location', 'text', 'excerpt') if text else ('id', 'source_id', 'role', 'location', 'excerpt')
    return [{k: e[k] for k in fields if k in e} for e in items]


def groups(items, budget=4500):
    result, group = [], []
    for item in items:
        if size([item]) > budget:
            raise DomainError(f'单条工作内容超过 {budget} 字符；请将该规则或场景拆成更小条目后重试，已完成工作保留。')
        if group and size(group + [item]) > budget:
            result.append(group)
            group = []
        group.append(item)
    if group:
        result.append(group)
    return result


class IncrementalAgent(Agent):
    supported_graph_version = 3
    CONTEXT_BUDGET = 16000
    EVIDENCE_BUDGET = 3500

    def __init__(self, engine, saver):
        self.engine, self.store = engine, engine.store
        self.workspace = Workspace(self.store)
        self.documents = DocumentWorkspace(self.store)
        builder = StateGraph(WorkState)
        for name in ('plan', 'work', 'gate', 'finish'):
            builder.add_node(name, self.observed(name))
        builder.add_edge(START, 'plan')
        builder.add_conditional_edges('plan', lambda s: 'plan' if s.get('stale') else s['action'])
        builder.add_edge('work', 'plan')
        builder.add_edge('gate', 'plan')
        builder.add_conditional_edges('finish', lambda s: END if s.get('done') else 'plan')
        self.graph = builder.compile(checkpointer=saver)

    def sync(self, run_id):
        work = self.workspace.view(run_id)
        all_items = work['items']
        # The inventory endpoint is paginated; SSE carries a bounded recent view.
        work['items'] = all_items[-20:]
        work['total'] = max(work['total'], self.store.run(run_id).get('_work_estimate', 0))
        self.update(run_id, work=work, preview_ids=[i['artifact_id'] for i in all_items if i.get('artifact_id') and i['status']=='completed'][-20:])

    def manifest(self, run_id):
        return self.store.cache_get(run_id, 'v3:manifest')

    def save_manifest(self, run_id, manifest):
        self.store.cache_set(run_id, 'v3:manifest', manifest)

    def result(self, run_id, key):
        record = self.workspace.get(run_id, key)
        return record['_result'] if record and record['status'] == 'completed' else None

    def job(self, run_id, key, task, title, data, unit=None):
        if self.result(run_id, key) is not None:
            return False
        self.store.cache_set(run_id, 'v3:job:' + key, {'key': key, 'task': task, 'title': title, 'data': data, 'unit': unit})
        return True

    async def plan(self, state):
        run = self.store.run(state['run_id'])
        state = {**state, 'epoch': run.get('_instruction_version', 0), 'stale': False}
        run_id = run['id']
        if run.get('_pause_requested'):
            return {**state, 'action': 'gate', 'key': 'pause'}
        manifest = self.manifest(run_id)
        if manifest is None:
            intent = run['intent']
            classification=None
            if intent == 'auto':
                route = await self.engine.call(run_id, 'v3:route', 'work_route', {
                    'goal': self.engine.routing_context(run_id)['request']['content'],
                    'requested_depth': run['_request'].get('depth', 'auto'),
                    'sources': self.documents.catalog(run['_source_ids']),
                    'artifact': {'type': run['_artifact_snapshot']['type']} if run.get('_artifact_snapshot') else None})
                self.current(state)
                contract.require(route.get('intent') in INTENTS and route['intent'] != 'auto', 'intent', 'supported_intent')
                intent = route['intent']
                classification=route.get('classification')
                chosen = run['_request'].get('depth')
                if chosen == 'auto' and route.get('depth') in contract.DEPTH_GUIDANCE:
                    self.update(run_id, depth=route['depth'])
            self.store.update_run(run_id, intent=intent)
            if intent in ('review_requirement','generate_scenario','generate_case') and not any(
                    e['role']!='example' for e in self.documents.evidence(run['_source_ids'])):
                if classification is None:
                    intake=await self.engine.call(run_id,'v3:intake','work_intake',{
                        'goal':self.engine.routing_context(run_id)['request']['content']})
                    self.current(state)
                    classification=intake.get('classification')
                contract.require(classification in ('requirement','instruction','ambiguous'),'classification','requirement|instruction|ambiguous')
                if classification=='requirement':
                    from .documents import parse_text
                    with self.store.transaction():
                        self.current(state)
                        text,chunks=parse_text(run['_request']['content'])
                        source=self.store.add_source(run['chat_id'],'当前消息中的业务规则','primary',text,chunks)
                        self.store.update_run(run_id,_source_ids=run['_source_ids']+[source['id']])
                        self.insight(run_id,'已将消息中的业务规则原样保存为需求来源，可点开核查。',[],'decision')
            if intent in ('query','modify','review_case') and run.get('_artifact_snapshot'):
                historical=run.get('_artifact_source_ids',[])
                for source_id in historical:
                    if self.store.get('source',source_id)['project_id']!=run['project_id']:
                        raise DomainError('历史成果引用超出当前项目范围',403)
                self.store.update_run(run_id,_source_ids=list(dict.fromkeys(run['_source_ids']+historical)),_historical_source_ids=historical)
            run = self.store.run(run_id)
            manifest = {'intent': intent, 'units': self.workspace.units(run, self.EVIDENCE_BUDGET),
                        'base_sources': list(run['_source_ids']), 'epoch': state['epoch'], 'overrides': {}, 'approved': False}
            self.save_manifest(run_id, manifest)
            self.store.update_run(run_id, _work_estimate=len(manifest['units']) * (4 if intent == 'generate_case' else 2))
            self.insight(run_id, '按小组提取规则并保存阶段成果；只在需要补充资料时查阅原文。', [], 'decision')
        state['intent'] = manifest['intent']
        if manifest['epoch'] != state['epoch']:
            await self.apply_instructions(state, manifest)
            run = self.current(state)
        if state['intent'] in ('query', 'modify', 'review_case', 'learn_template'):
            return await self.single_plan(state, manifest)
        if not manifest['units']:
            return {**state,'action':'gate','key':'input'}
        # Analysis first only when the user explicitly requested a confirmation.
        hitp = run['mode'] == 'hitp' and run['_request'].get('confirm_strategy', False) and not manifest['approved'] and not self.continuation(run)
        for unit in manifest['units']:
            version = fingerprint(manifest['overrides'].get(unit['id'], []))[:12]
            key = f'{unit["id"]}:{version}:analysis'
            evidence = unit['evidence'] + manifest['overrides'].get(unit['id'], [])
            if self.job(run_id, key, 'work_analyze', '分析 · ' + unit['title'], {'evidence': evidence_view(evidence), 'automatic_depth': run['_request'].get('depth')=='auto'}, unit['id']):
                return {**state, 'key': key, 'action': 'work'}
            if hitp or state['intent'] == 'review_requirement':
                continue
            analysis = self.result(run_id, key)
            if not analysis['items']:
                continue
            choice = self.design_job(run_id, unit, key, analysis, state['intent'])
            if choice:
                return {**state, 'key': choice, 'action': 'work'}
            choice = self.coverage_job(run_id, unit, key, analysis, state['intent'])
            if choice:
                return {**state, 'key': choice, 'action': 'work'}
        dependency = self.dependency_job(run_id, manifest)
        if dependency:
            return {**state, 'key':dependency, 'action':'work'}
        if hitp and state['intent'] != 'review_requirement':
            return {**state, 'action': 'gate', 'key': 'strategy'}
        # Exactly one logical review phase; each bounded case group is accepted once.
        if state['intent'] == 'generate_case':
            for unit in manifest['units']:
                version = fingerprint(manifest['overrides'].get(unit['id'], []))[:12]
                prefix = f'{unit["id"]}:{version}:analysis'
                analysis = self.result(run_id, prefix)
                for batch_index, data in enumerate(self.review_groups(run_id, prefix, analysis)):
                    key = prefix + f':review:{batch_index}'
                    if self.job(run_id, key, 'work_review', '评审优化 · ' + unit['title'], data, unit['id']):
                        return {**state, 'key': key, 'action': 'work'}
        return {**state, 'action': 'finish'}

    def dependency_job(self, run_id, manifest):
        if len(manifest['units']) < 2:
            return None
        if 'dependency_jobs' not in manifest:
            summaries=[]
            for unit in manifest['units']:
                if unit.get('derived'):
                    continue
                prefix=f'{unit["id"]}:{fingerprint(manifest["overrides"].get(unit["id"], []))[:12]}:analysis'
                accepted=self.result(run_id,prefix)
                summaries.append({'id':unit['id'],'title':unit['title'],'summary':accepted['report']['summary'],
                    'requirements':[{k:i[k] for k in ('id','title','refs')} for i in accepted['items']],
                    'nodes':accepted['report']['business_model']['nodes'],
                    'excerpts':evidence_view(unit['evidence']+manifest['overrides'].get(unit['id'],[]),False)})
            manifest['dependency_jobs']=[]
            for index,batch in enumerate(groups(summaries,8000)):
                key=f'v3:dependencies:{manifest["epoch"]}:{fingerprint(batch)[:16]}:{index}'
                refs={r for u in batch for i in u['requirements'] for r in i['refs']}
                data={'units':batch,'unit_count':len(summaries),
                      'evidence':evidence_view([e for e in self.authorized_evidence(self.store.run(run_id)).values() if e['id'] in refs],False)}
                self.store.cache_set(run_id,'v3:job:'+key,{'key':key,'task':'work_links','title':'核查跨组依赖','data':data,'unit':None})
                manifest['dependency_jobs'].append(key)
            self.save_manifest(run_id,manifest)
        return next((key for key in manifest['dependency_jobs'] if self.result(run_id,key) is None),None)

    def supersede(self, run_id, unit_id, previous_overrides):
        prefix=f'{unit_id}:{fingerprint(previous_overrides)[:12]}:analysis'
        for record in self.workspace.list(run_id):
            if record['key']==prefix or record['key'].startswith(prefix+':'):
                private=self.workspace.get(run_id,record['key'])
                private['_superseded']=True
                self.store.put('agent_work',private)
        # Accepted results themselves are immutable; only their current scope
        # membership changes. Full history remains inspectable.

    def accept_dependencies(self, state, result, available):
        manifest=self.manifest(state['run_id'])
        by_id={u['id']:u for u in manifest['units']}
        invalidated=False
        for change in result.get('updates',[]):
            unit_id=change['unit_id']
            additions=change['_evidence']
            # Only new source content changes the input fingerprint. No-op joins
            # cannot send a unit into an invalidation loop.
            old=manifest['overrides'].get(unit_id,[])
            existing={fingerprint(e) for e in by_id[unit_id]['evidence']+old}
            new=old+[e for e in additions if fingerprint(e) not in existing]
            if fingerprint(old)!=fingerprint(new):
                invalidated=True
                self.supersede(state['run_id'],unit_id,old)
                manifest['overrides'][unit_id]=new
                self.insight(state['run_id'],'后读规则影响已有草稿：'+change['summary']+'；重新设计受影响的组。',change['evidence_refs'],'decision')
        if invalidated:
            # These edges were proposed against the old graph. Recheck them
            # after the updated facts/nodes are accepted; never publish stale nodes.
            for unit in manifest['units']:
                if unit.get('derived'):
                    self.supersede(state['run_id'],unit['id'],manifest['overrides'].get(unit['id'],[]))
            manifest['units']=[u for u in manifest['units'] if not u.get('derived')]
            manifest.pop('dependency_jobs',None)
            self.save_manifest(state['run_id'],manifest)
            return
        if result.get('edges'):
            # A real cross-module path is its own covered work unit. It must
            # receive scenarios, cases and review just like any business rule.
            refs=list(dict.fromkeys(r for e in result['edges'] for r in e['refs']))
            basis=[]
            for unit in manifest['units']:
                current_refs={e['id'] for e in unit['evidence']+manifest['overrides'].get(unit['id'],[])}
                if unit.get('derived') or not current_refs.intersection(refs):
                    continue
                prefix=f'{unit["id"]}:{fingerprint(manifest["overrides"].get(unit["id"],[]))[:12]}:analysis'
                accepted=self.result(state['run_id'],prefix)
                if accepted:
                    basis.append({'items':accepted['items'],'business_model':accepted['report']['business_model']})
            # Identical paths reuse work; changed underlying rules produce a new
            # revision even when the edge label and paragraph IDs are unchanged.
            unit_id='unit_link_'+fingerprint([sorted(e['id'] for e in result['edges']),basis])[:20]
            evidence=evidence_view([available[r] for r in refs],False)
            nodes=result['nodes']
            items=[{'id':stable_id('R',unit_id,e['id']),'title':e['label'],'description':e['label'],'refs':e['refs']} for e in result['edges']]
            report={'summary':result['summary'],'questions':result.get('questions',[]),'assumptions':[],
                    'business_model':{'nodes':nodes,'edges':result['edges']},'strategy':{'depth':self.current(state)['agent']['depth'],
                    'rationale':'依据已引用的跨组规则','techniques':['业务流程与状态转换'],'scope':[i['title'] for i in items]},'diagrams':[]}
            unit={'id':unit_id,'title':'跨组业务路径','evidence':evidence,'refs':refs,'source_ids':list({e['source_id'] for e in evidence}),'derived':True}
            if unit_id not in by_id:
                manifest['units'].append(unit)
                key=unit_id+':'+fingerprint([])[:12]+':analysis'
                self.store.cache_set(state['run_id'],'v3:job:'+key,{'key':key,'task':'work_analyze','title':unit['title'],'data':{'evidence':evidence},'unit':unit_id})
                self.workspace.begin(state['run_id'],key,'work_analyze',unit['title'],refs)
                accepted={'items':items,'report':report}
                artifact=self.preview(state['run_id'],key,'work_analyze',accepted)
                self.workspace.accept(state['run_id'],key,accepted,artifact['id'])
                for record in self.workspace.list(state['run_id']):
                    if record['key']==key or record['key'].startswith(key+':'):
                        private=self.workspace.get(state['run_id'],record['key'])
                        private.pop('_superseded',None)
                        self.store.put('agent_work',private)
        self.save_manifest(state['run_id'],manifest)

    def generation_data(self, analysis, requirements=None, scenarios=None):
        requirements = analysis['items'] if requirements is None else requirements
        refs = {r for i in requirements for r in i['refs']}
        model = analysis['report']['business_model']
        edges = [e for e in model['edges'] if refs.intersection(e['refs'])]
        nodes = [n for n in model['nodes'] if refs.intersection(n['refs']) or n['id'] in {p for e in edges for p in (e['from'],e['to'])}]
        result = {'analysis': requirements, 'business_model': {'nodes': nodes, 'edges': edges},
                  'assumptions': analysis['report'].get('assumptions', []), 'questions': analysis['report'].get('questions', []),
                  'depth': analysis['report']['strategy']['depth'], 'techniques':analysis['report']['strategy']['techniques']}
        if scenarios is not None:
            result['scenarios'] = scenarios
        return result

    def design_job(self, run_id, unit, prefix, analysis, intent):
        for index, requirements in enumerate(groups(analysis['items'], 4500)):
            data = self.generation_data(analysis, requirements)
            for page in count():
                key = prefix + f':scenarios:{index}:{page}'
                previous = self.pages(run_id, prefix + f':scenarios:{index}:', page)
                context = {**data, 'cursor': previous[-1].get('next_cursor') if previous else None,
                           'previous_items': self.item_index([i for p in previous for i in p['items']])}
                if self.job(run_id, key, 'work_scenarios', '设计场景 · ' + unit['title'], context, unit['id']):
                    return key
                result = self.result(run_id, key)
                if intent == 'generate_case':
                    for si, scenarios in enumerate(groups(result['items'], 3000)):
                        needed = {r for s in scenarios for r in s['requirement_ids']}
                        case_data = self.generation_data(analysis, [r for r in requirements if r['id'] in needed], scenarios)
                        for cp in count():
                            case_key = key + f':cases:{si}:{cp}'
                            previous_cases = self.pages(run_id, key + f':cases:{si}:', cp)
                            payload = {**case_data, 'cursor': previous_cases[-1].get('next_cursor') if previous_cases else None,
                                       'previous_items': self.item_index([i for p in previous_cases for i in p['items']])}
                            if self.job(run_id, case_key, 'work_cases', '生成用例 · ' + unit['title'], payload, unit['id']):
                                return case_key
                            if not self.result(run_id, case_key).get('has_more'):
                                break
                if not result.get('has_more'):
                    break
        return None

    def coverage_job(self, run_id, unit, prefix, analysis, intent):
        scenarios = [i for r in self.records(run_id,prefix,'work_scenarios') for i in r['_result']['items']]
        cases = [i for r in self.records(run_id,prefix,'work_cases') for i in r['_result']['items']]
        scenario_coverage = contract.coverage(analysis['items'], analysis['report']['business_model'], scenarios,
                                               [{**s,'scenario_id':s['id']} for s in scenarios])
        case_coverage = contract.coverage(analysis['items'], analysis['report']['business_model'], scenarios, cases)
        gaps = scenario_coverage['gaps'] if scenario_coverage['gaps'] or intent=='generate_scenario' else case_coverage['gaps']
        if not gaps:
            return None
        self.update(run_id, coverage=case_coverage if intent=='generate_case' else scenario_coverage)
        task = 'work_scenarios' if scenario_coverage['gaps'] else 'work_cases'
        prefix_repair = prefix+':coverage:'+task+':'
        attempts = [r for r in self.workspace.list(run_id) if r['key'].startswith(prefix_repair)]
        completed = [r for r in attempts if r['status']=='completed']
        # A different remaining scope is progress. Two failed-to-close attempts
        # for exactly the same gap stop with actionable feedback.
        gap_signature = fingerprint(gaps)
        stalled = sum(self.store.cache_get(run_id,'v3:job:'+r['key'])['data'].get('gap_signature')==gap_signature for r in completed)
        if stalled >= 2:
            raise IncompleteCoverage(f'同一组覆盖缺口补齐两次仍未收敛；已保存成果，请补充这些规则：{", ".join(g["title"] for g in gaps[:5])}')
        active = next((r['key'] for r in attempts if r['status']!='completed'), None)
        key = active or prefix_repair+str(len(completed))
        # One missing scope per work item keeps even a large gap list local.
        gap = gaps[0]
        wanted_scenarios = [s for s in scenarios if s['id']==gap['id'] or gap['id'] in s['requirement_ids']+s['branch_ids']]
        req_ids = {r for s in wanted_scenarios for r in s['requirement_ids']}
        requirements = [r for r in analysis['items'] if r['id']==gap['id'] or r['id'] in req_ids or set(r['refs']).intersection(gap['refs'])]
        data = self.generation_data(analysis, requirements, wanted_scenarios if task=='work_cases' else None)
        data.update(coverage_gaps=[gap],gap_signature=gap_signature,previous_items=self.item_index(cases if task=='work_cases' else scenarios))
        if self.job(run_id,key,task,'补齐覆盖 · '+unit['title'],data,unit['id']):
            return key
        return None

    @staticmethod
    def item_index(items):
        # Never resend generated steps; only the latest page and coverage IDs.
        return [{k: i[k] for k in ('id', 'title', 'scenario_id', 'type') if k in i} for i in items][-20:]

    def pages(self, run_id, prefix, count):
        return [self.result(run_id, prefix + str(i)) for i in range(count)]

    def records(self, run_id, prefix, task):
        return [self.workspace.get(run_id, r['key']) for r in self.workspace.list(run_id)
                if r['key'].startswith(prefix + ':') and r['kind'] == task and r['status'] == 'completed']

    def review_groups(self, run_id, prefix, analysis):
        scenarios = [i for r in self.records(run_id, prefix, 'work_scenarios') for i in r['_result']['items']]
        cases = [i for r in self.records(run_id, prefix, 'work_cases') for i in r['_result']['items']]
        effective=self.effective_review_cases(run_id,prefix,cases)
        for group in groups(cases, 4500):
            scenario_ids = {c['scenario_id'] for c in group}
            wanted = [s for s in scenarios if s['id'] in scenario_ids]
            req_ids = {r for s in wanted for r in s['requirement_ids']}
            data = self.generation_data(analysis, [r for r in analysis['items'] if r['id'] in req_ids], wanted)
            yield {**data, 'items': group, 'case_types': self.store.run(run_id)['_profile'].get('case_types', []),
                   'type_coverage': {t:sum(c['type']==t for c in effective) for t in self.store.run(run_id)['_profile'].get('case_types',[])},
                   'review_phase': 'one_pass', 'selected_ids': [i['id'] for i in group]}

    def effective_review_cases(self, run_id, prefix, originals):
        current=copy.deepcopy(originals)
        if prefix=='single:batch':
            for batch in self.manifest(run_id).get('single_batches',{}).values():
                current=apply_operations(current,batch.get('previous_operations',[]))
                accepted=self.result(run_id,self.single_key(batch))
                if accepted:
                    current=apply_operations(current,accepted['operations'])
            return current
        for record in self.records(run_id,prefix,'work_review'):
            current=apply_operations(current,record['_result']['operations'])
        return current

    def scoped_review_data(self, run, batch, prefix):
        artifact=run['_artifact_snapshot']
        if artifact['type']!='cases':
            raise DomainError('用例评审需要选择用例成果。')
        report=artifact.get('report',{})
        if not all(k in report for k in ('requirements','scenarios','business_model')):
            return {}
        scenarios=[s for s in report['scenarios'] if s['id'] in {i['scenario_id'] for i in batch}]
        req_ids={r for i in batch for r in i['requirement_ids']}
        analysis={'items':report['requirements'],'report':{**report,'strategy':report.get('strategy',
            {'depth':run['agent']['depth'],'techniques':['业务规则检查']})}}
        data=self.generation_data(analysis,[r for r in report['requirements'] if r['id'] in req_ids],scenarios)
        effective=self.effective_review_cases(run['id'],prefix,artifact['items'])
        types=run['_profile'].get('case_types',[])
        return {**data,'case_types':types,'type_coverage':{t:sum(i['type']==t for i in effective) for t in types}}

    def single_batch(self, run, manifest, identity, items):
        """A batch revision changes only when its input is affected, not each chat turn."""
        batch_id=fingerprint(identity)[:20]
        batches=manifest.setdefault('single_batches',{})
        if batch_id not in batches:
            batches[batch_id]={'id':batch_id,'items':items,'revision':0,
                               'instructions':copy.deepcopy(run.get('_instructions',[]))}
            self.save_manifest(run['id'],manifest)
        return batches[batch_id]

    @staticmethod
    def single_key(batch):
        return f'single:batch:{batch["id"]}:{batch["revision"]}'

    def single_data(self, run, batch, data):
        data={**data,'_goal_frozen':True,'goal':self.workspace._goal(run)}
        instructions=batch.get('instructions',[])
        if instructions:
            latest=instructions[-1]
            if size(latest['content'])<=3000:
                data['goal']+='\n最新补充要求（冲突时以此为准）：'+latest['content']
            data['instruction_sources']=[{'refs':i['refs'],'characters':len(i['content'])} for i in instructions]
        return data

    async def update_single_batches(self, state, manifest):
        run=self.current(state)
        batches=list(manifest.get('single_batches',{}).values())
        candidates=[{'id':b['id'],'title':'当前成果条目','requirements':self.item_index(b['items'])} for b in batches]
        affected=set()
        for index,group in enumerate(groups(candidates,6500)):
            context=self.bounded_context(run,'work_impact',{'units':group})
            result=await self.engine.call(run['id'],f'v3:single-impact:{state["epoch"]}:{index}','work_impact',context)
            self.current(state)
            contract.require(type(result.get('global_change')) is bool,'global_change','boolean')
            contract.require(isinstance(result.get('unit_ids'),list) and set(result['unit_ids']) <= {u['id'] for u in group},'unit_ids','provided_unit_ids')
            affected.update(u['id'] for u in group if result['global_change'] or u['id'] in result['unit_ids'])
            if result.get('adds_new_scope') and group:
                affected.add(group[0]['id'])
        changed=0
        for batch in batches:
            key=self.single_key(batch)
            # Unfinished work must consume the latest request after the epoch
            # guard discarded any in-flight reply. Accepted unrelated work stays.
            if batch['id'] in affected or self.result(run['id'],key) is None:
                accepted=self.result(run['id'],key)
                if accepted is not None:
                    batch['items']=copy.deepcopy(accepted['items'])
                    batch['previous_operations']=batch.get('previous_operations',[])+copy.deepcopy(accepted['operations'])
                record=self.workspace.get(run['id'],key)
                if record:
                    record['_superseded']=True
                    self.store.put('agent_work',record)
                batch['revision']+=1
                batch['instructions']=copy.deepcopy(run.get('_instructions',[]))
                changed+=1
        self.insight(run['id'],f'补充要求更新 {changed}/{len(batches)} 组；保留不受影响的已完成修改和评审。',[],'decision')

    async def single_plan(self, state, manifest):
        run, run_id = self.current(state), state['run_id']
        intent = state['intent']
        prefix = f'single:{state["epoch"]}'
        if intent == 'query':
            key = prefix + ':query'
            data = {'sources': self.documents.catalog(run['_source_ids']), 'evidence': self.query_evidence(run)}
            task = 'work_query'
        elif intent == 'learn_template':
            key, task = prefix + ':template', 'work_template'
            # Samples are accessed through the same bounded tools.
            data = {'sources': self.documents.catalog(run['_source_ids']), 'evidence': []}
        else:
            artifact = run.get('_artifact_snapshot')
            if not artifact:
                if intent=='review_case':
                    return self.import_plan(state, manifest)
                raise DomainError('请先选择已保存的用例或场景，再执行修改。')
            selected = run['_request'].get('selected_ids')
            chosen = [i for i in artifact['items'] if selected is None or i['id'] in selected]
            task = 'work_modify' if intent == 'modify' else 'work_review'
            batches=[self.single_batch(run,manifest,[artifact['id'],artifact['revision'],task,[i['id'] for i in items]],items)
                     for items in groups(chosen,5500)]
            for batch in batches:
                key = self.single_key(batch)
                items=batch['items']
                data = {'items':items, 'artifact_type': artifact['type'], 'selected_ids': [i['id'] for i in items]}
                if intent=='review_case':
                    data.update(self.scoped_review_data(run,items,'single:batch'))
                if self.job(run_id, key, task, '定向修改' if intent == 'modify' else '评审用例', self.single_data(run,batch,data)):
                    return {**state, 'key': key, 'action': 'work'}
            return {**state, 'action': 'finish'}
        if self.job(run_id, key, task, '查询当前问题' if intent == 'query' else '学习模板', data):
            return {**state, 'key': key, 'action': 'work'}
        return {**state, 'action': 'finish'}

    def query_evidence(self, run):
        goal=run['_request']['content']
        if len(goal)>500:
            return []
        evidence=self.authorized_evidence(run)
        try:
            matches=self.documents.search(list(dict.fromkeys(e['source_id'] for e in evidence.values())),goal)['matches'][:2]
        except DomainError:
            return []
        selected=[]
        for match in matches:
            if match['id'] not in evidence:
                continue
            item=evidence_view([evidence[match['id']]])[0]
            body=item['text']
            start=max(0,body.find(match['text']))
            end=min(len(body),start+1500)
            item.update(text=body[start:end],excerpt={'start':start,'end':end,'total':len(body)})
            if size(selected+[item])>4000:
                break
            selected.append(item)
        return selected

    def import_plan(self, state, manifest):
        run_id = state['run_id']
        if not manifest['units']:
            raise DomainError('请上传待评审的实际用例；格式示例不能作为业务依据。')
        for unit in manifest['units']:
            prefix = f'single:import:{unit["id"]}:'
            for page in count():
                key = prefix+str(page)
                previous = self.pages(run_id,prefix,page)
                data = {'evidence':evidence_view(unit['evidence']),
                        'previous_items':self.item_index([i for p in previous for i in p['items']]),
                        'cursor':previous[-1].get('next_cursor') if previous else None}
                if self.job(run_id,key,'work_import','导入用例 · '+unit['title'],data,unit['id']):
                    return {**state,'key':key,'action':'work'}
                imported = self.result(run_id,key)
                for index,batch in enumerate(groups(imported['items'],4500)):
                    entry=self.single_batch(self.current(state),manifest,[key,index,'work_review'],batch)
                    review_key = self.single_key(entry)
                    review_data = {'artifact_type':'cases','items':batch,'selected_ids':[i['id'] for i in batch]}
                    review_data=self.single_data(self.current(state),entry,review_data)
                    if self.job(run_id,review_key,'work_review','评审导入用例 · '+unit['title'],review_data,unit['id']):
                        return {**state,'key':review_key,'action':'work'}
                if not imported.get('has_more'):
                    break
        return {**state,'action':'finish'}

    def context(self, state, purpose=None, **extra):
        # Feedback only; keep the inherited semantic feedback helper bounded.
        return self.workspace.model_context(self.current(state), purpose or 'feedback', extra)

    def authorized_evidence(self, run):
        sources={s:self.store.get('source',s) for s in run['_source_ids']}
        ids = [s for s,source in sources.items() if source['project_id']==run['project_id'] and
               (source.get('_active', True) or s in run.get('_historical_source_ids',[]))]
        return {e['id']: e for e in self.documents.evidence(ids) if e['role'] != 'example' or run['intent']=='learn_template'}

    async def work(self, state):
        run = self.current(state)
        run_id, key = run['id'], state['key']
        if self.result(run_id, key) is not None:
            return {}
        job = self.store.cache_get(run_id, 'v3:job:' + key)
        task, data = job['task'], copy.deepcopy(job['data'])
        session_key = 'v3:session:' + key
        session = self.store.cache_get(run_id, session_key) or {'round': 0, 'observations': [], 'seen': [], 'evidence': []}
        evidence = self.authorized_evidence(run)
        refs = {r for collection in ('analysis', 'scenarios', 'items') for item in data.get(collection, []) for r in item.get('refs', [])}
        if 'evidence' not in data:
            # Accepted business facts already carry their text. Do not resend documents.
            data['evidence'] = evidence_view([evidence[r] for r in refs if r in evidence], text=False)
        current_ids = {e['id'] for e in data.get('evidence', [])}
        supplied = data.get('evidence', [])
        for extra_evidence in session['evidence']:
            supplied = [e for e in supplied if e['id'] != extra_evidence['id']]
            supplied.append(extra_evidence)
        data['evidence'] = supplied
        if session['observations']:
            data['observations'] = session['observations']
        record = self.workspace.get(run_id, key)
        if record is None or record['status'] != 'running':
            self.workspace.begin(run_id, key, task, job['title'], [e['id'] for e in data['evidence']])
        self.engine.stage(run_id, task)
        self.sync(run_id)
        try:
            context = self.bounded_context(run, task, data)
            cache_id = self.reuse_key(run, task, context, key) if task in ('work_analyze', 'work_scenarios', 'work_cases') else None
            reused = self.reused(cache_id) if cache_id else None
            if reused is not None:
                with self.store.transaction():
                    self.current(state)
                    artifact = self.preview(run_id, key, task, reused)
                    self.workspace.accept(run_id, key, reused, artifact['id'] if artifact else None)
                    if task=='work_analyze' and context.get('automatic_depth'):
                        self.update(run_id,depth=reused['report']['strategy']['depth'])
                    self.insight(run_id, '复用已校验的当前规则和配置结果，无需再次请求模型。', list(refs), 'decision')
                    self.sync(run_id)
                return {}
            raw = await self.engine.call(run_id, key + ':call:' + str(session['round']), task, context)
            self.current(state)
            if raw.get('kind') == 'need_context':
                self.read_tools(state, key, raw, session, evidence)
                self.store.cache_set(run_id, session_key, session)
                return {}
            available = {e['id']: e for e in data['evidence']}
            result = await self.validate_reply(state, job, context, raw, available, session['round'])
            if result.get('kind') == 'need_context':
                self.read_tools(state, key, result, session, evidence)
                self.store.cache_set(run_id, session_key, session)
                return {}
            with self.store.transaction():
                self.current(state)
                if task=='work_links':
                    self.accept_dependencies(state, result, available)
                artifact = self.preview(run_id, key, task, result)
                self.workspace.accept(run_id, key, result, artifact['id'] if artifact else None)
                if task=='work_analyze' and context.get('automatic_depth'):
                    self.update(run_id,depth=result['report']['strategy']['depth'])
                if cache_id:
                    self.store.put('agent_result_cache', {'id':cache_id, 'project_id':run['project_id'],
                        'chat_id':run['chat_id'], '_result':result})
                self.insight(run_id, result.get('summary') or result.get('report', {}).get('summary') or '当前工作项已校验保存。', list(available), 'finding')
                self.sync(run_id)
        except StaleInstruction:
            raise
        except Exception as error:
            # GraphInterrupt is a BaseException and deliberately passes through.
            self.workspace.fail(run_id, key, str(error))
            self.sync(run_id)
            raise
        return {}

    def bounded_context(self, run, task, data):
        from .model import SYSTEM, TASK_INSTRUCTIONS
        overhead = len(SYSTEM) + len(TASK_INSTRUCTIONS[task])
        data=copy.deepcopy(data)
        frozen=data.pop('_goal_frozen',False)
        if run.get('_instructions') and not frozen:
            latest=run['_instructions'][-1]
            instruction=latest['content']
            data.setdefault('goal',self.workspace._goal(run))
            if size(instruction)<=3000:
                data['goal']+='\n最新补充要求（冲突时以此为准）：'+instruction
            else:
                data['latest_instruction']={'refs':latest['refs'],'characters':len(instruction),
                    'instruction':'完整补充要求已保存为来源，请按需读取这些引用。'}
        context = self.workspace.model_context(run, task, data, min(self.CONTEXT_BUDGET, 24000-overhead))
        self.engine.trace('context.selected', run['id'], task=task, context_characters=size(context),
                          complete_prompt_characters=size(context)+overhead,
                          token_estimate=size(context)+overhead, token_estimate_method='conservative_characters_not_usage')
        return context

    def reuse_key(self, run, task, context, key):
        # Scope, full relevant data, relevant profile fields, runtime and model
        # configuration all participate. Credentials are hashed, never exported.
        return 'reuse_' + fingerprint({'project':run['project_id'],'chat':run['chat_id'],
            'task':task,'context':context,'namespace':key,'runtime':{k:v for k,v in self.engine.runtime.items() if k not in ('loaded_at','commit')},
            'model':self.engine.settings.value})

    def reused(self, cache_id):
        try:
            return self.store.get('agent_result_cache', cache_id)['_result']
        except DomainError as error:
            if error.status != 404:
                raise
            return None

    def read_tools(self, state, key, raw, session, evidence):
        requests = raw.get('requests')
        contract.require(isinstance(requests, list) and 1 <= len(requests) <= 4, 'requests', '1..4_read_only_requests')
        if session['round'] >= session.get('read_allowance',8):
            interrupt({'type':'work_pause','reason':'context_budget',
                       'message':f'当前工作项已查阅 {session["round"]} 轮仍未产出成果。可继续查阅，或补充更明确的范围。'})
            self.current(state)
            session['read_allowance']=session.get('read_allowance',8)+8
        observations, read = [], []
        for request in requests:
            signature = fingerprint(request)
            if signature in session['seen']:
                raise DomainError('模型重复请求相同资料且未产生新结果；请核查当前问题或重试该工作项。')
            tool = request.get('tool')
            if tool == 'search_evidence':
                result = self.documents.search(sorted({e['source_id'] for e in evidence.values()}), request.get('query'))
                # Search previews are locators, not substituted full evidence.
                result['matches'] = result['matches'][:6]
                result['truncated'] = len(result['matches']) < result['total_matches']
            elif tool == 'read_evidence':
                wanted = request.get('refs')
                contract.require(isinstance(wanted, list) and 1 <= len(wanted) <= 12 and all(isinstance(r,str) and r in evidence for r in wanted), 'requests.refs', 'authorized_evidence_ids')
                offset = request.get('offset', 0)
                contract.require(type(offset) is int and offset >= 0, 'requests.offset', 'nonnegative_integer')
                result = {'evidence': [], 'omitted_refs': []}
                for ref in wanted:
                    e = evidence_view([evidence[ref]])[0]
                    text = e['text']
                    if offset > len(text):
                        raise DomainError('读取位置超过原文长度')
                    end = min(len(text), offset + 2000)
                    e['text'] = text[offset:end]
                    e['excerpt'] = {'start': offset, 'end': end, 'total': len(text)}
                    if size(result) + size(e) > 4000:
                        result['omitted_refs'].append(ref)
                    else:
                        result['evidence'].append(e)
                        read.append(e)
                result['next_offsets'] = {e['id']: e['excerpt']['end'] for e in result['evidence'] if e['excerpt']['end'] < e['excerpt']['total']}
            elif tool in ('get_facts','list_units','get_dependencies'):
                manifest=self.manifest(state['run_id'])
                cursor=request.get('cursor',0)
                contract.require(type(cursor) is int and cursor>=0,'requests.cursor','nonnegative_integer')
                inventory=[]
                for unit in manifest['units']:
                    prefix=f'{unit["id"]}:{fingerprint(manifest["overrides"].get(unit["id"], []))[:12]}:analysis'
                    accepted=self.result(state['run_id'],prefix)
                    if not accepted:
                        continue
                    if tool=='list_units':
                        inventory.append({'id':unit['id'],'title':unit['title'],'refs':unit['refs']})
                    else:
                        wanted=request.get('unit_ids')
                        contract.require(isinstance(wanted,list) and all(isinstance(i,str) for i in wanted),'requests.unit_ids','array_of_unit_ids')
                        if unit['id'] in wanted:
                            inventory.append({'id':unit['id'],'title':unit['title'],'requirements':accepted['items'],
                                              'business_model':accepted['report']['business_model']})
                result={'units':[],'next_cursor':None,'total':len(inventory)}
                for index,item in enumerate(inventory[cursor:],cursor):
                    if size({**result,'units':result['units']+[item]})>6500:
                        result['next_cursor']=index
                        break
                    result['units'].append(item)
                for unit in result['units']:
                    for requirement in unit.get('requirements',[]):
                        read += evidence_view([evidence[r] for r in requirement['refs'] if r in evidence],False)
            elif tool == 'get_artifact_items':
                artifact=self.current(state).get('_artifact_snapshot')
                if not artifact:
                    result={'items':[],'total':0,'next_cursor':None}
                else:
                    wanted=request.get('ids')
                    cursor=request.get('cursor',0)
                    contract.require(type(cursor) is int and cursor>=0,'requests.cursor','nonnegative_integer')
                    contract.require(wanted is None or isinstance(wanted,list) and all(isinstance(i,str) for i in wanted),'requests.ids','artifact_item_ids')
                    inventory=[i for i in artifact['items'] if wanted is None or i['id'] in wanted]
                    result={'items':[],'total':len(inventory),'next_cursor':None}
                    for index,item in enumerate(inventory[cursor:],cursor):
                        if size({**result,'items':result['items']+[item]})>6000:
                            result['next_cursor']=index
                            break
                        result['items'].append(item)
                        read += evidence_view([evidence[r] for r in item.get('refs',[]) if r in evidence],False)
            elif tool == 'get_coverage_gaps':
                cursor=request.get('cursor',0)
                contract.require(type(cursor) is int and cursor>=0,'requests.cursor','nonnegative_integer')
                gaps=self.current(state)['agent'].get('coverage',{}).get('gaps',[])
                result={'gaps':gaps[cursor:cursor+5],'total':len(gaps),'next_cursor':cursor+5 if cursor+5<len(gaps) else None}
            elif tool == 'get_profile_fields':
                fields=request.get('fields')
                profile=self.current(state)['_profile']
                contract.require(isinstance(fields,list) and all(isinstance(f,str) and f in profile for f in fields),'requests.fields','profile_field_names')
                result={'fields':{f:profile[f] for f in fields}}
                if size(result)>5000:
                    raise DomainError('所选配置字段超过读取预算；请减少一次读取的字段。')
            elif tool == 'list_sections':
                cursor = request.get('cursor', 0)
                contract.require(type(cursor) is int and cursor >= 0, 'requests.cursor', 'nonnegative_integer')
                values = list(evidence.values())
                result = {'sections': evidence_view(values[cursor:cursor+20], text=False), 'total': len(values),
                          'next_cursor': cursor+20 if cursor+20 < len(values) else None}
            else:
                raise DomainError('不支持的资料工具；只允许 search_evidence、read_evidence、list_sections')
            # Read body lives once in evidence; the observation contains cursors only.
            observation_result = {k:v for k,v in result.items() if k != 'evidence'}
            if tool == 'read_evidence':
                observation_result['read_refs'] = [e['id'] for e in result['evidence']]
            observations.append({'tool': tool, 'result': observation_result})
            session['seen'].append(signature)
            self.insight(state['run_id'], {'search_evidence':'检索相关段落','read_evidence':'读取所需原文','list_sections':'查看文档目录','get_facts':'读取已确认规则','list_units':'查看规则分组','get_dependencies':'查看关联业务图','get_artifact_items':'读取相关成果','get_coverage_gaps':'查看覆盖缺口','get_profile_fields':'读取所需配置'}[tool] + '：' + str(raw.get('summary') or '补充当前工作项所需资料。'), [e['id'] for e in result.get('evidence', [])], 'tool')
        previous={fingerprint(e):e for e in session['evidence']}
        for e in read:
            previous[fingerprint(e)]=e
        # Keep the newest requested evidence; old bodies remain addressable by ref.
        retained=[]
        for e in reversed(list(previous.values())):
            if size(retained+[e])>6000:
                break
            retained.insert(0,e)
        session.update(round=session['round']+1, observations=observations, evidence=retained)

    async def validate_reply(self, state, job, context, raw, evidence, round_number):
        task, key = job['task'], job['key']
        def validate(value):
            if task == 'work_analyze':
                return analysis_patch(value, evidence, job.get('unit') or key, value.get('depth',context['depth']) if context.get('automatic_depth') else context['depth'])
            if task in ('work_scenarios', 'work_cases'):
                kind = 'cases' if task == 'work_cases' else 'scenarios'
                result = generated_patch(value, kind, evidence, context, key)
                if result['has_more']:
                    contract.require(bool(result['items']) and isinstance(result.get('next_cursor'), str) and result['next_cursor'] != context.get('cursor'), 'next_cursor', 'new_cursor_with_new_items')
                previous = self.records(state['run_id'], key.rsplit(':',1)[0], task)
                def semantic(item):
                    return fingerprint({k:v for k,v in item.items() if k!='id'})
                old = {semantic(i) for r in previous for i in r['_result']['items']}
                if any(semantic(i) in old for i in result['items']):
                    raise DomainError('模型重复生成已保存条目；已保留前面的页，请重试当前页。')
                cursors = {r['_result'].get('next_cursor') for r in previous}
                if result['has_more'] and result['next_cursor'] in cursors:
                    raise DomainError('模型返回重复分页游标；已保留前面的页，请重试当前页。')
                return result
            if task in ('work_review','work_modify'):
                patched = operation_patch(value, context.get('artifact_type', 'cases'), context['items'], evidence, key,
                                       context.get('selected_ids'), context if 'analysis' in context else None)
                if task=='work_review' and 'analysis' in context:
                    assessment=patched['report'].get('type_assessment')
                    contract.require(isinstance(assessment,list),'report.type_assessment','array')
                    configured=context.get('case_types',[])
                    contract.require({a.get('type') for a in assessment if isinstance(a,dict)}==set(configured),'report.type_assessment','assessment_for_each_configured_type')
                    for index,entry in enumerate(assessment):
                        contract.require(isinstance(entry,dict) and type(entry.get('applicable')) is bool,f'report.type_assessment[{index}].applicable','boolean')
                        contract.text(entry.get('reason'),f'report.type_assessment[{index}].reason')
                        if entry['applicable']:
                            count=context.get('type_coverage',{}).get(entry['type'],0) + sum(i['type']==entry['type'] for i in patched['items']) - sum(i['type']==entry['type'] for i in context['items'])
                            if count<=0:
                                raise IncompleteCoverage('评审确认类型 '+entry['type']+' 适用但没有对应用例；请重试当前评审组补齐。')
                    def links(items):
                        return {('requirement',r) for i in items for r in i['requirement_ids']} | {('branch',b) for i in items for b in i['branch_ids']} | {('scenario',i['scenario_id']) for i in items}
                    if links(context['items']) - links(patched['items']):
                        raise IncompleteCoverage('本组评审删除了唯一覆盖用例且未提供替代；请重试当前评审组，其他组保留。')
                return patched
            if task == 'work_links':
                output=copy.deepcopy(value)
                contract.text(output.get('summary'),'summary')
                contract.require(isinstance(output.get('edges'),list),'edges','array')
                manifest=self.manifest(state['run_id'])
                units={u['id'] for u in manifest['units']}
                node_map={n['id']:n for u in context.get('units',[]) for n in u['nodes']}
                for observation in context.get('observations',[]):
                    if observation['tool']=='get_facts':
                        for fact in observation['result'].get('units',[]):
                            node_map.update({n['id']:n for n in fact['business_model']['nodes']})
                for index,edge in enumerate(output['edges']):
                    contract.require(isinstance(edge,dict),f'edges[{index}]','object')
                    contract.require(edge.get('from') in node_map and edge.get('to') in node_map,f'edges[{index}]','provided_node_endpoints')
                    contract.text(edge.get('label'),f'edges[{index}].label')
                    contract.refs(edge.get('refs'),evidence,f'edges[{index}].refs')
                    edge['id']=stable_id('B','dependencies',fingerprint({k:edge[k] for k in ('from','to','label','refs')}))
                output['nodes']=[node_map[n] for n in dict.fromkeys(p for e in output['edges'] for p in (e['from'],e['to']))]
                updates=output.get('updates',[])
                contract.require(isinstance(updates,list),'updates','array')
                for index,change in enumerate(updates):
                    contract.require(isinstance(change,dict) and change.get('unit_id') in units,f'updates[{index}].unit_id','provided_unit_id')
                    contract.text(change.get('summary'),f'updates[{index}].summary')
                    contract.refs(change.get('evidence_refs'),evidence,f'updates[{index}].evidence_refs')
                    from_units=change.get('from_units')
                    contract.require(from_units is None or isinstance(from_units,list) and set(from_units)<=units,f'updates[{index}].from_units','provided_unit_ids')
                    candidates={fingerprint(e):e for u in manifest['units'] if not u.get('derived') and (from_units is None or u['id'] in from_units)
                                for e in u['evidence']+manifest['overrides'].get(u['id'],[]) if e['id'] in change['evidence_refs']}
                    parts=evidence_view(list(candidates.values()))
                    # A paragraph ref can name many excerpts. Never expand it
                    # back to the full document; request precise source units.
                    contract.require(bool(parts) and size(parts)<=7500,f'updates[{index}].from_units','bounded_subset_of_source_units')
                    change['_evidence']=parts
                return output
            if task == 'work_import':
                imported = copy.deepcopy(value)
                validate_items('cases', imported.get('items'), evidence)
                contract.require(type(imported.get('has_more')) is bool,'has_more','boolean')
                if imported['has_more']:
                    contract.require(bool(imported['items']) and isinstance(imported.get('next_cursor'),str) and imported['next_cursor']!=context.get('cursor'),'next_cursor','new_cursor_and_new_items')
                for item in imported['items']:
                    item['id'] = stable_id('C',key,item['id'])
                return imported
            if task == 'work_query':
                contract.text(value.get('answer'), 'answer')
                contract.strings(value.get('refs'), 'refs')
                contract.require(set(value['refs']) <= set(evidence), 'refs', 'provided_evidence_ids')
                return value
            if task == 'work_template':
                profile_config(value.get('config'))
                return value
            raise DomainError('未知工作项类型')
        from .incremental_repair import recover
        return await recover(self, state, job, context, raw, evidence, round_number, validate)

    @staticmethod
    def patch_issue(value, context, issue):
        # Validation runs on merged items, but the provider returned operations.
        # Address only the operation that supplied the invalid leaf.
        import re
        match=re.fullmatch(r'items\[(\d+)\]\.(.+)',issue['path'])
        if not match:
            return issue
        merged=apply_operations(context['items'],value['operations'],context.get('selected_ids'))
        target=merged[int(match[1])]['id']
        field=match[2].split('.')[0].split('[')[0]
        for index in range(len(value['operations'])-1,-1,-1):
            op=value['operations'][index]
            item=op.get('item')
            if isinstance(item,dict) and (op.get('id')==target or op.get('op')=='add' and item.get('id')==target) and field in item:
                return {**issue,'path':f'operations[{index}].item.{match[2]}'}
        raise OutputValidationError('无效字段不来自本次修改，不能自动改动原条目',issue['path'],'field_written_by_current_patch')

    def preview(self, run_id, key, task, result):
        if 'items' not in result:
            return None
        kind = {'work_analyze':'analysis','work_scenarios':'scenarios','work_cases':'cases','work_review':'cases','work_modify':'cases','work_import':'cases'}[task]
        job = self.store.cache_get(run_id, 'v3:job:' + key)
        kind = job['data'].get('artifact_type', kind)
        artifact = self.store.artifact(run_id, key+':artifact', kind, '阶段成果 · '+job['title'], result['items'], result.get('report', {'summary': result.get('summary','')}))
        # Draft is inspectable, but remains excluded from the chat's final targets.
        artifact['_preview_run_id'] = run_id
        self.store.put('artifact', artifact)
        return artifact

    async def gate(self, state):
        run = self.current(state)
        if state['key']=='input':
            response=interrupt({'type':'clarification','questions':['请描述要测试的业务或上传需求文档，例如：登录使用用户名和密码，失败五次锁定。']})
            if response.get('answer') and not self.is_continue_command(response['answer']):
                self.add_instruction(run['id'],response['answer'],resume=False)
            self.store.update_run(run['id'],_gate_feedback_pending=False)
            return {'stale':bool(response.get('instruction') or response.get('answer'))}
        if state['key'] == 'pause':
            response = interrupt({'type':'work_pause', 'message':'已在工作项边界暂停，完成的成果已保存。'})
            self.store.update_run(run['id'], _pause_requested=False, _gate_feedback_pending=False)
            self.update(run['id'], pause_requested=False)
            return {}
        manifest = self.manifest(run['id'])
        artifacts = [r['artifact_id'] for r in self.workspace.list(run['id']) if r['kind']=='work_analyze' and r.get('artifact_id')]
        response = interrupt({'type':'strategy_review', 'artifact_id': artifacts[-1], 'artifact_ids': artifacts,
                              'message':'业务图和测试方向已保存。可以补充规则，或按当前信息继续。'})
        if response.get('instruction'):
            return {}
        if response.get('answer') and not self.is_continue_command(response['answer']):
            answer=response['answer']
            if size(answer)<=4000:
                context=self.workspace.model_context(run,'feedback',{'feedback':answer})
                decision=await self.engine.call(run['id'],'v3:feedback:'+fingerprint(answer),'agent_feedback',context)
                self.current(state)
                contract.require(type(decision.get('proceed')) is bool,'proceed','boolean')
                contract.require(type(decision.get('has_changes')) is bool,'has_changes','boolean')
            else:
                decision={'proceed':False,'has_changes':True}
            proceed=bool(response.get('proceed')) or decision['proceed']
            if decision['has_changes']:
                revised=self.add_instruction(run['id'],answer,resume=False)
                self.store.update_run(run['id'],_gate_feedback_pending=False,
                    _clarification_decision={'mode':'proceed','epoch':revised['_instruction_version']} if proceed else None)
                return {'stale':True}
            if not proceed:
                self.store.update_run(run['id'],_gate_feedback_pending=False)
                self.insight(run['id'],'保留当前方向确认；可以明确指出需要修改的规则，或选择按当前信息继续。',[],'decision')
                return {}
        manifest['approved'] = True
        self.save_manifest(run['id'], manifest)
        self.store.update_run(run['id'], _gate_feedback_pending=False,
            _clarification_decision={'mode':'proceed','epoch':run.get('_instruction_version',0)})
        self.insight(run['id'], '按当前信息继续；未决规则仍标为未确认，不会再次阻塞同一项设计。', [], 'decision')
        return {}

    async def apply_instructions(self, state, manifest):
        run = self.current(state)
        if manifest['intent'] in ('query','modify','review_case','learn_template'):
            if manifest['intent'] in ('modify','review_case'):
                await self.update_single_batches(state,manifest)
            manifest['epoch']=state['epoch']
            self.save_manifest(run['id'],manifest)
            self.store.update_run(run['id'],_applied_instruction_version=state['epoch'])
            self.update(run['id'],pending_instructions=0)
            return
        new_sources = [s for s in run['_source_ids'] if s not in manifest['base_sources']]
        changes = evidence_view(self.documents.evidence(new_sources))
        # Impact sees accepted rule titles, never complete source bodies/history.
        candidates = []
        for unit in manifest['units']:
            old = f'{unit["id"]}:{fingerprint(manifest["overrides"].get(unit["id"], []))[:12]}:analysis'
            accepted = self.result(run['id'], old)
            candidates.append({'id':unit['id'], 'title':unit['title'], 'requirements':self.item_index(accepted['items']) if accepted else [], 'refs':unit['refs']})
        affected = set()
        new_scope=False
        for index, batch in enumerate(groups(candidates, 6500)):
            context = self.workspace.model_context(run, 'work_impact', {'units':batch,'evidence':changes})
            result = await self.engine.call(run['id'], f'v3:impact:{state["epoch"]}:{index}', 'work_impact', context)
            self.current(state)
            contract.require(type(result.get('global_change')) is bool, 'global_change','boolean')
            contract.require(isinstance(result.get('unit_ids'),list) and set(result['unit_ids']) <= {u['id'] for u in batch},'unit_ids','provided_unit_ids')
            affected.update(u['id'] for u in batch if result['global_change'] or u['id'] in result['unit_ids'])
            new_scope = new_scope or result.get('adds_new_scope',False)
        for uid in affected:
            self.supersede(run['id'],uid,manifest['overrides'].get(uid,[]))
            manifest['overrides'][uid] = changes
        added=[]
        if new_scope or not affected:
            existing_sources={s for u in manifest['units'] for s in u['source_ids']}
            selected=[s for s in new_sources if s not in existing_sources]
            if selected:
                added=self.workspace.units({**run,'_source_ids':selected},self.EVIDENCE_BUDGET)
        if affected or added:
            for unit in manifest['units']:
                if unit.get('derived'):
                    self.supersede(run['id'],unit['id'],manifest['overrides'].get(unit['id'],[]))
            manifest['units']=[u for u in manifest['units'] if not u.get('derived')]+added
            manifest.pop('dependency_jobs',None)
        manifest.update(epoch=state['epoch'], approved=bool(self.continuation(run)))
        self.save_manifest(run['id'], manifest)
        self.store.update_run(run['id'], _applied_instruction_version=state['epoch'])
        self.update(run['id'], pending_instructions=0)
        self.insight(run['id'], f'补充指令影响 {len(affected)}/{len(candidates)} 组，新增 {len(added)} 组；保留其他已完成工作。', [e['id'] for e in changes], 'decision')

    def add_instruction(self, run_id, content, resume=True):
        run=self.store.run(run_id)
        if resume and run['status']=='waiting' and run.get('interrupt',{}).get('type')=='work_pause' and self.is_continue_command(content):
            return self.engine.resume(run_id,{'proceed':True})
        if resume and run['status']=='failed':
            # A correction after failure creates a new input revision and reuses
            # unaffected work; the old checkpoint exits through the epoch guard.
            with self.store.transaction():
                self.store.update_run(run_id,status='queued',error=None,_gate_feedback_pending=False)
                value=super().add_instruction(run_id,content,resume=False)
            self.engine.schedule(run_id)
            return value
        return super().add_instruction(run_id,content,resume)

    async def finish(self, state):
        run = self.current(state)
        run_id, intent = run['id'], state['intent']
        if intent in ('query','learn_template','modify','review_case'):
            return self.finish_single(state)
        manifest = self.manifest(run_id)
        requirements, scenarios, cases, nodes, edges, diagrams = [],[],[],[],[],[]
        questions, assumptions, reports = [],[],[]
        for unit in manifest['units']:
            prefix = f'{unit["id"]}:{fingerprint(manifest["overrides"].get(unit["id"], []))[:12]}:analysis'
            analysis = self.result(run_id, prefix)
            requirements += analysis['items']
            report = analysis['report']
            nodes += report['business_model']['nodes']; edges += report['business_model']['edges']
            diagrams += report.get('diagrams', [])
            questions += report.get('questions', []); assumptions += report.get('assumptions', [])
            scenarios += [i for r in self.records(run_id,prefix,'work_scenarios') for i in r['_result']['items']]
            reviewed = self.records(run_id,prefix,'work_review')
            cases += [i for r in reviewed for i in r['_result']['items']]
            reports += [r['_result']['report'] for r in reviewed]
        model = {'nodes':list({n['id']:n for n in nodes}.values()),'edges':list({e['id']:e for e in edges}.values())}
        kind = {'generate_case':'cases','generate_scenario':'scenarios','review_requirement':'analysis'}[intent]
        items = {'cases':cases,'scenarios':scenarios,'analysis':requirements}[kind]
        coverage = contract.coverage(requirements,model,scenarios,cases if kind=='cases' else [{**s,'scenario_id':s['id']} for s in scenarios])
        applicable={entry['type'] for report in reports for entry in report.get('type_assessment',[]) if entry.get('applicable')}
        if kind=='cases' and applicable - {i['type'] for i in cases}:
            raise IncompleteCoverage('最终结果缺少已确认适用的用例类型，不能发布完成结果。')
        if kind != 'analysis' and coverage['gaps']:
            self.update(run_id, coverage=coverage)
            raise IncompleteCoverage(f'仍有 {len(coverage["gaps"])} 个覆盖缺口，阶段成果已保存；请查看缺口后补充规则或重试当前工作项。')
        summary = f'已完成 {len(manifest["units"])} 组需求分析，保存 {len(items)} 条' + {'cases':'用例','scenarios':'场景','analysis':'需求规则'}[kind] + '。'
        if kind=='cases':
            summary += f' 已完成一次评审，分 {len(reports)} 组保存；设计覆盖需求 {coverage["requirements_covered"]}/{coverage["requirements_total"]}，分支 {coverage["branches_covered"]}/{coverage["branches_total"]}。尚未执行测试。'
        questions, assumptions = list(dict.fromkeys(questions)),list(dict.fromkeys(assumptions))
        if questions:
            summary += '\n未决问题（保留并继续）：\n'+'\n'.join('- '+q for q in questions)
        if assumptions:
            summary += '\n未确认假设：\n'+'\n'.join('- '+a for a in assumptions)
        report = {'summary':summary,'requirements':requirements,'scenarios':scenarios,'business_model':model,'diagrams':diagrams,
                  'questions':questions,'deferred_questions':questions,'assumptions':assumptions,'coverage':coverage,
                  'review':{'summary':'\n'.join(r.get('summary','已完成本组检查。') for r in reports), 'groups':reports},
                  'source_processing':{'completed_units':len(manifest['units']),'total_units':len(manifest['units'])}}
        with self.store.transaction():
            self.current(state)
            artifact = self.store.artifact(run_id,f'v3:final:{state["epoch"]}',kind,'测试设计结果',items,report)
            self.update(run_id,summary=summary,coverage=coverage)
            self.store.update_run(run_id,_work_estimate=0)
            self.sync(run_id)
            self.store.publish(run_id,[artifact['id']],summary)
        return {'done':True}

    def finish_single(self, state):
        run = self.current(state)
        run_id, intent = run['id'],state['intent']
        manifest=self.manifest(run_id)
        keys={self.single_key(b) for b in manifest.get('single_batches',{}).values()}
        records = [r for r in self.workspace.list(run_id) if r['status']=='completed' and
                   (r['key'] in keys or (intent in ('query','learn_template') and r['key'].startswith(f'single:{state["epoch"]}:')))]
        values = [self.result(run_id,r['key']) for r in records]
        with self.store.transaction():
            self.current(state)
            if intent=='query':
                result=values[0]
                artifact=self.engine.answer(run_id,'v3:answer:'+str(state['epoch']),result['answer'],result['refs'])
                summary=result['answer']
            elif intent=='learn_template':
                result=values[0]; summary=result.get('summary','已生成模板建议。')
                artifact=self.store.artifact(run_id,'v3:template:'+str(state['epoch']),'proposal','模板建议',[],{'config':result['config'],'summary':summary})
            elif intent=='review_case' and not run.get('_artifact_snapshot'):
                reviewed=[self.result(run_id,r['key']) for r in records if r['kind']=='work_review']
                items=[i for v in reviewed for i in v['items']]
                if not items:
                    raise DomainError('没有提取到可评审的实际用例；原文件保留，请检查文件内容。')
                summary=f'已导入并评审 {len(items)} 条用例。'
                artifact=self.store.artifact(run_id,'v3:imported:'+str(state['epoch']),'cases','用例评审结果',items,
                    {'review':{'summary':summary,'groups':[v['report'] for v in reviewed]}})
            else:
                original=run['_artifact_snapshot']
                operations=[]
                by_key={self.single_key(b):b for b in manifest['single_batches'].values()}
                for record,value in zip(records,values):
                    operations+=by_key[record['key']].get('previous_operations',[])+value['operations']
                items=apply_operations(original['items'],operations)
                if intent=='review_case':
                    checked=contract.refreshed_report(original['type'],items,original.get('report',{}),self.authorized_evidence(run))
                    if not items or checked.get('coverage',{}).get('gaps'):
                        raise IncompleteCoverage('评审后的成果仍有覆盖缺口，原已发布版本保持不变。')
                artifact=self.store.revise_artifact(original['id'],original['revision'],items,'agent_'+intent,run_id,f'v3:edit:{state["epoch"]}')
                summary='\n'.join(v['summary'] for v in values)
            self.update(run_id,summary=summary)
            self.store.update_run(run_id,_work_estimate=0)
            self.sync(run_id)
            self.store.publish(run_id,[artifact['id']],summary,proposal=artifact.get('report',{}).get('config') if intent=='learn_template' else None)
        return {'done':True}

