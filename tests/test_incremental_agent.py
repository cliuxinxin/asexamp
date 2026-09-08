"""Behavioral tests run the real API, graph and SQLite with a controlled provider."""
import copy
import json
import threading
import asyncio

from fastapi.testclient import TestClient

from tcg.main import create_app
from tcg.schemas import DomainError
from test_backend_api import setup_chat, start, until


class WorkModel:
    def __init__(self, questions=False, fail_analysis=0, need_context=False):
        self.calls = []
        self.questions, self.fail_analysis = questions, fail_analysis
        self.need_context = need_context
        self.analysis_count = 0
        self.block_task = None
        self.entered, self.release = threading.Event(), threading.Event()

    async def generate(self, task, context):
        self.calls.append((task, copy.deepcopy(context)))
        if task == self.block_task and not self.release.is_set():
            self.entered.set()
            while not self.release.is_set():
                await asyncio.sleep(.01)
        evidence = [e for e in context.get('evidence', []) if e['role'] != 'example']
        refs = [e['id'] for e in evidence]
        if task == 'work_route':
            return {'intent': 'generate_case', 'depth': 'standard', 'classification': 'instruction'}
        if task == 'work_analyze':
            self.analysis_count += 1
            if self.analysis_count == self.fail_analysis:
                error = DomainError('Controlled second-unit failure')
                error.retryable = False
                raise error
            return {'kind': 'patch', 'items': [{'id': f'R{i}', 'title': e['text'][:35], 'description': e['text'], 'refs': [e['id']]} for i,e in enumerate(evidence)],
                    'nodes': [{'id': 'entry', 'label': '输入', 'refs': refs}, {'id': 'result', 'label': '结果', 'refs': refs}],
                    'edges': [{'id': 'flow', 'from': 'entry', 'to': 'result', 'label': '校验规则', 'refs': refs}],
                    'summary': '已提取本组业务规则。', 'questions': ['锁定时长尚未明确'] if self.questions else [],
                    'assumptions': [], 'techniques': ['等价类', '边界值'], 'depth': context['depth'], 'evidence_review': []}
        if task == 'work_scenarios':
            return {'kind': 'patch', 'items': [{'id': f'S{i}', 'title': r['title'], 'description': '验证规则的有效和无效输入', 'priority': 'P1',
                    'refs': r['refs'], 'requirement_ids': [r['id']], 'branch_ids': [b['id'] for b in context['business_model']['edges']]} for i,r in enumerate(context['analysis'])],
                    'has_more': False, 'summary': '已保存本组测试场景。'}
        if task == 'work_cases':
            return {'kind': 'patch', 'items': [{'id': f'C{i}', 'title': s['title'], 'scenario_id': s['id'], 'type': 'Business', 'priority': 'P1',
                    'preconditions': '已准备规则中明确的输入', 'steps': [{'action': '提交符合要求的数据', 'expected': '按已引用规则处理'}],
                    'refs': s['refs'], 'requirement_ids': s['requirement_ids'], 'branch_ids': s['branch_ids']} for i,s in enumerate(context['scenarios'])],
                    'has_more': False, 'summary': '已保存本组用例草稿。'}
        if task == 'work_review':
            return {'kind': 'patch', 'operations': [], 'report': {'summary': '已检查本组规则、步骤与引用。', 'issues': [], 'score': 85, 'type_assessment':[{'type':t,'applicable':t=='Business','reason':'Controlled fixture checks Business coverage; other types are explicitly out of this fixture scope.'} for t in context.get('case_types',[])]}}
        if task == 'work_links':
            return {'kind': 'patch', 'edges': [], 'summary': '未发现需要补充的跨组关联。', 'questions': []}
        if task == 'work_query':
            if self.need_context and not context.get('observations'):
                return {'kind': 'need_context', 'requests': [{'tool': 'search_evidence', 'query': '退款窗口'}], 'summary': '需要查阅退款规则。'}
            if self.need_context and context['observations'][-1]['tool'] == 'search_evidence':
                matches = context['observations'][-1]['result']['matches']
                return {'kind': 'need_context', 'requests': [{'tool': 'read_evidence', 'refs': [m['id'] for m in matches]}], 'summary': '读取命中的规则。'}
            return {'kind': 'answer', 'answer': '退款窗口为 30 天。', 'refs': [e['id'] for e in evidence if '退款' in e.get('text', '')]}
        if task == 'work_impact':
            candidates = context['units']
            selected = [u['id'] for u in candidates if any('锁定' in r.get('title','') for r in u.get('requirements', []))]
            return {'unit_ids': selected, 'global_change': False, 'summary': '修改只影响账户锁定规则。'}
        if task == 'agent_feedback':
            return {'proceed': True, 'has_changes': False}
        raise AssertionError(task)


