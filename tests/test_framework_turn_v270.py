"""Real LangGraph durability and capability contract integration tests."""
import copy
import pytest
from tcg.conversation import ConversationController, default_registry
from tcg.conversation_context import resolve_artifact, build_context, expand_catalog
from tcg.conversation_receipts import commit_result
from tcg.storage import Store, now


class Model:
    def __init__(self, actions):
        self.actions=actions
        self.calls=0
    def fits(self, *args):return True
    async def invoke_model(self, *args):
        self.calls+=1
        return {'actions':copy.deepcopy(self.actions)}


def setup(tmp_path):
    store=Store(tmp_path)
    chat=store.create_chat(store.list('project')[0]['id'],'durable turn')
    return store,chat


def artifact(store,chat,aid='scenarios'):
    return store.put('artifact',{'id':aid,'project_id':chat['project_id'],'chat_id':chat['id'],
        'type':'scenarios','revision':1,'title':aid,'items':[{'id':'S1','title':'One'}],
        '_visible':True,'created_at':now()})


@pytest.mark.asyncio
async def test_real_checkpoint_recovers_after_commit_before_node_result(tmp_path):
    store,chat=setup(tmp_path)
    effects=[]
    async def execute(store,engine,chat,name,args,turn_id=None):
        effects.append(name)
        result={'status':'succeeded','message':name,'parts':[{'type':'answer','text':name}]}
        with store.transaction():
            store.put('test_effect',{'id':'effect:'+turn_id,'project_id':chat['project_id'],'name':name})
            commit_result(store,turn_id,result)
        if name=='test.first':raise RuntimeError('simulated process failure after durable commit')
        return result
    registry={name:{'effect':'write','description':name,'parameters':{},'execute':execute} for name in ('test.first','test.second')}
    model=Model([{'name':name,'arguments':{}} for name in registry])
    body={'client_message_id':'stable-message','content':'顺序执行两项'}
    first=ConversationController(store,model,registry)
    response=await first.submit(chat['id'],body)
    assert response['status']=='recoverable'
    checkpoint=await first._turn_graph.graph.aget_state({'configurable':{'thread_id':response['id']}})
    assert checkpoint.next==('command',)
    assert set(checkpoint.values)<= {'turn_id','command_id','route'}
    await first.close()
    store.close()
    store=Store(tmp_path)
    second=ConversationController(store,model,registry)
    try:
        resumed=await second.submit(chat['id'],body)
        assert resumed['status']=='succeeded', resumed
        assert effects==['test.first','test.second']
        assert model.calls==1
        assert len(store.list('test_effect'))==2
        assert [p['text'] for p in resumed['parts']]==effects
        checkpoint=await second._turn_graph.graph.aget_state({'configurable':{'thread_id':resumed['id']}})
        assert not checkpoint.next
    finally:
        await second.close();store.close()


@pytest.mark.asyncio
async def test_waiting_checkpoint_resumes_original_command_after_selection(tmp_path):
    store,chat=setup(tmp_path)
    artifact(store,chat,'one');artifact(store,chat,'two')
    seen=[]
    async def execute(store,engine,chat,name,args,turn_id=None):
        seen.append(args['artifact_id'])
        return {'status':'succeeded','message':'已读取','parts':[]}
    registry={'custom.inspect':{'effect':'read','target_types':['scenarios'],'target_required':True,
        'context_policy':'artifact_evidence','parameters':{},'execute':execute}}
    controller=ConversationController(store,Model([]),registry)
    try:
        result=await controller.submit(chat['id'],{'client_message_id':'ask','content':'读取','command':{'name':'custom.inspect','arguments':{}}})
        assert result['status']=='needs_input'
        checkpoint=await controller._turn_graph.graph.aget_state({'configurable':{'thread_id':result['id']}})
        assert checkpoint.next==('waiting',)
        assert checkpoint.tasks[0].interrupts
        pending=result['pending'][0]
        result=await controller.submit(chat['id'],{'client_message_id':'choose','content':'第二项','command':{
            'name':'conversation.resolve','arguments':{'pending_id':pending['id'],'choice_id':'two'}}})
        assert result['status']=='succeeded'
        assert seen==['two']
    finally:
        await controller.close();store.close()


