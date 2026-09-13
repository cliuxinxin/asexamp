"""Exercise template-driven filling through the real graph, persistence and XLSX writer."""
import copy
import io
from fastapi.testclient import TestClient
from openpyxl import load_workbook
from tcg.main import create_app
from test_workflow_v25 import FlowModel
from test_backend_api import setup_chat, start, until

COLUMNS = [
    {'field':'title','header':'用例名称'},
    {'field':'validation_goal','header':'验证目的','definition':'说明本用例验证的业务行为'},
    {'field':'test_data','header':'输入数据','definition':'描述输入数据的条件，不编造真实账号'},
    {'field':'expected','header':'检查点'},
    {'field':'actual_result','header':'实际结果','value_source':'manual'},
    {'field':'execution_status','header':'执行状态','value_source':'default','default_value':'未执行'},
    {'field':'retry_limit','header':'次数','value_source':'default','default_value':0},
    {'field':'risk_note','header':'风险说明','value_source':'ai','required':False},
]

class TemplateModel(FlowModel):
    def __init__(self):
        super().__init__()
        self.missing_fact=False
        self.reject_first=False

    async def generate(self, task, context):
        if task=='learn_template':
            self.calls.append((task,copy.deepcopy(context)))
            return {'config':{'excel_columns':COLUMNS,'sheet_name':'我的模板'},'summary':'保留自定义字段定义；实际结果留给人工填写。'}
        if task=='complete_case_fields':
            self.calls.append((task,copy.deepcopy(context)))
            values={'validation_goal':'验证已有账号能使用正确凭证登录。','test_data':'使用有效凭证，不包含真实密码。','description':'验证已有账号通过正确凭证进入首页。','new_check':'登录后检查已认证状态。'}
            records=[]
            for row in context['cases']:
                fields={};unresolved={}
                for field in context['missing_fields'][row['id']]:
                    if self.missing_fact and field=='validation_goal':unresolved[field]='需要补充该检查项目的业务定义。'
                    else:fields[field]=values[field]
                if self.reject_first and 'validation_repair' not in context:fields['title']='不允许覆盖标题'
                records.append({'id':row['id'],'fields':fields,'unresolved':unresolved})
            return {'items':records}
        result=await super().generate(task,context)
        if task=='generate_cases':
            # A model can accidentally invent execution results; template policy must prevent this.
            result['items'][0].update(actual_result='通过',execution_status='已通过')
        if task=='modify':
            result['operations'][0]['item'].update(validation_goal='',actual_result='AI 编造的执行结果')
        return result

def test_learn_apply_generate_review_export_arbitrary_columns(tmp_path):
    model=TemplateModel()
    with TestClient(create_app(tmp_path,model)) as c:
        project,chat,_=setup_chat(c)
        learned=until(c,start(c,chat,experience='reliable',intent='learn_template'))
        proposal=c.get('/api/artifacts/'+learned['artifact_ids'][-1]).json()
        saved=c.post('/api/projects/'+project['id']+'/profiles',json={'name':'学习的模板','config':proposal['report']['config']})
        assert saved.status_code==200,saved.text
        run=until(c,start(c,chat,experience='reliable',profile_id=saved.json()['id']))
        assert run['status']=='completed',run.get('error')
        a=c.get('/api/artifacts/'+run['artifact_ids'][-1]).json();row=a['items'][0]
        assert row.get('validation_goal')=='验证已有账号能使用正确凭证登录。'
        assert row['test_data']=='使用有效凭证，不包含真实密码。'
        assert not row.get('actual_result')
        assert row['execution_status']=='未执行' and row['retry_limit']==0
        assert [t for t,_ in model.calls].count('complete_case_fields')==1
        ctx=next(ctx for t,ctx in model.calls if t=='complete_case_fields')
        assert set(next(iter(ctx['missing_fields'].values())))=={'validation_goal','test_data'}
        assert ctx['columns'][0]['definition']=='说明本用例验证的业务行为'
        output=c.get('/api/artifacts/'+a['id']+'/export');assert output.status_code==200,output.text
        sheet=load_workbook(io.BytesIO(output.content)).active
        assert sheet.title=='我的模板'
        assert list(next(sheet.values))==['用例名称','验证目的','输入数据','检查点','实际结果','执行状态','次数','风险说明']
        assert list(sheet.values)[1][1:]==('验证已有账号能使用正确凭证登录。','使用有效凭证，不包含真实密码。','1. User is authenticated',None,'未执行','0',None)