def test_small_auto_generation_has_one_review_without_repeated_planning(tmp_path):
    model = WorkModel(questions=True)
    with TestClient(create_app(tmp_path, model)) as client:
        _, chat, _ = setup_chat(client)
        run = until(client, start(client, chat, experience='agent', mode='auto', confirm_strategy=False))
        assert run['status'] == 'completed', run.get('error', run)
        assert run['graph_version'] == 3
        tasks = [t for t,_ in model.calls]
        assert tasks.count('work_review') == 1
        assert len(tasks) <= 5
        assert not {'agent_plan', 'agent_summary'}.intersection(tasks)
        assert '锁定时长' in run['agent']['summary']
        artifact = client.get('/api/artifacts/' + run['artifact_ids'][-1]).json()
        assert artifact['report']['review']['summary']
        assert not artifact['report']['coverage']['gaps']


def test_completed_work_is_visible_before_later_document_analysis_finishes(tmp_path):
    model = WorkModel(fail_analysis=2)
    with TestClient(create_app(tmp_path, model)) as client:
        _, chat, _ = setup_chat(client, text='登录密码必须校验。' * 160)
        client.post('/api/chats/' + chat['id'] + '/sources/text', json={'name': '退款模块', 'role': 'primary', 'text': '退款窗口为30天。' * 160}).raise_for_status()
        run = until(client, start(client, chat, experience='agent', mode='auto', confirm_strategy=False))
        assert run['status'] == 'failed'
        assert run['agent']['preview_ids']
        work = client.get('/api/runs/' + run['id'] + '/work').json()
        assert work['completed'] >= 1
        completed_before = [t for t,c in model.calls if t == 'work_analyze' and '登录' in json.dumps(c)]
        model.fail_analysis = 0
        client.post('/api/runs/' + run['id'] + '/retry').raise_for_status()
        run = until(client, run)
        assert run['status'] == 'completed', run.get('error', run)
        assert len([t for t,c in model.calls if t == 'work_analyze' and '登录' in json.dumps(c)]) == len(completed_before)


def test_current_work_can_search_and_read_without_global_planner(tmp_path):
    model = WorkModel(need_context=True)
    with TestClient(create_app(tmp_path, model)) as client:
        _, chat, _ = setup_chat(client, text='退款窗口为30天。')
        run = until(client, start(client, chat, experience='agent', intent='query', content='退款窗口是多少？', confirm_strategy=False))
        assert run['status'] == 'completed', run.get('error', run)
        assert {t for t,_ in model.calls} == {'work_query'}
        assert len(model.calls) == 3
        assert any('读取' in i['summary'] for i in run['agent']['insights'])
        assert '30' in client.get('/api/artifacts/' + run['artifact_ids'][-1]).json()['items'][0]['description']


def test_confirmed_continuation_does_not_reanalyze(tmp_path):
    model = WorkModel(questions=True)
    with TestClient(create_app(tmp_path, model)) as client:
        _, chat, _ = setup_chat(client)
        run = until(client, start(client, chat, experience='agent', mode='hitp', confirm_strategy=True))
        assert run['status'] == 'waiting'
        before = model.analysis_count
        client.post('/api/runs/' + run['id'] + '/resume', json={'proceed': True}).raise_for_status()
        run = until(client, run)
        if run['status'] == 'waiting':
            assert run['interrupt']['type'] == 'scenario_review'
            client.post('/api/runs/' + run['id'] + '/resume', json={'approved': True}).raise_for_status()
            run = until(client, run)
        assert run['status'] == 'completed', run.get('error', run)
        assert model.analysis_count == before


