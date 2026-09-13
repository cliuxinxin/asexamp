"""Behavior checks for conversation dispatch with real persisted state."""
import asyncio
import copy
import sys
import tempfile
import unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'backend'))
from tcg.storage import Store, now
from tcg.schemas import DomainError
from tcg.conversation import ConversationController
from tcg.conversation_receipts import commit_result

class Model:
    def __init__(self, actions):
        self.actions, self.calls = actions, []
    def fits(self, task, context): return True
    async def invoke_model(self, task, context, run_id=None):
        self.calls.append(copy.deepcopy(context))
        return {'actions': self.actions}

class ControllerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(); self.store=Store(self.temp.name)
        self.project=self.store.list('project')[0]['id']; self.chat=self.store.create_chat(self.project,'testing')
        self.count=0
    def tearDown(self): self.store.close(); self.temp.cleanup()
    def artifact(self, aid='scenes', kind='scenarios', revision=1):
        return self.store.put('artifact',{'id':aid,'type':kind,'project_id':self.project,'chat_id':self.chat['id'],
            'revision':revision,'title':aid,'items':[{'id':f'S{i}','title':f'scene {i}','description':'PRIVATE BODY'} for i in range(1,4)],
            '_visible':True,'created_at':now(),'_profile':{'sample_cases':['PRIVATE SAMPLE']}})
    def controller(self, model):
        async def execute(store,engine,chat,name,args,turn_id=None):
            self.count += 1
            result={'status':'succeeded','message':'done','parts':[{'type':'answer','text':'done'}]}
            if name=='artifact.revise':
                with store.transaction():
                    value=store.get('artifact',args['artifact_id']); value['revision']+=1; store.put('artifact',value)
                    commit_result(store,turn_id,result)
            if name=='artifact.estimate':
                result['parts']=[{'type':'estimate','data':{'artifact_id':args['artifact_id'], 'artifact_revision':args['expected_revision'], 'scenarios':[{'scenario_id':i} for i in args.get('selected_ids') or ['S1','S2','S3']]}}]
            return result
        registry={n:{'effect':effect,'description':n,'parameters':{},'execute':execute} for n,effect in [
            ('artifact.estimate','read'),('artifact.analyze','read'),('artifact.revise','write'),('workflow.continue','control')]}
        return ConversationController(self.store,model,registry=registry)
    async def ask(self,c,text='估算',key='one',**body):
        return await c.submit(self.chat['id'],{'client_message_id':key,'content':text,**body})
    async def test_duplicate_message_returns_receipt_once(self):
        self.artifact(); c=self.controller(Model([{'name':'artifact.revise','arguments':{'artifact_id':'scenes','instruction':'clarify'}}]))
        first=await self.ask(c,'改写'); second=await self.ask(c,'改写')
        self.assertEqual(first['id'],second['id']); self.assertEqual(self.count,1)
        self.assertEqual(self.store.get('artifact','scenes')['revision'],2)
        self.assertEqual(len(self.store.list('message',chat_id=self.chat['id'])),2)
    async def test_same_key_with_different_body_rejected(self):
        c=self.controller(Model([{'name':'artifact.analyze','arguments':{}}])); await self.ask(c)
        with self.assertRaises(DomainError): await self.ask(c,'其他内容')
    async def test_selection_resolves_current_visible_order(self):
        self.artifact(); c=self.controller(Model([{'name':'artifact.estimate','arguments':{'artifact_type':'scenarios','ordinals':[2]}}]))
        result=await self.ask(c,artifact_id='scenes',artifact_revision=1,view_order=['S3','S1','S2'])
        self.assertEqual(result['parts'][0]['data']['scenarios'],[{'scenario_id':'S1'}])
    async def test_ambiguous_target_creates_persistent_pending_without_execute(self):
        self.artifact('first'); self.artifact('second'); c=self.controller(Model([{'name':'artifact.estimate','arguments':{}}]))
        result=await self.ask(c)
        self.assertEqual(result['status'],'needs_input'); self.assertEqual(self.count,0)
        self.assertEqual({x['id'] for x in result['pending'][0]['candidates']},{'first','second'})
        self.assertTrue(self.store.list('conversation_pending',chat_id=self.chat['id']))
    async def test_candidate_reply_resumes_original_operation(self):
        self.artifact('first');self.artifact('second'); model=Model([{'name':'artifact.estimate','arguments':{}}]);c=self.controller(model)
        first=await self.ask(c); pending=first['pending'][0]
        model.actions=[{'name':'conversation.resolve','arguments':{'pending_id':pending['id'],'choice_id':'second'}}]
        second=await self.ask(c,'第二份',key='two')
        self.assertEqual(second['status'],'succeeded'); self.assertEqual(second['parts'][0]['data']['artifact_id'],'second')
        self.assertEqual(self.count,1)
    async def test_bare_ack_after_estimate_never_continues_workflow(self):
        self.artifact(); model=Model([{'name':'artifact.estimate','arguments':{}}]);c=self.controller(model);await self.ask(c)
        model.actions=[{'name':'workflow.continue','arguments':{'confirmation':'explicit'}}]
        result=await self.ask(c,'可以',key='two')
        self.assertEqual(self.count,1); self.assertEqual(result['status'],'succeeded')
    async def test_planner_context_omits_document_and_case_bodies(self):
        self.artifact();self.store.add_source(self.chat['id'],'requirement','primary','PRIVATE DOCUMENT',[{'text':'PRIVATE DOCUMENT'}])
        model=Model([{'name':'artifact.estimate','arguments':{}}]);await self.ask(self.controller(model))
        text=str(model.calls[0])
        for secret in ('PRIVATE DOCUMENT','PRIVATE BODY','PRIVATE SAMPLE'):self.assertNotIn(secret,text)
    async def test_unknown_capability_never_executes(self):
        model=Model([{'name':'database.delete_all','arguments':{}}]);result=await self.ask(self.controller(model))
        self.assertEqual(result['status'],'failed');self.assertEqual(self.count,0)
    async def test_cross_chat_artifact_rejected(self):
        a=self.artifact();other=self.store.create_chat(self.project,'other');a['chat_id']=other['id'];self.store.put('artifact',a)
        result=await self.ask(self.controller(Model([{'name':'artifact.revise','arguments':{'artifact_id':'scenes'}}])))
        self.assertNotEqual(result['status'],'succeeded');self.assertEqual(self.count,0)

if __name__=='__main__': unittest.main()
