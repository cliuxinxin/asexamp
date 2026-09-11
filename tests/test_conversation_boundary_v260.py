"""Real Engine method bodies + SQLite; LangGraph checkpoint integration is separate.

Extracting the methods keeps these focused consistency tests executable in the
offline runtime without replacing the Store or the production model boundary.
"""
import ast
import asyncio
import json
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace
import unittest

from tcg.diagnostics import Diagnostics, endpoint_origin, error_details
from tcg.schemas import DomainError
from tcg.storage import Store, uid


class CapturedInterrupt(BaseException):
    def __init__(self, value):
        self.value = value


def capture(value):
    raise CapturedInterrupt(value)


tree = ast.parse((Path(__file__).resolve().parents[1] / 'backend/tcg/graph.py').read_text())
original = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == 'Engine')
methods = {'request_boundary', 'observed_node', 'resume', 'cancel', 'invoke_model', '_invoke_model', 'trace'}
body = [node for node in original.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in methods]
definition = ast.ClassDef(name='ProductionMethods', bases=[], keywords=[], body=body, decorator_list=[])
namespace = {'__package__': 'tcg', 'asyncio': asyncio, 'json': json, 'time': time, 'uid': uid,
             'endpoint_origin': endpoint_origin, 'error_details': error_details, 'DomainError': DomainError,
             'GraphInterrupt': CapturedInterrupt, 'interrupt': capture}
exec(compile(ast.fix_missing_locations(ast.Module(body=[definition], type_ignores=[])), 'graph.py', 'exec'), namespace)
ProductionMethods = namespace['ProductionMethods']
workflow_tree = ast.parse((Path(__file__).resolve().parents[1] / 'backend/tcg/workflow.py').read_text())
workflow = next(node for node in workflow_tree.body if isinstance(node, ast.ClassDef) and node.name == 'WorkflowEngine')
guard = next(node for node in workflow.body if isinstance(node, ast.FunctionDef) and node.name == 'workflow_boundary')
exec(compile(ast.Module(body=[guard], type_ignores=[]), 'workflow.py', 'exec'), namespace)
ProductionMethods.workflow_boundary = namespace['workflow_boundary']


class BoundaryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.store = Store(self.directory.name)
        chat = self.store.create_chat(self.store.list('project')[0]['id'], 'Boundary')
        _, run = self.store.create_run(chat['id'], {'content': 'Generate', 'intent': 'generate_case',
            'mode': 'auto', 'experience': 'reliable'})
        self.rid = run['id']
        self.store.update_run(self.rid, status='running')
        self.engine = ProductionMethods()
        self.engine.store = self.store
        self.engine.diagnostics = Diagnostics(self.store)
        self.engine.settings = SimpleNamespace(value={'timeout_seconds': 5, 'provider': 'controlled',
            'model': 'controlled', 'base_url': 'http://localhost'})
        self.engine.tasks = {}
        self.engine.edit_tasks = {}
        self.engine.stopping = False
        self.engine.heartbeat_seconds = 30
        self.scheduled = []
        self.engine.schedule = self.scheduled.append

    async def asyncTearDown(self):
        namespace['interrupt'] = capture
        self.engine.diagnostics.close()
        self.store.close()
        self.directory.cleanup()

    async def test_pause_saves_current_stage_before_next_node_and_resume_preserves_it(self):
        entered, release = asyncio.Event(), asyncio.Event()
        async def scenarios(state):
            entered.set()
            await release.wait()
            artifact = self.store.artifact(self.rid, 'stage-result', 'answer', 'Completed stage',
                [{'id': 'row-1', 'title': 'Saved result', 'description': 'Durable', 'refs': []}])
            return {'scenario_ref': artifact['id']}
        calls = []
        async def cases(state):
            calls.append('cases')
            return {}
        self.engine.node_scenarios, self.engine.node_cases = scenarios, cases
        current = asyncio.create_task(self.engine.observed_node('scenarios')({'run_id': self.rid}))
        await entered.wait()
        self.engine.request_boundary(self.rid, 'pause')
        release.set()
        result = await current
        self.assertEqual(self.store.get('artifact', result['scenario_ref'])['items'][0]['id'], 'row-1')
        with self.assertRaises(CapturedInterrupt) as raised:
            await self.engine.workflow_boundary('cases')({'run_id': self.rid})
        self.assertEqual(raised.exception.value['type'], 'workflow_paused')
        self.assertEqual(calls, [])
        self.store.update_run(self.rid, status='waiting', interrupt=raised.exception.value,
            _interrupt_id='persisted-gate', _boundary_requested=None)
        self.engine.resume(self.rid, {'approved': True})
        self.assertEqual(self.scheduled, [self.rid])
        namespace['interrupt'] = lambda value: {'approved': True}
        self.store.update_run(self.rid, status='running')
        result = await self.engine.workflow_boundary('cases')({'run_id': self.rid})
        self.assertFalse(result['boundary_again'])
        await self.engine.observed_node('cases')({'run_id': self.rid})
        self.assertEqual(calls, ['cases'])
        self.assertIsNone(self.store.run(self.rid)['_boundary_node'])
        self.assertEqual(len(self.store.list('artifact')), 1)

    async def _blocked_call(self):
        entered, release = asyncio.Event(), asyncio.Event()
        class Model:
            async def generate(self, task, context):
                entered.set()
                await release.wait()
                return {'items': [{'id': 'late'}]}
        self.engine.gateway = Model()
        pending = asyncio.create_task(self.engine.invoke_model('generate_scenarios', {}, self.rid))
        await entered.wait()
        return pending, release

    async def test_cancel_rejects_late_model_result(self):
        pending, release = await self._blocked_call()
        self.engine.cancel(self.rid)
        release.set()
        with self.assertRaises(asyncio.CancelledError):
            await pending
        self.assertEqual(self.store.run(self.rid)['status'], 'cancelled')
        self.assertEqual(self.store.list('artifact'), [])

    async def test_changed_input_version_rejects_model_output(self):
        pending, release = await self._blocked_call()
        self.store.update_run(self.rid, input_version=1, _input_version=1)
        release.set()
        with self.assertRaises(DomainError) as raised:
            await pending
        self.assertEqual(raised.exception.status, 409)
        self.assertEqual(self.store.list('artifact'), [])

    async def test_new_boundary_uses_current_reason_after_a_previous_pause(self):
        self.store.update_run(self.rid, _boundary_reason='pause', _control_hold=False)
        self.engine.request_boundary(self.rid, 'write')
        with self.assertRaises(CapturedInterrupt) as raised:
            await self.engine.workflow_boundary('clarification_gate')({'run_id': self.rid})
        self.assertEqual(raised.exception.value['reason'], 'write')
        self.assertEqual(self.store.run(self.rid)['_boundary_node'], 'boundary_clarification_gate')