def test_run_diagnostics_identify_loaded_runtime_version(tmp_path):
    with TestClient(create_app(tmp_path, WorkModel())) as client:
        health = client.get('/api/health').json()
        assert health['runtime']['graph_version'] == 3
        assert health['runtime']['code_fingerprint']
        _,chat,_=setup_chat(client)
        run=until(client,start(client,chat,experience='agent',confirm_strategy=False))
        diagnostics=client.get('/api/runs/'+run['id']+'/diagnostics').json()
        assert diagnostics['runtime']['code_fingerprint'] == health['runtime']['code_fingerprint']


def test_pause_is_checkpointed_and_resume_does_not_repeat_work(tmp_path):
    model = WorkModel()
    model.block_task = 'work_analyze'
    with TestClient(create_app(tmp_path, model)) as client:
        _,chat,_ = setup_chat(client)
        run = start(client,chat,experience='agent',confirm_strategy=False)
        assert model.entered.wait(4)
        paused = client.post('/api/runs/'+run['id']+'/pause')
        paused.raise_for_status()
        model.release.set()
        run=until(client,run)
        assert run['status']=='waiting' and run['interrupt']['type']=='work_pause'
        client.post('/api/runs/'+run['id']+'/resume',json={'proceed':True}).raise_for_status()
        run=until(client,run)
        assert run['status']=='completed',run.get('error')
        assert model.analysis_count==1
        assert not run['agent'].get('pause_requested')


def test_work_previews_are_read_only_and_do_not_become_edit_targets(tmp_path):
    model=WorkModel(fail_analysis=2)
    with TestClient(create_app(tmp_path,model)) as client:
        _,chat,_=setup_chat(client)
        client.post('/api/chats/'+chat['id']+'/sources/text',json={'name':'第二部分','role':'primary','text':'订单允许退款。'}).raise_for_status()
        run=until(client,start(client,chat,experience='agent',confirm_strategy=False))
        preview_id=run['agent']['preview_ids'][0]
        preview=client.get('/api/artifacts/'+preview_id).json()
        assert preview['preview'] is True
        assert client.put('/api/artifacts/'+preview_id,json={'expected_revision':1,'items':preview['items']}).status_code==404
        assert preview_id not in run['artifact_ids']


def test_auto_routing_and_explicit_depth_use_one_combined_call(tmp_path):
    model=WorkModel()
    with TestClient(create_app(tmp_path,model)) as client:
        _,chat,_=setup_chat(client)
        run=until(client,start(client,chat,intent='auto',experience='agent',depth='deep',confirm_strategy=False))
        assert run['status']=='completed',run.get('error')
        assert [t for t,c in model.calls].count('work_route')==1
        assert len(model.calls)<=5
        assert all(c['depth']=='deep' for t,c in model.calls if t!='work_route')


def test_long_document_tail_is_processed_and_generation_does_not_resend_source(tmp_path):
    model=WorkModel()
    text='密码必须至少六位。\n'*160+'\n末尾独有规则：管理员退出后立即使会话失效。'
    with TestClient(create_app(tmp_path,model)) as client:
        _,chat,_=setup_chat(client,text=text)
        run=until(client,start(client,chat,experience='agent',confirm_strategy=False))
        assert run['status']=='completed',run.get('error')
        analysis_calls=[c for t,c in model.calls if t=='work_analyze']
        assert '管理员退出' in ''.join(e['text'] for c in analysis_calls for e in c['evidence'])
        assert all(len(json.dumps(c,ensure_ascii=False))<=16000 for t,c in model.calls)
        assert all('text' not in e for t,c in model.calls if t in ('work_scenarios','work_cases','work_review') for e in c['evidence'])
        final=client.get('/api/artifacts/'+run['artifact_ids'][-1]).json()
        assert any('管理员退出' in r['description'] for r in final['report']['requirements'])
        assert not final['report']['coverage']['gaps']


def test_repeated_tool_request_fails_actionably_instead_of_spinning(tmp_path):
    class Repeating(WorkModel):
        async def generate(self,task,context):
            self.calls.append((task,copy.deepcopy(context)))
            return {'kind':'need_context','requests':[{'tool':'search_evidence','query':'退款'}],'summary':'查询退款'}
    model=Repeating()
    with TestClient(create_app(tmp_path,model)) as client:
        _,chat,_=setup_chat(client)
        run=until(client,start(client,chat,intent='query',experience='agent',confirm_strategy=False))
        assert run['status']=='failed'
        assert '重复请求' in run['error']
        assert len(model.calls)==2