@pytest.mark.asyncio
async def test_estimate_is_snapshot_only_and_ack_cannot_approve_history(tmp_path):
    store,chat=setup(tmp_path);artifact(store,chat)
    _,run=store.create_run(chat['id'],{'content':'test','intent':'generate_case','mode':'auto'})
    run=store.update_run(run['id'],status='waiting',control_version=4,stop_after='scenarios',interrupt={'type':'scenario_review','artifact_id':'scenarios'})
    store.put('conversation_pending',{'id':'old-preview','kind':'proposal','status':'open','proposal_id':'old',
        'chat_id':chat['id'],'project_id':chat['project_id']})
    async def estimate(store,engine,chat,name,args,turn_id=None):
        return {'status':'succeeded','message':'已估算','parts':[{'type':'estimate','data':{'total':2}}]}
    controller=ConversationController(store,Model([]),{'artifact.estimate':{**default_registry()['artifact.estimate'],'execute':estimate}})
    before=copy.deepcopy(store.run(run['id']))
    try:
        response=await controller.submit(chat['id'],{'client_message_id':'estimate','content':'本轮只估算，不生成',
            'command':{'name':'artifact.estimate','arguments':{}}})
        assert response['status']=='succeeded'
        assert store.run(run['id'])==before
        response=await controller.submit(chat['id'],{'client_message_id':'ack','content':'好的'})
        assert response['actions']==[]
        assert store.get('conversation_pending','old-preview')['status']=='open'
        assert store.run(run['id'])==before
    finally:
        await controller.close();store.close()


@pytest.mark.asyncio
async def test_metadata_owned_target_and_bounded_catalog(tmp_path):
    store,chat=setup(tmp_path)
    for i in range(16):artifact(store,chat,str(i))
    try:
        definition={'target_types':['scenarios'],'target_required':True}
        resolved=resolve_artifact(store,chat,{}, {},'third_party.inspect',{'artifact_id':'15'},definition)
        assert resolved['artifact_id']=='15'
        context=build_context(store,Model([]),chat,{'content':'读取'},default_registry())
        assert context['catalog_totals']['artifacts']==16
        assert context['catalog_partial']['artifacts']
        result=await expand_catalog(store,None,chat,'conversation.catalog',{'offset':12,'limit':4})
        page=result['parts'][0]['catalog']
        assert page['total']==16 and len(page['items'])==4
        assert page['partial'] and page['next_offset'] is None
    finally:store.close()


@pytest.mark.asyncio
async def test_prepared_preview_receipt_replays_without_duplicate_proposal(tmp_path):
    store,chat=setup(tmp_path)
    calls=[]
    async def preview(store,engine,chat,name,args,turn_id=None):
        calls.append(turn_id)
        result={'status':'needs_confirmation','message':'请确认预览','parts':[{'type':'diff','proposal_id':'preview-stable'}]}
        with store.transaction():commit_result(store,turn_id,result)
        raise RuntimeError('lost response after preview commit')
    registry={'custom.preview':{'effect':'write','parameters':{},'execute':preview}}
    controller=ConversationController(store,Model([]),registry)
    body={'client_message_id':'preview','content':'先预览','command':{'name':'custom.preview','arguments':{}}}
    try:
        first=await controller.submit(chat['id'],body)
        assert first['status']=='recoverable'
        second=await controller.submit(chat['id'],body)
        assert second['status']=='needs_confirmation'
        assert len(calls)==1
        assert len(second['pending'])==1
        checkpoint=await controller._turn_graph.graph.aget_state({'configurable':{'thread_id':second['id']}})
        assert checkpoint.next==('waiting',) and checkpoint.tasks[0].interrupts
    finally:
        await controller.close();store.close()


def test_review_capability_owns_effect_preparation():
    from tcg.capability_contracts import prepare_command
    definition=default_registry()['artifact.review_cases']
    assert definition['target_types']==['cases']
    assert definition['target_required'] is True
    assert prepare_command(definition,{'optimize':True})[1]=='write'
    assert prepare_command(definition,{'optimize':False})[1]=='read'


def saved_scenarios(store,chat):
    from tcg.schemas import MessageInput
    from tcg.documents import parse_text
    text,chunks=parse_text('用户需要验证登录限制')
    source=store.add_source(chat['id'],'需求','primary',text,chunks)
    _,run=store.create_run(chat['id'],MessageInput(content='生成',intent='generate_scenario',mode='hitp').model_dump())
    value=store.artifact(run['id'],'recovery-scenarios','scenarios','场景',[
        {'id':'S1','title':'登录验证','description':'验证登录限制','priority':'P1','refs':[source['id']+'#P1']}])
    value=store.put('artifact',{**value,'_visible':True})
    store.update_run(run['id'],status='completed')
    return value,run['id']


