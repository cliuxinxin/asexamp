"""Exhaustive source-impact reads and freshness checks against real SQLite inputs."""
import copy
import pytest
from tcg.conversation import default_registry
from tcg.conversation_project import execute
from tcg.documents import parse_text
from tcg.schemas import DomainError, MessageInput
from tcg.storage import Store


def setup(tmp_path, requirement_count=4, chunk_count=4):
    store=Store(tmp_path)
    chat=store.create_chat(store.list('project')[0]['id'],'impact')
    text,chunks=parse_text('Existing account rules')
    original=store.add_source(chat['id'],'base','primary',text,chunks)
    _,run=store.create_run(chat['id'],MessageInput(content='分析需求',intent='generate_scenario',mode='hitp').model_dump())
    rows=[{'id':'R'+str(i),'title':'Rule '+str(i),'description':'Account requirement '+str(i),
        'refs':[original['id']+'#P1']} for i in range(requirement_count)]
    analysis=store.artifact(run['id'],'v7:analysis:artifact','analysis','Analysis',rows)
    analysis=store.put('artifact',{**analysis,'_visible':True})
    scenario=store.artifact(run['id'],'scenario','scenarios','Scenario',[{'id':'S1','title':'Check R0','description':'Check account rule',
        'priority':'P1','requirement_ids':['R0'],'refs':[original['id']+'#P1']}],
        {'lineage':{'analysis_artifact_id':analysis['id'],'analysis_revision':analysis['revision']}})
    store.put('artifact',{**scenario,'_visible':True})
    store.update_run(run['id'],status='waiting',interrupt={'type':'scenario_review','artifact_id':scenario['id']},stop_after='scenarios')
    chunks=[{'locator':'Section '+str(i),'text':('new evidence '+str(i)+' ') * 80} for i in range(chunk_count)]
    new=store.add_source(chat['id'],'change','change','\n'.join(c['text'] for c in chunks),chunks)
    return store,chat,analysis,new,run['id']


class Model:
    def __init__(self,split=False,hook=None):self.calls=[];self.split=split;self.hook=hook
    def fits(self,task,context):
        if not self.split:return True
        return len(context['requirements'])<=2 and len(context['new_evidence'])<=1 and len(context['new_evidence'][0]['text'])<=450
    async def invoke_model(self,task,context,run_id=None):
        assert task=='project_source_impact'
        assert self.fits(task,context)
        self.calls.append(copy.deepcopy(context))
        if self.hook:self.hook();self.hook=None
        return {'requirement_ids':[r['id'] for r in context['requirements'] if r['id']=='R0'],
            'summary':'R0 may change','refs':list(dict.fromkeys(e['id'] for e in context['new_evidence'])),
            'uncertain':False,'global_impact':False}


@pytest.mark.asyncio
async def test_read_impact_leaves_artifacts_sources_and_run_unchanged(tmp_path):
    store,chat,analysis,new,rid=setup(tmp_path)
    before={a['id']:store.revisions(a['id']) for a in store.list('artifact',chat_id=chat['id'])}
    source_before=store.list('source',chat_id=chat['id']);run_before=store.run(rid)
    try:
        result=await execute(store,Model(),chat,'project.source_impact',{'artifact_id':analysis['id'],
            'source_ids':[new['id']],'instruction':'只看看新增资料影响，不修改'})
        report=result['parts'][0]['data']
        assert report['requirement_ids']==['R0']
        assert report['downstream_candidates'][0]['candidate_item_ids']==['S1']
        assert report['input_versions']['sources'][0]['version']==1
        assert report['coverage']['checked_pairs']==report['coverage']['expected_pairs']==16
        assert not report['coverage']['partial']
        assert source_before==store.list('source',chat_id=chat['id'])
        assert run_before==store.run(rid)
        assert before=={a['id']:store.revisions(a['id']) for a in store.list('artifact',chat_id=chat['id'])}
        definition=default_registry()['project.source_impact']
        assert definition['effect']=='read' and definition['target_types']==['analysis']
    finally:store.close()


@pytest.mark.asyncio
async def test_large_impact_covers_each_requirement_and_every_full_chunk(tmp_path):
    store,chat,analysis,new,_=setup(tmp_path)
    model=Model(split=True)
    try:
        result=await execute(store,model,chat,'project.source_impact',{'artifact_id':analysis['id'],'source_ids':[new['id']]})
        report=result['parts'][0]['data'];evidence=store.evidence([new['id']])
        assert len(model.calls)>len(evidence)
        for row in analysis['items']:
            for chunk in evidence:
                fragments={e.get('fragment_start',0):e['text'] for c in model.calls if row['id'] in {r['id'] for r in c['requirements']}
                    for e in c['new_evidence'] if e['id']==chunk['id']}
                assert ''.join(fragments[start] for start in sorted(fragments))==chunk['text']
        assert set(report['coverage']['evidence_ids'])=={e['id'] for e in evidence}
        assert report['coverage']['checked_pairs']==16
        assert not report['coverage']['partial']
    finally:store.close()


@pytest.mark.asyncio
async def test_changed_source_before_targeted_update_is_rejected(tmp_path):
    store,chat,analysis,new,_=setup(tmp_path)
    before=store.revisions(analysis['id'])
    def change_source():
        value=store.get('source',new['id'])
        store.put('source',{**value,'name':'changed after impact snapshot'})
    try:
        with pytest.raises(DomainError,match='依赖已改变'):
            await execute(store,Model(hook=change_source),chat,'project.update_from_sources',
                {'artifact_id':analysis['id'],'source_ids':[new['id']]})
        assert store.revisions(analysis['id'])==before
    finally:store.close()


@pytest.mark.asyncio
async def test_read_impact_inline_text_does_not_create_source(tmp_path):
    store,chat,analysis,_,_=setup(tmp_path)
    before=store.list('source',chat_id=chat['id'])
    try:
        result=await execute(store,Model(),chat,'project.source_impact',{'artifact_id':analysis['id'],'content':'新的锁定要求'})
        assert result['parts'][0]['data']['transient_content_digest']
        assert before==store.list('source',chat_id=chat['id'])
    finally:store.close()


@pytest.mark.asyncio
async def test_interrupted_update_reuses_completed_impact_partition_receipts(tmp_path):
    store,chat,analysis,new,_=setup(tmp_path,requirement_count=2,chunk_count=2)
    command_id='impact-retry-command'
    store.put('conversation_command',{'id':command_id,'project_id':chat['project_id'],'chat_id':chat['id'],
        'name':'project.update_from_sources','effect':'write','status':'pending'})
    class Interrupted(Model):
        async def invoke_model(self,task,context,run_id=None):
            result=await super().invoke_model(task,context,run_id)
            if len(self.calls)==2:raise RuntimeError('interrupted after first partition receipt')
            result['requirement_ids']=[]
            return result
    model=Interrupted(split=True)
    args={'artifact_id':analysis['id'],'source_ids':[new['id']]}
    before=store.revisions(analysis['id'])
    try:
        with pytest.raises(RuntimeError):
            await execute(store,model,chat,'project.update_from_sources',args,command_id)
        first=copy.deepcopy(model.calls[0])
        result=await execute(store,model,chat,'project.update_from_sources',args,command_id)
        assert result['status']=='succeeded'
        assert sum(call==first for call in model.calls)==1
        calls=len(model.calls)
        repeated=await execute(store,model,chat,'project.update_from_sources',args,command_id)
        assert repeated==result and len(model.calls)==calls
        assert store.revisions(analysis['id'])==before
    finally:store.close()