def test_out_of_scope_tool_reference_is_rejected(tmp_path):
    class Stranger(WorkModel):
        async def generate(self,task,context):
            return {'kind':'need_context','requests':[{'tool':'read_evidence','refs':['secret-from-another-chat']}],'summary':'读文件'}
    with TestClient(create_app(tmp_path,Stranger())) as client:
        _,chat,_=setup_chat(client)
        run=until(client,start(client,chat,intent='query',experience='agent',confirm_strategy=False))
        assert run['status']=='failed'
        assert 'authorized_evidence' in run['error']


def test_restart_retains_accepted_work(tmp_path):
    model=WorkModel()
    model.block_task='work_cases'
    app=create_app(tmp_path,model)
    with TestClient(app) as client:
        _,chat,_=setup_chat(client)
        run=start(client,chat,experience='agent',confirm_strategy=False)
        assert model.entered.wait(4)
    model.block_task=None
    with TestClient(create_app(tmp_path,model)) as client:
        run=until(client,run)
        assert run['status']=='completed',run.get('error')
        assert model.analysis_count==1
        assert [t for t,c in model.calls].count('work_scenarios')==1


def test_field_repair_preserves_other_items_and_does_not_resend_response(tmp_path):
    class Broken(WorkModel):
        async def generate(self,task,context):
            if task=='agent_repair':
                self.calls.append((task,copy.deepcopy(context)))
                return {'path':context['repair']['path'],'value':'可以提交有效数据'}
            value=await super().generate(task,context)
            if task=='work_cases':
                value['items'][0]['preconditions']=['wrong wrapper']
            return value
    model=Broken()
    with TestClient(create_app(tmp_path,model)) as client:
        _,chat,_=setup_chat(client)
        run=until(client,start(client,chat,experience='agent',confirm_strategy=False))
        assert run['status']=='completed',run.get('error')
        repairs=[c for t,c in model.calls if t=='agent_repair']
        assert len(repairs)==1 and repairs[0]['repair']['path']=='items[0].preconditions'
        assert 'items' not in repairs[0] and 'evidence' not in repairs[0]


def test_changed_instruction_only_recomputes_affected_module(tmp_path):
    model=WorkModel()
    model.block_task='work_review'
    with TestClient(create_app(tmp_path,model)) as client:
        _,chat,_=setup_chat(client,text='账户锁定规则：连续失败4次锁定30分钟。')
        client.post('/api/chats/'+chat['id']+'/sources/text',json={'name':'退款','role':'primary','text':'退款窗口30天。'}).raise_for_status()
        run=start(client,chat,experience='agent',confirm_strategy=False)
        assert model.entered.wait(4)
        client.post('/api/runs/'+run['id']+'/instructions',json={'content':'锁定阈值改为5次；退款规则不变。'}).raise_for_status()
        model.release.set()
        run=until(client,run)
        assert run['status']=='completed',run.get('error')
        refund_analyses=[c for t,c in model.calls if t=='work_analyze' and c['evidence'][0]['text']=='退款窗口30天。']
        assert len(refund_analyses)==1
        assert any('5次' in json.dumps(c,ensure_ascii=False) for t,c in model.calls if t=='work_analyze')


def test_query_can_read_tail_of_oversized_paragraph(tmp_path):
    class Tail(WorkModel):
        async def generate(self,task,context):
            self.calls.append((task,copy.deepcopy(context)))
            if not context.get('observations'):
                return {'kind':'need_context','requests':[{'tool':'list_sections','cursor':0}],'summary':'先定位段落'}
            if context['observations'][-1]['tool']=='list_sections':
                ref=context['observations'][-1]['result']['sections'][0]['id']
                return {'kind':'need_context','requests':[{'tool':'read_evidence','refs':[ref],'offset':4000}],'summary':'读取段落末尾'}
            e=context['evidence'][0]
            assert '尾部答案' in e['text']
            return {'kind':'answer','answer':'尾部答案为7天。','refs':[e['id']]}
    with TestClient(create_app(tmp_path,Tail())) as client:
        _,chat,source=setup_chat(client,text='placeholder')
        client.delete('/api/sources/'+source['id']).raise_for_status()
        body='前文'*2100+'尾部答案为7天。'
        client.app.state.store.add_source(chat['id'],'长段落','primary',body,[{'text':body,'location':'P1'}])
        run=until(client,start(client,chat,intent='query',experience='agent',confirm_strategy=False))
        assert run['status']=='completed',run.get('error')