@pytest.mark.asyncio
@pytest.mark.parametrize('crash_at',['apply','tail','after_tail'])
async def test_apply_receipt_reconciles_resolved_parent_tail_across_restart(tmp_path,crash_at):
    store,chat=setup(tmp_path);saved,rid=saved_scenarios(store,chat);calls=[]
    async def adapter(store,engine,chat,name,args,turn_id=None):
        calls.append(name)
        if name=='test.preview':
            return {'status':'needs_confirmation','message':'请确认预览','parts':[{'type':'diff','proposal_id':'stable-preview'}]}
        result={'status':'succeeded','message':name,'parts':[{'type':'answer','text':name}]}
        with store.transaction():
            if name=='artifact.apply':
                value=store.get('artifact',saved['id']);rows=copy.deepcopy(value['items']);rows[0]['title']='改进登录验证'
                updated=store.revise_artifact(value['id'],value['revision'],rows,command_id='operation:'+turn_id)
                result['parts']=[{'type':'artifact','artifact_id':updated['id'],'revision':updated['revision']}]
            else:store.put('tail_effect',{'id':'effect:'+turn_id,'project_id':chat['project_id'],'name':name})
            commit_result(store,turn_id,result)
        if name==('artifact.apply' if crash_at=='apply' else 'test.tail1' if crash_at=='tail' else None):
            raise RuntimeError('crash after atomic effect receipt')
        return result
    registry={name:{'effect':'write' if name=='artifact.apply' else 'read','parameters':{},'execute':adapter}
              for name in ('test.preview','artifact.apply','test.tail1','test.tail2')}
    model=Model([{'name':name,'arguments':{}} for name in ('test.preview','test.tail1','test.tail2')])
    controller=ConversationController(store,model,registry)
    original=await controller.submit(chat['id'],{'client_message_id':'preview-parent','content':'先预览，应用后再执行两项'})
    assert original['status']=='needs_confirmation'
    if crash_at=='after_tail':
        ordinary_run=controller._run
        async def crash_after_tail(turn,chat):
            response=await ordinary_run(turn,chat)
            if turn['id']==original['id'] and response['status']=='succeeded':raise RuntimeError('lost continuation acknowledgement')
            return response
        controller._run=crash_after_tail
    body={'client_message_id':'apply-child','content':'应用预览','command':{'name':'artifact.apply','arguments':{'proposal_id':'stable-preview'}}}
    failed=await controller.submit(chat['id'],body)
    assert failed['status']=='recoverable',failed
    # Also cover old partial projection, where the pending was resolved before parent resume.
    for pending in store.list('conversation_pending',chat_id=chat['id']):
        pending['status']='resolved';store.put('conversation_pending',pending)
    await controller.close();store.close()
    store=Store(tmp_path);controller=ConversationController(store,model,registry)
    try:
        response=await controller.submit(chat['id'],body)
        assert response['status']=='succeeded',response
        assert store.get('conversation_turn',original['id'])['status']=='succeeded'
        assert calls==['test.preview','artifact.apply','test.tail1','test.tail2']
        assert len(store.revisions(saved['id']))==2
        assert len(store.list('tail_effect'))==2
        assert [p['text'] for p in response['parts'] if p['type']=='answer']==['test.tail1','test.tail2']
        again=await controller.submit(chat['id'],body)
        assert again==response
    finally:
        await controller.close();store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('preview,external_pause',[(False,False),(True,False),(False,True),(True,True),(False,'control'),(True,'control')])
