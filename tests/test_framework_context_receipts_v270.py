"""Turn provenance bindings and safe context receipts with real diagnostics/SQLite."""
import copy
import json
import pytest
from tcg.context_receipts import start, complete, list_turn_contexts
from tcg.conversation import ConversationController
from tcg.diagnostics import Diagnostics
from tcg.schemas import DomainError
from tcg.storage import Store, uid


SECRET='PRIVATE evidence, profile example and request text'


class Engine:
    def __init__(self,store):
        self.store=store;self.diagnostics=Diagnostics(store);self.bindings=[]
    def fits(self,*args):return True
    async def invoke_model(self,task,context,run_id=None):
        binding=copy.deepcopy(self.diagnostics.context.get());self.bindings.append(binding)
        receipt=start(self.store,task,context,binding=binding,call_id=uid('call_'),
            budget={'fits':True,'input_tokens':17,'request_digest':'a'*64,'messages':[SECRET]})
        try:
            if task=='fail':raise RuntimeError(SECRET)
            result={'actions':[{'name':'test.analyze','arguments':{}}]} if task=='conversation_turn' else {'answer':'已分析'}
            complete(self.store,receipt,'succeeded',usage={'input_tokens':17,'output_tokens':4,'raw_response':SECRET})
            return result
        except Exception as exc:
            complete(self.store,receipt,'failed',error=exc)
            raise


def safe_context():
    return {'instruction':SECRET,'evidence':[{'id':'src#P1','source_id':'src','source_version':3,'role':'primary','text':SECRET}],
        'profile':{'samples':[SECRET]},'coverage':{'included_evidence_ids':['src#P1'],'omitted_evidence_ids':['src#P2'],
            'partial':True,'note':SECRET},
        'dependency_manifest':{'version':1,'digest':'b'*64,'sources':[{'id':'src','version':3,'digest':'c'*64,'text':SECRET}],
            'profiles':[{'id':'profile','version':2,'digest':'d'*64,'snapshot':{'samples':[SECRET]}}]}}


@pytest.mark.asyncio
async def test_planner_and_adapter_inherit_correct_turn_context_without_payloads(tmp_path):
    store=Store(tmp_path);chat=store.create_chat(store.list('project')[0]['id'],'receipts');engine=Engine(store)
    async def adapter(store,engine,chat,name,args,turn_id=None):
        await engine.invoke_model('artifact_explain',safe_context())
        return {'status':'succeeded','message':'已分析','parts':[]}
    controller=ConversationController(store,engine,{'test.analyze':{'effect':'read','parameters':{},'execute':adapter}})
    try:
        with engine.diagnostics.bind(run_id='unrelated-parent',turn_id='unrelated-parent'):
            response=await controller.submit(chat['id'],{'client_message_id':'receipt-turn','content':SECRET})
            assert engine.diagnostics.context.get()['run_id']=='unrelated-parent'
        assert response['status']=='succeeded'
        receipts=list_turn_contexts(store,chat['id'],response['id'])
        assert [r['task'] for r in receipts]==['conversation_turn','artifact_explain']
        assert receipts[0]['command_id'] is None
        assert receipts[1]['command_id']==response['actions'][0]['id']
        assert all(r['turn_id']==response['id'] and r['run_id'] is None for r in receipts)
        assert receipts[1]['coverage']['partial']
        assert receipts[1]['evidence_versions']==[{'id':'src#P1','source_id':'src','source_version':3,'role':'primary'}]
        assert receipts[1]['context_manifest']['profiles']==[{'id':'profile','version':2,'digest':'d'*64}]
        assert len(receipts[1]['profile_digest'])==64
        serialized=json.dumps(receipts)
        assert SECRET not in serialized
        assert '_fingerprint' not in serialized
        assert 'messages' not in receipts[1]['request_budget']
        assert receipts[1]['usage']=={'input_tokens':17,'output_tokens':4}
    finally:
        await controller.close();engine.diagnostics.close();store.close()


@pytest.mark.asyncio
async def test_failure_receipt_has_type_only_and_cross_chat_listing_rejected(tmp_path):
    store=Store(tmp_path);project=store.list('project')[0]['id'];chat=store.create_chat(project,'owner');other=store.create_chat(project,'other')
    engine=Engine(store)
    async def adapter(store,engine,chat,name,args,turn_id=None):return await engine.invoke_model('fail',safe_context())
    controller=ConversationController(store,engine,{'test.fail':{'effect':'read','parameters':{},'execute':adapter}})
    try:
        response=await controller.submit(chat['id'],{'client_message_id':'fail','content':'读取',
            'command':{'name':'test.fail','arguments':{}}})
        receipt=list_turn_contexts(store,chat['id'],response['id'])[0]
        assert receipt['status']=='failed' and receipt['error']=={'type':'RuntimeError'}
        assert SECRET not in json.dumps(receipt)
        with pytest.raises(DomainError):list_turn_contexts(store,other['id'],response['id'])
    finally:
        await controller.close();engine.diagnostics.close();store.close()


def test_unbound_calls_ignored_and_reused_call_ids_are_checked(tmp_path):
    store=Store(tmp_path);chat=store.create_chat(store.list('project')[0]['id'],'owner')
    store.put('conversation_turn',{'id':'turn-safe','chat_id':chat['id'],'project_id':chat['project_id']})
    try:
        assert start(store,'connection_test',safe_context(),binding={},call_id='unbound') is None
        binding={'turn_id':'turn-safe','chat_id':chat['id'],'project_id':chat['project_id']}
        receipt=start(store,'analyze',safe_context(),binding=binding,call_id='stable')
        assert start(store,'analyze',safe_context(),binding=binding,call_id='stable')==receipt
        with pytest.raises(DomainError):start(store,'other',safe_context(),binding=binding,call_id='stable')
        complete(store,receipt,'cancelled')
        assert complete(store,receipt,'succeeded')['status']=='cancelled'
        assert len(store.list('context_receipt'))==1
    finally:store.close()