def test_second_run_reuses_unchanged_work_but_performs_its_review(tmp_path):
    model=WorkModel()
    with TestClient(create_app(tmp_path,model)) as client:
        _,chat,_=setup_chat(client)
        first=until(client,start(client,chat,experience='agent',confirm_strategy=False))
        assert first['status']=='completed',first.get('error')
        before=len(model.calls)
        second=until(client,start(client,chat,experience='agent',confirm_strategy=False))
        assert second['status']=='completed',second.get('error')
        assert [t for t,c in model.calls[before:]]==['work_review']


def test_selected_modify_preserves_unselected_case_and_revision_guard(tmp_path):
    class Editing(WorkModel):
        async def generate(self,task,context):
            if task=='work_modify':
                self.calls.append((task,copy.deepcopy(context)))
                assert len(context['items'])==1
                return {'kind':'patch','operations':[{'op':'update','id':context['items'][0]['id'],'item':{'title':'仅修改选定标题'}}],'summary':'标题已更新。'}
            return await super().generate(task,context)
    model=Editing()
    with TestClient(create_app(tmp_path,model)) as client:
        _,chat,_=setup_chat(client,text='密码至少6位。\n\n登录成功进入首页。')
        first=until(client,start(client,chat,experience='agent',confirm_strategy=False))
        assert first['status']=='completed',first.get('error')
        artifact=client.get('/api/artifacts/'+first['artifact_ids'][-1]).json()
        assert len(artifact['items'])==2
        untouched=copy.deepcopy(artifact['items'][1:])
        run=until(client,start(client,chat,experience='agent',intent='modify',artifact_id=artifact['id'],selected_ids=[artifact['items'][0]['id']],content='把选定标题改成：仅修改选定标题'))
        assert run['status']=='completed',run.get('error')
        updated=client.get('/api/artifacts/'+artifact['id']).json()
        assert updated['revision']==artifact['revision']+1
        assert updated['items'][0]['title']=='仅修改选定标题'
        assert updated['items'][1:]==untouched


def test_source_read_does_not_deserialize_other_source_bodies(tmp_path, monkeypatch):
    with TestClient(create_app(tmp_path,WorkModel())) as client:
        _,chat,source=setup_chat(client,text='Selected body')
        client.post('/api/chats/'+chat['id']+'/sources/text',json={'name':'Unselected','role':'primary','text':'UNSELECTED_PRIVATE_BODY'}).raise_for_status()
        import tcg.storage as storage
        original=storage.json.loads
        loaded=[]
        def track(value,*args,**kwargs):
            loaded.append(value)
            return original(value,*args,**kwargs)
        monkeypatch.setattr(storage.json,'loads',track)
        client.app.state.store.evidence([source['id']])
        assert all('UNSELECTED_PRIVATE_BODY' not in value for value in loaded)


def test_missing_case_is_repaired_before_the_single_review(tmp_path):
    class Missing(WorkModel):
        async def generate(self,task,context):
            value=await super().generate(task,context)
            if task=='work_cases' and not context.get('coverage_gaps'):
                value['items']=[]
            return value
    model=Missing()
    with TestClient(create_app(tmp_path,model)) as client:
        _,chat,_=setup_chat(client)
        run=until(client,start(client,chat,experience='agent',confirm_strategy=False))
        assert run['status']=='completed',run.get('error')
        assert [t for t,c in model.calls].count('work_cases')==2
        assert [t for t,c in model.calls].count('work_review')==1