def test_new_export_template_completes_missing_fields_without_reanalysis(tmp_path):
    model=TemplateModel();model.reject_first=True
    with TestClient(create_app(tmp_path,model)) as c:
        project,chat,_=setup_chat(c)
        run=until(c,start(c,chat,experience='reliable'))
        a=c.get('/api/artifacts/'+run['artifact_ids'][-1]).json()
        rows=a['items'];rows[0]['test_data']='保留人工数据';rows[0]['switch_enabled']=False
        rows.append({**copy.deepcopy(rows[0]),'id':'unselected','test_data':'不改此行'})
        before=c.put('/api/artifacts/'+a['id'],json={'expected_revision':a['revision'],'items':rows}).json()
        profile=c.post('/api/projects/'+project['id']+'/profiles',json={'name':'新模板','config':{'excel_columns':COLUMNS+[{'field':'switch_enabled','header':'开关'},{'field':'new_check','header':'新增检查'}]}}).json()
        marker=len(model.calls)
        body={'expected_revision':before['revision'],'profile_id':profile['id'],'selected_ids':[rows[0]['id']]}
        req=c.post('/api/artifacts/'+a['id']+'/complete-fields',json=body)
        assert req.status_code==200,req.text
        run=until(c,req.json()['run']);assert run['status']=='completed',run.get('error')
        after=c.get('/api/artifacts/'+a['id']).json()
        assert after['items'][0]['test_data']=='保留人工数据'
        assert after['items'][0]['title']==rows[0]['title'] and after['items'][0]['switch_enabled'] is False
        assert after['items'][0]['new_check']=='登录后检查已认证状态。'
        assert after['items'][1]==before['items'][1]
        assert [t for t,_ in model.calls[marker:]]==['complete_case_fields','complete_case_fields']
        body['expected_revision']=after['revision']
        assert c.post('/api/artifacts/'+a['id']+'/complete-fields',json=body).json()['unchanged'] is True
        assert c.get('/api/artifacts/'+a['id']+'/export',params={'profile_id':profile['id'],'ids':rows[0]['id']}).status_code==200

def test_missing_business_fact_is_explained_without_failing_or_retry_loop(tmp_path):
    model=TemplateModel();model.missing_fact=True
    with TestClient(create_app(tmp_path,model)) as c:
        _,chat,_=setup_chat(c)
        run=until(c,start(c,chat,experience='reliable',profile_override={'excel_columns':COLUMNS}))
        assert run['status']=='completed',run.get('error')
        a=c.get('/api/artifacts/'+run['artifact_ids'][-1]).json()
        options=c.get('/api/artifacts/'+a['id']+'/export-options').json()
        gap=options['snapshot_check']['missing'][0]
        assert gap['field']=='validation_goal' and gap['reason']=='需要补充该检查项目的业务定义。'
        assert [t for t,_ in model.calls].count('complete_case_fields')==1
        result=c.get('/api/artifacts/'+a['id']+'/export')
        assert result.status_code==422 and '验证目的' in result.text
        # Clarifying/editing the actual value resolves the check without generating everything again.
        a['items'][0]['validation_goal']='人工补充的验证目的'
        c.put('/api/artifacts/'+a['id'],json={'expected_revision':a['revision'],'items':a['items']})
        assert c.get('/api/artifacts/'+a['id']+'/export').status_code==200

def test_chat_edit_preserves_manual_fields_and_repairs_missing_custom_content(tmp_path):
    model=TemplateModel()
    with TestClient(create_app(tmp_path,model)) as c:
        _,chat,_=setup_chat(c)
        run=until(c,start(c,chat,experience='reliable',profile_override={'excel_columns':COLUMNS}))
        a=c.get('/api/artifacts/'+run['artifact_ids'][-1]).json()
        a['items'][0]['actual_result']='用户实测：待复核'
        c.put('/api/artifacts/'+a['id'],json={'expected_revision':a['revision'],'items':a['items']})
        marker=len(model.calls)
        run=until(c,start(c,chat,experience='reliable',intent='modify',artifact_id=a['id'],profile_override={'excel_columns':COLUMNS}))
        assert run['status']=='completed',run.get('error')
        after=c.get('/api/artifacts/'+a['id']).json()
        assert after['items'][0]['actual_result']=='用户实测：待复核'
        assert after['items'][0]['validation_goal']=='验证已有账号能使用正确凭证登录。'
        assert after['items'][0]['title']=='Modified through chat'
        assert [t for t,_ in model.calls[marker:]]==['modify','complete_case_fields','summarize']
