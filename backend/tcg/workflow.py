"""Version 7 authoring graph: each durable transition has one responsibility."""
import hashlib
from typing import TypedDict
from langgraph.graph import StateGraph, START, END
from langgraph.types import interrupt
from .flow import FlowEngine
from .graph import State
from .schemas import DomainError, INTENTS, profile_config
from .documents import parse_text
from .storage import public, uid, now


class WorkflowState(State, total=False):
    clarification_answer: str
    clarification_questions: list[str]
    summary: str


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
                 'understanding_gate','scenarios','scenario_gate','cases','review',
                 'conversation','single','summarize','publish')
        for name in nodes:
            graph.add_node(name, self.observed_node(name))
        graph.add_edge(START,'dispatch')
        graph.add_conditional_edges('dispatch', lambda s: 'conversation' if s['intent']=='query' else 'single' if s['intent'] in ('modify','learn_template') else 'inputs')
        graph.add_conditional_edges('inputs', self.next_input)
        graph.add_conditional_edges('understand', self.next_understanding)
        graph.add_edge('clarification_gate','apply_answer')
        graph.add_edge('apply_answer','understanding_gate')
        graph.add_conditional_edges('understanding_gate',lambda s:'summarize' if s['intent']=='review_requirement' else 'scenarios')
        graph.add_conditional_edges('scenarios',lambda s:'scenario_gate' if s.get('scenario_ref') else 'scenarios')
        graph.add_conditional_edges('scenario_gate',lambda s:'summarize' if s['intent']=='generate_scenario' else 'cases')
        graph.add_conditional_edges('cases',lambda s:'review' if s.get('cases_ref') else 'cases')
        for name in ('review','conversation','single'):
            graph.add_edge(name,'summarize')
        graph.add_edge('summarize','publish')
        graph.add_edge('publish',END)
        return graph.compile(checkpointer=saver)

    async def invoke_model(self, task, context, run_id=None):
        if run_id and self.store.run(run_id).get('graph_version')==7:
            # The stage contract, not the eventual user goal, owns this response.
            names={'analyze_requirement':'review_requirement','generate_scenarios':'generate_scenario','generate_cases':'generate_case','review_cases':'review_case'}
            if task in names:
                context={**context,'user_goal':context.get('request',{}),
                    'request':{**context.get('request',{}),'intent':names[task]},
                    'current_stage':task,'stage_instruction':'只执行当前阶段的输出契约。最终用户目标不表示现在就生成最终用例或回答。'}
        return await super().invoke_model(task,context,run_id)

    async def paused_dialogue(self, run_id, content):
        run=self.store.run(run_id)
        if run['status']!='waiting':raise DomainError('当前任务未等待确认',409)
        context=self.context(run_id)
        aid=run.get('interrupt',{}).get('artifact_id')
        if aid:context['artifact']=public(self.store.get('artifact',aid))
        context['request']={**context['request'],'content':content,'intent':'query'}
        context['pending_confirmation']=run.get('interrupt',{})
        result=await self.invoke_model('dialogue',context,run_id)
        refs=result.get('refs',[])
        valid={e['id'] for e in context['evidence'] if e['role']!='example'}
        if not isinstance(result.get('answer'),str) or not isinstance(refs,list) or not set(refs)<=valid:
            raise DomainError('对话回答格式或引用无效，请重新提问；当前确认节点保留')
        with self.store.transaction():
            if self.store.run(run_id)['status']!='waiting':raise DomainError('任务已继续，请在最新结果上提问',409)
            for role,text in [('user',content),('assistant',result['answer'])]:
                self.store.put('message',{'id':uid('msg_'),'project_id':run['project_id'],'chat_id':run['chat_id'],'role':role,'content':text,'created_at':now(),'metadata':{'dialogue_run_id':run_id,'refs':refs if role=='assistant' else []}})
        return {'answer':result['answer']}

    async def node_dispatch(self,state):
        rid=state['run_id'];self.stage(rid,'routing')
        run=self.store.run(rid);intent=run['intent']
        if intent=='auto':
            result=await self.validated(rid,'v7:route','route',self.routing_context(rid),
                lambda r: [] if r.get('intent') in INTENTS and r['intent']!='auto' else [{'path':'intent','code':'intent','expected':sorted(set(INTENTS)-{'auto'})}])
            intent=result['intent']
        snapshot=run.get('_artifact_snapshot')
        if intent in ('modify','review_case'):
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
        artifact=await self.analyze(state['run_id'],'v7:analysis')
        return {'analysis_ref':artifact['id'],'output_ref':artifact['id']}

    def next_understanding(self,state):
        run=self.store.run(state['run_id']);artifact=self.store.get('artifact',state['analysis_ref'])
        return 'clarification_gate' if run['mode']=='hitp' and artifact.get('report',{}).get('questions') else 'understanding_gate'

    async def node_clarification_gate(self,state):
        rid=state['run_id'];self.stage(rid,'clarification')
        artifact=self.store.get('artifact',state['analysis_ref']);questions=artifact['report']['questions']
        self.store.publish(rid,[artifact['id']],'已整理需求理解和业务图。请回答会影响测试设计的问题。',waiting=True)
        response=interrupt({'type':'clarification','artifact_id':artifact['id'],'questions':questions})
        return {'clarification_answer':response['answer'],'clarification_questions':questions}

    async def node_apply_answer(self,state):
        rid=state['run_id'];run=self.store.run(rid);self.stage(rid,'applying_clarification')
        # Questions travel with answers so terse replies retain their meaning.
        answer='问题：\n'+'\n'.join(state['clarification_questions'])+'\n用户回答：\n'+state['clarification_answer']
        saved=self.store.cache_get(rid,'v7:clarification_source')
        if not saved:
            text,chunks=parse_text(answer)
            with self.store.transaction():
                source=self.store.add_source(run['chat_id'],'用户澄清','clarification',text,chunks)
                saved=self.store.cache_set(rid,'v7:clarification_source',{'id':source['id']})
        self.store.update_run(rid,_source_ids=list(dict.fromkeys(run['_source_ids']+[saved['id']])),_source_roles={**run['_source_roles'],saved['id']:'clarification'})
        artifact=await self.analyze(rid,'v7:clarified',answer)
        return {'analysis_ref':artifact['id'],'output_ref':artifact['id']}

    async def node_understanding_gate(self,state):
        rid=state['run_id'];run=self.store.run(rid)
        if run['mode']=='hitp':
            self.stage(rid,'strategy_review')
            artifact=self.store.get('artifact',state['analysis_ref'])
            self.store.publish(rid,[artifact['id']],'请确认需求理解、业务图和测试方案。剩余不确定项可在此修订，不会自动反复追问。',waiting=True)
            response=interrupt({'type':'strategy_review','artifact_id':artifact['id'],
                'recommended_depth':artifact.get('report',{}).get('strategy',{}).get('depth','standard')})
            if response.get('approved') is not True:raise DomainError('请确认理解与方案')
            if response.get('depth'):
                self.store.update_run(rid,_profile=profile_config({**run['_profile'],'scenario_level':response['depth'],'case_level':response['depth']}))
        artifact=self.store.get('artifact',state['analysis_ref'])
        self.store.cache_set(rid,'v6:requirement_map',{**artifact.get('report',{}),'confirmed_requirements':artifact['items']})
        return {'output_ref':artifact['id']}

    async def node_review(self,state):
        if self.store.run(state['run_id']).get('graph_version')==7 and not state.get('cases_ref'):
            snapshot=self.store.run(state['run_id']).get('_artifact_snapshot')
            state={**state,'cases_ref':snapshot['id']}
        return await super().node_review(state)

    async def node_conversation(self,state):
        rid=state['run_id'];self.stage(rid,'query');context=self.context(rid)
        context['instruction']='允许解释系统流程或当前产物，无需求时也能对话。只有业务事实需要引用证据，不得编造业务规则。'
        valid_refs={e['id'] for e in context['evidence'] if e['role']!='example'}
        result=await self.validated(rid,'v7:conversation','dialogue',context,
            lambda r: [] if isinstance(r.get('answer'),str) and isinstance(r.get('refs',[]),list) and set(r.get('refs',[]))<=valid_refs else [{'path':'answer/refs','code':'answer','expected':'text and valid optional refs'}])
        artifact=self.answer(rid,'v7:answer',result['answer'],result.get('refs',[]))
        return {'output_ref':artifact['id']}

    async def node_single(self,state):
        if self.store.run(state['run_id']).get('graph_version')==7 and state['intent']=='modify' and not self.store.run(state['run_id']).get('_artifact_snapshot'):
            artifact=self.answer(state['run_id'],'v7:no_target','当前还没有可修改的结果。请先生成或上传用例。')
            return {'output_ref':artifact['id']}
        return await super().node_single(state)

    async def node_summarize(self,state):
        rid=state['run_id'];self.stage(rid,'summarizing');artifact=self.store.get('artifact',state['output_ref'])
        if artifact['type'] in ('answer','proposal'):
            return {'summary':'\n'.join(i.get('description','') for i in artifact['items'])}
        reports=self.store.cache_get(rid,'v4:review_reports') or []
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
        title=f'已保存{labels.get(artifact["type"],artifact["type"])}，共 {len(artifact["items"])} 条。'
        if artifact['type']=='cases':title+='已完成一轮 AI 评审。用例尚未实际执行。' if reports else '用例尚未实际执行。'
        self.store.cache_set(rid,'v7:summary',title+'\n'+narrative)
        return {'summary':title+'\n'+narrative}

    async def node_publish(self,state):
        rid=state['run_id'];artifact=self.store.get('artifact',state['output_ref'])
        content=state.get('summary') or self.store.cache_get(rid,'v7:summary') or '已保存当前结果。'
        reports=self.store.cache_get(rid,'v4:review_reports') or []
        if reports and not artifact.get('report',{}).get('review_reports'):
            from .storage import dump
            with self.store.transaction():
                artifact['report']={**artifact.get('report',{}),'review_reports':reports}
                self.store.put('artifact',artifact)
                self.store.db.execute('UPDATE revisions SET payload=? WHERE artifact_id=? AND revision=?',(dump(artifact),artifact['id'],artifact['revision']))
        self.store.publish(rid,[artifact['id']],content,proposal=artifact.get('report',{}).get('config') if artifact['type']=='proposal' else None)
        self.store.update_run(rid,progress={'phase':'completed','completed':1,'total':1,'label':'结果已保存'})
        return {}

    async def node_finish(self,state):
        if self.store.run(state['run_id']).get('graph_version')==7:
            if self.store.run(state['run_id'])['status']!='completed':return await self.node_publish(state)
            return {}
        return await super().node_finish(state)