def test_review_cannot_delete_only_coverage_and_retry_keeps_generation(tmp_path):
    class Deleting(WorkModel):
        bad=True
        async def generate(self,task,context):
            value=await super().generate(task,context)
            if task=='work_review' and self.bad:
                value['operations']=[{'op':'delete','id':context['items'][0]['id']}]
            return value
    model=Deleting()
    with TestClient(create_app(tmp_path,model)) as client:
        _,chat,_=setup_chat(client)
        run=until(client,start(client,chat,experience='agent',confirm_strategy=False))
        assert run['status']=='failed' and not run['artifact_ids']
        model.bad=False
        client.post('/api/runs/'+run['id']+'/retry').raise_for_status()
        run=until(client,run)
        assert run['status']=='completed',run.get('error')
        assert [t for t,c in model.calls].count('work_cases')==1


def test_import_review_covers_every_source_unit(tmp_path):
    class Imported(WorkModel):
        async def generate(self,task,context):
            if task=='work_import':
                self.calls.append((task,copy.deepcopy(context)))
                return {'kind':'patch','items':[{'id':str(i),'title':e['text'][:35],'scenario_id':'','type':'Business','priority':'P1','preconditions':'',
                    'steps':[{'action':'提交输入','expected':'业务规则结果'}],'refs':[e['id']]} for i,e in enumerate(context['evidence'])],
                    'has_more':False,'summary':'提取了当前用例。'}
            return await super().generate(task,context)
    model=Imported()
    with TestClient(create_app(tmp_path,model)) as client:
        _,chat,_=setup_chat(client,text='用例1：提交有效密码，应允许登录。\n\n用例2：提交错误密码，应拒绝登录。')
        run=until(client,start(client,chat,intent='review_case',experience='agent',confirm_strategy=False))
        assert run['status']=='completed',run.get('error')
        artifact=client.get('/api/artifacts/'+run['artifact_ids'][-1]).json()
        assert len(artifact['items'])==2
        assert [t for t,c in model.calls]==['work_import','work_review']


def test_template_tools_can_read_examples_without_promoting_business_evidence(tmp_path):
    class Template(WorkModel):
        async def generate(self,task,context):
            if not context.get('evidence'):
                if not context.get('observations'):
                    return {'kind':'need_context','requests':[{'tool':'list_sections','cursor':0}],'summary':'查看格式示例目录'}
                ref=context['observations'][0]['result']['sections'][0]['id']
                return {'kind':'need_context','requests':[{'tool':'read_evidence','refs':[ref]}],'summary':'读取格式示例'}
            assert context['evidence'][0]['role']=='example'
            return {'kind':'patch','config':{'additional_rules':'步骤需要编号'},'summary':'只提取编号格式。'}
    with TestClient(create_app(tmp_path,Template())) as client:
        _,chat,source=setup_chat(client)
        client.delete('/api/sources/'+source['id']).raise_for_status()
        client.post('/api/chats/'+chat['id']+'/sources/text',json={'name':'样式','role':'example','text':'1. 提交输入\n2. 检查结果'}).raise_for_status()
        run=until(client,start(client,chat,intent='learn_template',experience='agent',confirm_strategy=False))
        assert run['status']=='completed',run.get('error')
        artifact=client.get('/api/artifacts/'+run['artifact_ids'][-1]).json()
        assert artifact['type']=='proposal'
        assert artifact['report']['config']['additional_rules']=='步骤需要编号'


