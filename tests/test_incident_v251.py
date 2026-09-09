"""Replay actual user-returned JSON; do not fill missing fields in test generation."""
import copy,json
from pathlib import Path
from fastapi.testclient import TestClient
from tcg.main import create_app
from test_backend_api import setup_chat,start,until
from test_workflow_v25 import FlowModel

FIX=Path(__file__).parent/'fixtures'
class IncidentModel(FlowModel):
    async def generate(self,task,context):
        if task in ('analyze_requirement','generate_scenarios','learn_template'):
            self.calls.append((task,copy.deepcopy(context)))
            name={'analyze_requirement':'user_analysis','generate_scenarios':'user_scenarios','learn_template':'user_template'}[task]
            value=json.loads((FIX/(name+'.json')).read_text())
            if 'items' in value:
                ref=next(e['id'] for e in context['evidence'] if e['role']!='example')
                for item in value['items']:item['refs']=[ref]
            return value
        if task=='link_scenarios':
            self.calls.append((task,copy.deepcopy(context)))
            # Explicit expected trace links for the supplied login fixture.
            mapping=[1,2,2,3,3,3,4,4,4,4,5,5,5,5,6,6,7,7,2,3]
            return {'links':[{'id':s['id'],'requirement_ids':[context['analysis'][mapping[i]-1]['id']]} for i,s in enumerate(context['scenarios'])]}
        if task=='generate_cases':
            self.calls.append((task,copy.deepcopy(context)))
            return {'items':[{'id':'TC-'+str(i),'title':s['title'],'scenario_id':s['id'],'type':'Business','priority':'P1','preconditions':'已准备测试账号','steps':[{'action':s['description'],'expected':'满足场景描述的预期行为'}],'refs':s['refs']} for i,s in enumerate(context['scenarios'])], 'has_more':False}
        return await super().generate(task,context)

def test_user_scenarios_missing_trace_links_complete(tmp_path):
    model=IncidentModel()
    with TestClient(create_app(tmp_path,model)) as c:
        _,chat,_=setup_chat(c)
        run=until(c,start(c,chat,experience='reliable',mode='auto'))
        assert run['status']=='completed',run.get('error')
        assert sum(t=='analyze_requirement' for t,_ in model.calls)==1
        assert sum(t=='generate_scenarios' for t,_ in model.calls)==1
        assert sum(t=='link_scenarios' for t,_ in model.calls)==1
        assert c.get('/api/artifacts/'+run['artifact_ids'][0]+'/export').status_code==200

def test_non_template_excel_response_keeps_profile(tmp_path):
    model=IncidentModel()
    with TestClient(create_app(tmp_path,model)) as c:
        project,chat,_=setup_chat(c)
        before=c.get('/api/projects/'+project['id']+'/profiles').json()
        run=until(c,start(c,chat,experience='reliable',intent='learn_template'))
        assert run['status']=='completed',run.get('error')
        artifact=c.get('/api/artifacts/'+run['artifact_ids'][0]).json()
        assert artifact['report']['config']['excel_columns']==before[0]['config']['excel_columns']
        assert artifact['report']['config']['filename_pattern']==before[0]['config']['filename_pattern']
        assert c.get('/api/projects/'+project['id']+'/profiles').json()==before

def test_clarification_keeps_analysis_ids_and_no_reanalysis(tmp_path):
    model=IncidentModel()
    with TestClient(create_app(tmp_path,model)) as c:
        _,chat,_=setup_chat(c)
        run=until(c,start(c,chat,experience='reliable',mode='hitp'))
        aid=run['interrupt']['artifact_id'];before=c.get('/api/artifacts/'+aid).json()
        c.post('/api/runs/'+run['id']+'/resume',json={'answer':'不用，按已有规则继续'})
        run=until(c,run)
        assert run['interrupt']['type']=='strategy_review'
        assert run['interrupt']['artifact_id']==aid
        after=c.get('/api/artifacts/'+aid).json()
        assert after['items']==before['items']
        assert '不用' in after['report']['clarification']
        assert sum(t=='analyze_requirement' for t,_ in model.calls)==1
