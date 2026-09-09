"""Stage-complete authoring: optimize batch size, never skip human decisions."""
from langgraph.types import interrupt
from .direct import DirectEngine
from .graph import Engine
from .reliable import ReliableEngine, item_errors
from .schemas import DomainError, profile_config
from .documents import parse_text


class FlowEngine(DirectEngine):
    def reliable(self, run_id):
        return self.store.run(run_id).get('graph_version') in (4, 5, 6)

    def direct(self, run_id):
        # Share token-budgeted invocation, but v6 restores the full stage graph.
        return self.store.run(run_id).get('graph_version') in (5, 6)

    def full_flow(self, run_id):
        return self.store.run(run_id).get('graph_version') == 6

    def after_route(self, state):
        if self.full_flow(state['run_id']):
            return Engine.after_route(self, state)
        return super().after_route(state)

    def capacity_groups(self, task, values, builder):
        if self.fits(task, {**builder(values), 'budget_margin': 'x' * 3000}):
            return [values] if values else []
        groups, current = [], []
        for value in values:
            candidate = current + [value]
            if current and not self.fits(task, {**builder(candidate), 'budget_margin': 'x' * 3000}):
                groups.append(current)
                current = []
            current.append(value)
            if not self.fits(task, {**builder(current), 'budget_margin': 'x' * 3000}):
                raise DomainError('单个业务条目与必要上下文超过模型容量，请提高实际模型容量配置；未截断原文。')
        if current:
            groups.append(current)
        return groups

    def groups(self, run_id, items, field, context_builder=None):
        if not self.full_flow(run_id):
            return super().groups(run_id, items, field, context_builder)
        task = {'analysis': 'generate_scenarios', 'scenarios': 'generate_cases', 'cases': 'review_cases'}.get(field, 'analyze_requirement')
        builder = context_builder or (lambda values: self.with_evidence(run_id, {field: values}))
        return self.capacity_groups(task, items, builder)

    def progress(self, run_id, phase, completed, total, label):
        if self.full_flow(run_id):
            names = {'requirement_analysis':'理解需求与绘制业务图','scenario_generation':'生成测试场景','case_generation':'生成测试用例','case_review':'AI 审核与定向优化'}
            label = names.get(phase, label)
            if total > 1:
                label += f' · 容量分组 {min(completed + 1, total)}/{total}'
        return super().progress(run_id, phase, completed, total, label)

    def with_evidence(self, run_id, fields, include_changes=False):
        context = super().with_evidence(run_id, fields, include_changes)
        if self.full_flow(run_id):
            global_map = self.store.cache_get(run_id, 'v6:requirement_map')
            if global_map:
                context['global_requirement_map'] = global_map
        return context

    async def analyze(self, run_id, key, clarification=''):
        evidence = [e for e in self.all_evidence(run_id) if e['role'] != 'example']
        def build(values):
            return {**self.small_context(run_id, clarification=clarification,
                output_contract='返回items与report。整体理解业务规则，忽略封面、签署人和审批元数据。report包含summary,in_scope,out_of_scope,questions,assumptions,requirement_map,diagrams,strategy。diagrams包含业务流程图；复杂需求另含mindmap，存在生命周期则增加stateDiagram-v2。strategy包含建议depth与rationale。questions仅保留会影响下游的歧义。每条业务需求用精确refs引用原文。不要求逐个解释被忽略的元数据。'), 'evidence':values}
        groups = self.capacity_groups('analyze_requirement', evidence, build)
        items, reports = [], []
        self.stage(run_id, 'requirement_analysis')
        for index, group in enumerate(groups):
            self.progress(run_id, 'requirement_analysis', index, len(groups), '')
            refs = {e['id']: e for e in group}
            def validate(result):
                errors = item_errors('analysis', result.get('items'), refs)
                report = result.get('report')
                if not isinstance(report, dict):
                    return errors + [{'path':'report','code':'type','expected':'object'}]
                for field in ('questions','assumptions'):
                    if not isinstance(report.get(field, []), list) or not all(isinstance(x,str) for x in report.get(field, [])):
                        errors.append({'path':'report.'+field,'code':'type','expected':'string_array'})
                diagrams = report.get('diagrams', [])
                if not isinstance(diagrams,list) or not diagrams or not all(isinstance(d,dict) and isinstance(d.get('mermaid'),str) for d in diagrams):
                    errors.append({'path':'report.diagrams','code':'diagram','expected':'at least one business diagram with Mermaid source'})
                return errors
            result = await self.validated(run_id, f'{key}:{index}', 'analyze_requirement', build(group), validate)
            items.extend({**row,'id':f'R{index+1}-{row["id"]}'} for row in result['items'])
            reports.append(result['report'])
        if not items:
            raise DomainError('没有提取到业务规则。请补充功能需求；文档元数据不会作为业务需求。')
        report = dict(reports[0]) if len(reports)==1 else {'summary':'已按容量分组理解需求','segments':reports,'diagrams':[d for r in reports for d in r.get('diagrams',[])]}
        report['questions'] = list(dict.fromkeys(q for r in reports for q in r.get('questions',[])))
        report['assumptions'] = list(dict.fromkeys(q for r in reports for q in r.get('assumptions',[])))
        if self.store.run(run_id)['mode']=='auto':
            report['assumptions'] += ['待核实风险：'+q for q in report['questions']]
            report['questions'] = []
        self.store.cache_set(run_id, 'v6:requirement_map', {k:report[k] for k in ('summary','in_scope','out_of_scope','requirement_map','assumptions') if k in report})
        return self.store.artifact(run_id, key+':artifact', 'analysis','需求理解与业务图',items,report)

    async def node_analysis(self, state):
        if not self.full_flow(state['run_id']):
            return await super().node_analysis(state)
        artifact = await self.analyze(state['run_id'], 'v6:analysis')
        return {'analysis_ref':artifact['id'],'output_ref':artifact['id']}

    async def node_clarify(self, state):
        run_id=state['run_id']
        if not self.full_flow(run_id):
            return await super().node_clarify(state)
        run=self.store.run(run_id)
        if run['mode']=='auto':
            return {}
        analysis=self.store.get('artifact',state['analysis_ref'])
        round_index=0
        while analysis.get('report',{}).get('questions'):
            self.store.publish(run_id,[analysis['id']],'先核对我的需求理解与业务图，以下问题会影响后续场景。',waiting=True)
            answer=interrupt({'type':'clarification','artifact_id':analysis['id'],'questions':analysis['report']['questions']})
            key=f'v6:clarification:{round_index}'
            if not self.store.cache_get(run_id,key):
                text,chunks=parse_text(answer['answer'])
                with self.store.transaction():
                    source=self.store.add_source(run['chat_id'],'用户澄清','clarification',text,chunks)
                    current=self.store.run(run_id)
                    self.store.update_run(run_id,_source_ids=current['_source_ids']+[source['id']],_source_roles={**current['_source_roles'],source['id']:'clarification'})
                    self.store.cache_set(run_id,key,{'id':source['id']})
            analysis=await self.analyze(run_id,key+':analysis',answer['answer'])
            round_index+=1
        if state['intent']!='review_requirement':
            self.store.publish(run_id,[analysis['id']],'需求理解已整理。请确认范围、业务图和测试深度后生成场景。',waiting=True)
            response=interrupt({'type':'strategy_review','artifact_id':analysis['id'],'recommended_depth':analysis.get('report',{}).get('strategy',{}).get('depth','standard')})
            if response.get('approved') is not True:
                raise DomainError('请确认需求理解后继续。')
            if response.get('depth'):
                current=self.store.run(run_id)
                self.store.update_run(run_id,_profile=profile_config({**current['_profile'],'scenario_level':response['depth'],'case_level':response['depth']}),_confirmed_depth=response['depth'])
            analysis=self.store.get('artifact',analysis['id'])
            self.store.cache_set(run_id,'v6:requirement_map',{k:analysis.get('report',{})[k] for k in ('summary','in_scope','out_of_scope','requirement_map','assumptions') if k in analysis.get('report',{})})
        return {'analysis_ref':analysis['id'],'output_ref':analysis['id']}

    async def node_finish(self,state):
        if not self.full_flow(state['run_id']):
            return await super().node_finish(state)
        run_id=state['run_id']
        run=self.store.run(run_id)
        if run['status']=='completed':return {}
        artifact=self.store.get('artifact',state['output_ref'])
        reports=self.store.cache_get(run_id,'v4:review_reports') or []
        if state['intent']=='generate_case':
            content=f'已完成需求理解、场景设计、用例生成和一轮 AI 审核，共 {len(artifact["items"])} 条用例。用例尚未实际执行。\n接下来可以查看依据、修改选中项、调整 Profile 或按模板导出 Excel。'
        elif state['intent']=='learn_template':
            content='已整理 Excel 模板与写作规则建议。请选择更新当前 Profile、新建 Profile 或仅下一次运行使用。'
        else:
            content='已完成本次任务。可以继续提问、修改结果或进入下一阶段。'
        if reports:
            # Store review summary with the final revision without an extra model call.
            with self.store.transaction():
                artifact['report']={**artifact.get('report',{}),'summary':content,'review_reports':reports}
                self.store.put('artifact',artifact)
                from .storage import dump
                self.store.db.execute('UPDATE revisions SET payload=? WHERE artifact_id=? AND revision=?',(dump(artifact),artifact['id'],artifact['revision']))
        proposal=artifact.get('report',{}).get('config') if artifact['type']=='proposal' else None
        self.store.publish(run_id,[artifact['id']],content,proposal=proposal)
        self.store.update_run(run_id,progress={'phase':'completed','completed':1,'total':1,'label':'已完成并保存'})
        return {}