def test_later_rule_updates_earlier_draft_before_review(tmp_path):
    class Dependency(WorkModel):
        async def generate(self,task,context):
            if task=='work_links':
                self.calls.append((task,copy.deepcopy(context)))
                early=next(u for u in context['units'] if '登录' in u['title'])
                late=next(u for u in context['units'] if '安全' in u['title'])
                refs=[r for i in late['requirements'] for r in i['refs']]
                return {'kind':'patch','edges':[],'updates':[{'unit_id':early['id'],'evidence_refs':refs,'summary':'安全规则限制登录失败次数'}],
                    'summary':'发现登录依赖安全策略。','questions':[]}
            value=await super().generate(task,context)
            if task=='work_analyze' and len(context['evidence'])>1:
                value['items']=[{'id':'combined','title':'登录受安全策略限制','description':'失败5次锁定；原无上限假设失效。','refs':[e['id'] for e in context['evidence']]}]
            return value
    model=Dependency()
    with TestClient(create_app(tmp_path,model)) as client:
        _,chat,source=setup_chat(client,text='登录允许用户输入密码；失败次数未定义。')
        source_value=client.app.state.store.get('source',source['id'])
        source_value['name']='登录'
        client.app.state.store.put('source',source_value)
        client.post('/api/chats/'+chat['id']+'/sources/text',json={'name':'安全策略','role':'supplement','text':'所有登录入口失败5次后锁定。'}).raise_for_status()
        run=until(client,start(client,chat,experience='agent',confirm_strategy=False))
        assert run['status']=='completed',run.get('error')
        artifact=client.get('/api/artifacts/'+run['artifact_ids'][-1]).json()
        assert any('登录受安全' in r['title'] for r in artifact['report']['requirements'])
        assert not any('失败次数未定义' in r['description'] for r in artifact['report']['requirements'])
        assert [t for t,c in model.calls].count('work_analyze')==3
        first_review=next(i for i,(t,c) in enumerate(model.calls) if t=='work_review')
        assert all(t!='work_analyze' for t,c in model.calls[first_review:])


def test_cross_unit_path_receives_its_own_cases_and_review(tmp_path):
    class Linked(WorkModel):
        async def generate(self,task,context):
            if task=='work_links':
                self.calls.append((task,copy.deepcopy(context)))
                a,b=context['units'][:2]
                return {'kind':'patch','summary':'审批后才能退款。','questions':[],
                    'edges':[{'id':'cross','from':a['nodes'][-1]['id'],'to':b['nodes'][0]['id'],'label':'审批通过才允许退款','refs':a['requirements'][0]['refs']+b['requirements'][0]['refs']}]}
            return await super().generate(task,context)
    model=Linked()
    with TestClient(create_app(tmp_path,model)) as client:
        _,chat,_=setup_chat(client,text='审批单通过后进入退款环节。')
        client.post('/api/chats/'+chat['id']+'/sources/text',json={'name':'退款','role':'primary','text':'退款需要已通过的审批单。'}).raise_for_status()
        run=until(client,start(client,chat,experience='agent',confirm_strategy=False))
        assert run['status']=='completed',run.get('error')
        artifact=client.get('/api/artifacts/'+run['artifact_ids'][-1]).json()
        assert any('审批通过才允许退款' in i['title'] for i in artifact['items'])
        assert not artifact['report']['coverage']['gaps']
        assert [t for t,c in model.calls].count('work_review')==3


def test_read_budget_can_resume_without_repeating_model_calls(tmp_path):
    class Reading(WorkModel):
        async def generate(self,task,context):
            self.calls.append((task,copy.deepcopy(context)))
            number=len(self.calls)
            if number<=9:
                return {'kind':'need_context','requests':[{'tool':'search_evidence','query':f'退款规则{number}'}],'summary':'用不同关键词定位规则'}
            return {'kind':'answer','answer':'这些查询尚不足以确认所问规则。','refs':[]}
    model=Reading()
    with TestClient(create_app(tmp_path,model)) as client:
        _,chat,_=setup_chat(client)
        run=until(client,start(client,chat,experience='agent',intent='query',confirm_strategy=False))
        assert run['status']=='waiting' and run['interrupt']['reason']=='context_budget'
        assert len(model.calls)==9
        client.post('/api/runs/'+run['id']+'/resume',json={'proceed':True}).raise_for_status()
        run=until(client,run)
        assert run['status']=='completed',run.get('error')
        assert len(model.calls)==10


def test_relevant_simple_question_uses_one_call(tmp_path):
    model=WorkModel()
    with TestClient(create_app(tmp_path,model)) as client:
        _,chat,_=setup_chat(client,text='退款窗口为30天。')
        run=until(client,start(client,chat,experience='agent',intent='query',content='退款窗口是多少天？',confirm_strategy=False))
        assert run['status']=='completed',run.get('error')
        assert len(model.calls)==1
        assert model.calls[0][1]['evidence'][0]['text']=='退款窗口为30天。'
        artifact=client.get('/api/artifacts/'+run['artifact_ids'][-1]).json()
        assert artifact['items'][0]['refs']