async def test_compound_continue_uses_only_own_committed_gate_transition(tmp_path,preview,external_pause):
    from tcg.workflow_bindings import validate_confirmation
    from tcg.conversation_context import get_state
    store,chat=setup(tmp_path);saved,rid=saved_scenarios(store,chat)
    store.update_run(rid,status='waiting',control_version=4,_interrupt_id='gate-stable',
        interrupt={'type':'scenario_review','artifact_id':saved['id'],'artifact_revision':1})
    continued=[]
    async def adapter(store,engine,chat,name,args,turn_id=None):
        if name=='test.preview':return {'status':'needs_confirmation','message':'预览','parts':[{'type':'diff','proposal_id':'causal-preview'}]}
        result={'status':'succeeded','message':name,'parts':[]}
        if name=='workflow.continue':
            validate_confirmation(store,store.run(rid),args)
            continued.append(copy.deepcopy(args))
            return result
        with store.transaction():
            value=store.get('artifact',saved['id']);rows=copy.deepcopy(value['items']);rows[0]['title']='授权修改'
            value=store.revise_artifact(value['id'],value['revision'],rows,command_id='effect:'+turn_id)
            result['parts']=[{'type':'artifact','artifact_id':value['id'],'revision':value['revision']}]
            commit_result(store,turn_id,result)
        if external_pause:
            # A newer explicit hold occurs after the artifact's atomic own-write transition.
            run=store.run(rid);store.update_run(rid,control_version=run['control_version']+1,_control_hold=True)
            if external_pause!='control':
                state=get_state(store,chat);state['continue_epoch']=state.get('continue_epoch',0)+1;store.put('conversation_state',state)
        return result
    registry={name:{'effect':effect,'parameters':{},'execute':adapter} for name,effect in
        [('test.preview','read'),('artifact.apply','write'),('artifact.revise','write'),('workflow.continue','control')]}
    actions=[{'name':'test.preview' if preview else 'artifact.revise','arguments':{} if preview else {'artifact_id':saved['id']}},
             {'name':'workflow.continue','arguments':{}}]
    controller=ConversationController(store,Model(actions),registry)
    try:
        original=await controller.submit(chat['id'],{'client_message_id':'causal','content':'修改后继续'})
        if preview:
            assert original['status']=='needs_confirmation'
            response=await controller.submit(chat['id'],{'client_message_id':'causal-apply','content':'应用',
                'command':{'name':'artifact.apply','arguments':{'proposal_id':'causal-preview'}}})
        else:response=original
        if external_pause:
            assert not continued
            parent=store.get('conversation_turn',original['id'])
            if external_pause=='control':assert parent['status']=='failed'
            else:assert parent['actions'][-1]['status']=='cancelled'
        else:
            assert response['status']=='succeeded',response
            assert len(continued)==1
            assert continued[0]['expected_control_version']==5
            assert continued[0]['expected_revision']==2
            assert continued[0]['interrupt_id']=='gate-stable'
        assert len(store.revisions(saved['id']))==2
    finally:
        await controller.close();store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('crash_at',['effect','parent_return'])
async def test_input_resolution_receipt_replays_already_resolved_parent(tmp_path,crash_at):
    store,chat=setup(tmp_path);artifact(store,chat,'one');artifact(store,chat,'two');calls=[]
    async def inspect(store,engine,chat,name,args,turn_id=None):
        calls.append(args['artifact_id'])
        result={'status':'succeeded','message':'已读取所选成果','parts':[{'type':'answer','text':args['artifact_id']}]}
        with store.transaction():commit_result(store,turn_id,result)
        if crash_at=='effect':raise RuntimeError('lost selected command response')
        return result
    registry={'custom.inspect':{'effect':'read','target_types':['scenarios'],'target_required':True,'parameters':{},'execute':inspect}}
    controller=ConversationController(store,Model([]),registry)
    original=await controller.submit(chat['id'],{'client_message_id':'input-parent','content':'读取成果',
        'command':{'name':'custom.inspect','arguments':{}}})
    assert original['status']=='needs_input'
    if crash_at=='parent_return':
        ordinary_run=controller._run
        async def crash_after_return(turn,chat):
            response=await ordinary_run(turn,chat)
            if turn['id']==original['id'] and response['status']=='succeeded':raise RuntimeError('lost resolved-parent response')
            return response
        controller._run=crash_after_return
    body={'client_message_id':'input-answer','content':'第二份','command':{'name':'conversation.resolve','arguments':{
        'pending_id':original['pending'][0]['id'],'choice_id':'two'}}}
    failed=await controller.submit(chat['id'],body)
    assert failed['status']=='recoverable',failed
    assert store.get('conversation_pending',original['pending'][0]['id'])['status']=='resolved'
    await controller.close();store.close()
    store=Store(tmp_path);controller=ConversationController(store,Model([]),registry)
    try:
        response=await controller.submit(chat['id'],body)
        assert response['status']=='succeeded',response
        assert calls==['two']
        assert response['parts']==[{'type':'answer','text':'two'}]
        assert store.get('conversation_turn',original['id'])['status']=='succeeded'
        assert await controller.submit(chat['id'],body)==response
    finally:
        await controller.close();store.close()
