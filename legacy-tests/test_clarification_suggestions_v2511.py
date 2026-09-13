"""Focused checks of production helpers and gates without optional server dependencies.

Load the unchanged function/class definitions with a stub superclass. This keeps the
checks runnable with stdlib Python when FastAPI/LangGraph/pytest are unavailable;
these checks do not claim to exercise the HTTP server or LangGraph persistence.
"""
import ast
import asyncio
import copy
import hashlib
import json
from pathlib import Path
import re
import unittest


ROOT = Path(__file__).resolve().parents[1]
flow_tree = ast.parse((ROOT / 'backend/tcg/flow.py').read_text())
namespace = {'DirectEngine': object, 're': re}
exec(compile(ast.Module(body=[node for node in flow_tree.body
    if isinstance(node, (ast.FunctionDef, ast.ClassDef))], type_ignores=[]),
    str(ROOT / 'backend/tcg/flow.py'), 'exec'), namespace)
valid_suggestions = namespace['valid_question_suggestions']
complete_suggestions = namespace['complete_question_suggestions']
completion_context = namespace['question_suggestion_context']
FlowEngine = namespace['FlowEngine']
workflow_tree = ast.parse((ROOT / 'backend/tcg/workflow.py').read_text())
workflow_class = next(node for node in workflow_tree.body
                      if isinstance(node, ast.ClassDef) and node.name == 'WorkflowEngine')
gate = next(node for node in workflow_class.body if isinstance(node, ast.AsyncFunctionDef)
            and node.name == 'node_clarification_gate')
exec(compile(ast.Module(body=[gate], type_ignores=[]),
    str(ROOT / 'backend/tcg/workflow.py'), 'exec'), namespace)


def candidate(question, confidence='assumption', refs=None):
    return {'question': question, 'answer': '建议暂将该条件列为待确认，按已明确规则设计。',
            'basis': '这是未确认的测试设计假设，采用前可修改。',
            'refs': refs if refs is not None else [], 'confidence': confidence}


EVIDENCE = {
    'src#P1': {'id': 'src#P1', 'role': 'primary', 'text': '登录失败时显示错误。'},
    'example#P1': {'id': 'example#P1', 'role': 'example', 'text': '示例中的账号锁定。'},
}


class Store:
    graph_version = 6

    def __init__(self):
        self.cache = {}
        self.artifact = None
        self.published = []

    def run(self, run_id):
        return {'status': 'running', 'graph_version': self.graph_version, 'mode': 'hitp', '_profile': {'language': '中文'}}

    def cache_get(self, run_id, key):
        return copy.deepcopy(self.cache.get((run_id, key)))

    def cache_set(self, run_id, key, value):
        self.cache[(run_id, key)] = copy.deepcopy(value)
        return value

    def assert_running(self, run_id):
        assert self.run(run_id)['status'] == 'running'

    def get(self, kind, artifact_id):
        return self.artifact

    def publish(self, *args, **kwargs):
        self.published.append((args, kwargs))


class StubEngine(FlowEngine):
    def __init__(self, response=None, error=None, fits=True):
        self.store = Store()
        self.response = response or {}
        self.error = error
        self.requests = []
        self.events = []
        self.can_fit = fits

    async def invoke_model(self, task, context, run_id):
        self.requests.append((task, copy.deepcopy(context)))
        if self.error:
            raise self.error
        return self.response

    def fits(self, task, context):
        return self.can_fit

    def trace(self, *args, **kwargs):
        self.events.append((args, kwargs))

    def stage(self, *args):
        pass

    def all_evidence(self, run_id):
        return list(EVIDENCE.values())


class ParseDomainError(Exception):
    def __init__(self):
        super().__init__('invalid JSON')
        self.raw_response = '{"question_suggestions": [broken'
        self.parse_error = {'line': 1, 'column': 27}


# Retain the real method inside a class so zero-argument super() uses the actual
# production wrapper, with only its model transport superclass replaced.
invocation_class = copy.deepcopy(workflow_class)
invocation_class.name = 'InvocationEngine'
invocation_class.bases = [ast.Name(id='StubEngine', ctx=ast.Load())]
invocation_class.keywords = []
invocation_class.body = [node for node in invocation_class.body
    if isinstance(node, ast.AsyncFunctionDef) and node.name == 'invoke_model']
namespace.update(StubEngine=StubEngine, DomainError=ParseDomainError,
                 hashlib=hashlib, json=json)
exec(compile(ast.fix_missing_locations(ast.Module(body=[invocation_class], type_ignores=[])),
    str(ROOT / 'backend/tcg/workflow.py'), 'exec'), namespace)
InvocationEngine = namespace['InvocationEngine']


class CapturedInterrupt(BaseException):
    def __init__(self, payload):
        self.payload = payload


def capture_interrupt(payload):
    raise CapturedInterrupt(payload)


namespace['interrupt'] = capture_interrupt


class SuggestionValidationTests(unittest.TestCase):
    def test_supported_needs_real_non_example_refs_but_assumption_can_be_unreferenced(self):
        assumption = candidate('是否锁定？')
        self.assertEqual(valid_suggestions({'questions': ['是否锁定？'],
            'question_suggestions': [assumption]}, EVIDENCE), [assumption])
        supported = candidate('是否锁定？', 'supported', ['src#P1'])
        self.assertEqual(valid_suggestions({'questions': ['是否锁定？'],
            'question_suggestions': [supported]}, EVIDENCE), [supported])
        for invalid in [candidate('是否锁定？', 'supported'),
                        candidate('是否锁定？', refs=['example#P1']),
                        candidate('是否锁定？', refs=['invented#P1']),
                        candidate('是否锁定？', refs=[{}]),
                        {**assumption, 'answer': '  '}, {**assumption, 'basis': ''},
                        {**assumption, 'question': ' 是否锁定？'},
                        {**assumption, 'confidence': 'certain'}]:
            with self.subTest(invalid=invalid):
                self.assertEqual(valid_suggestions({'questions': ['是否锁定？'],
                    'question_suggestions': [invalid]}, EVIDENCE), [])

    def test_duplicates_rejected_and_unconfirmed_label_added(self):
        raw = {**candidate('是否锁定？'), 'basis': '建议保守处理。'}
        result = valid_suggestions({'questions': ['是否锁定？'],
            'question_suggestions': [raw, raw, candidate('额外问题？')]}, EVIDENCE)
        self.assertEqual(len(result), 1)
        self.assertIn('未确认', result[0]['basis'])
        self.assertEqual(raw['basis'], '建议保守处理。')

    def test_fallback_covers_exact_unique_questions_without_inventing_business_facts(self):
        report = {'questions': ['锁定多久？', '允许哪些角色？', '锁定多久？'],
                  'question_suggestions': None}
        result = complete_suggestions(report, EVIDENCE)
        self.assertEqual([item['question'] for item in result], ['锁定多久？', '允许哪些角色？'])
        for item in result:
            self.assertEqual(item['refs'], [])
            self.assertEqual(item['confidence'], 'assumption')
            self.assertIn(item['question'], item['answer'])
            self.assertIn('不新增限制或例外', item['answer'])
            self.assertIn('假设', item['basis'])
        self.assertIsNone(report['question_suggestions'])

    def test_completion_context_is_bounded_and_explicit_about_omissions(self):
        evidence = {**EVIDENCE, **{f'large#{i}': {'id': f'large#{i}', 'role': 'primary',
                    'text': '账号锁定。' * 600} for i in range(9)}}
        context = completion_context({'summary': '长摘要' * 2000}, evidence, ['是否锁定账号？'], '中文')
        self.assertEqual(context['questions'], ['是否锁定账号？'])
        self.assertLessEqual(len(context['analysis_context']['summary']), 1600)
        self.assertLessEqual(sum(len(item['text']) for item in context['evidence']), 5000)
        self.assertEqual([item['id'] for item in context['evidence']], ['src#P1'])
        self.assertTrue(context['evidence_scope']['partial'])
        self.assertEqual(context['evidence_scope']['available_chunks'], 10)


class SuggestionCompletionTests(unittest.IsolatedAsyncioTestCase):
    async def test_actual_workflow_wrapper_does_not_repair_completion_json(self):
        engine = InvocationEngine(error=ParseDomainError())
        engine.store.graph_version = 7
        report = {'questions': ['是否锁定？']}
        first = await engine.ensure_question_suggestions('run', 'key', report, EVIDENCE)
        second = await engine.ensure_question_suggestions('run', 'key', report, EVIDENCE)
        self.assertEqual(first, complete_suggestions(report, EVIDENCE))
        self.assertEqual(second, first)
        self.assertEqual(len(engine.requests), 1)
        self.assertNotIn('json_repair', engine.requests[0][1])
        self.assertFalse(any(key.startswith('json_hint:') for _, key in engine.store.cache))

    async def test_actual_workflow_wrapper_keeps_json_repair_for_normal_tasks(self):
        engine = InvocationEngine(error=ParseDomainError())
        engine.store.graph_version = 7
        with self.assertRaises(ParseDomainError):
            await engine.invoke_model('dialogue', {'questions': []}, 'run')
        self.assertEqual(len(engine.requests), 3)
        self.assertNotIn('json_repair', engine.requests[0][1])
        for _, context in engine.requests[1:]:
            self.assertEqual(context['json_repair']['previous_response_text'], engine.error.raw_response)
        self.assertTrue(any(key.startswith('json_hint:') for _, key in engine.store.cache))

    async def test_complete_initial_response_adds_no_model_request(self):
        report = {'questions': ['是否锁定？'], 'question_suggestions': [candidate('是否锁定？')]}
        engine = StubEngine()
        result = await engine.ensure_question_suggestions('run', 'key', report, EVIDENCE)
        self.assertEqual(result, report['question_suggestions'])
        self.assertEqual(engine.requests, [])

    async def test_only_missing_question_is_completed_once_and_result_is_cached(self):
        existing = candidate('错误提示？', 'supported', ['src#P1'])
        report = {'questions': ['错误提示？', '是否锁定？'],
                  'summary': '理解登录规则', 'question_suggestions': [existing]}
        engine = StubEngine({'question_suggestions': [candidate('是否锁定？'), candidate('额外问题？')]})
        first = await engine.ensure_question_suggestions('run', 'key', report, EVIDENCE)
        second = await engine.ensure_question_suggestions('run', 'key', report, EVIDENCE)
        self.assertEqual(first, second)
        self.assertEqual(first, [existing, candidate('是否锁定？')])
        self.assertEqual(len(engine.requests), 1)
        task, context = engine.requests[0]
        self.assertEqual(task, 'complete_question_suggestions')
        self.assertEqual(context['questions'], ['是否锁定？'])
        self.assertNotIn('items', context)
        self.assertNotIn('conversation', context)

    async def test_invalid_completion_and_model_failure_fall_back_without_retry(self):
        report = {'questions': ['是否锁定？', '锁定多久？']}
        for engine in [StubEngine({'question_suggestions': [candidate('是否锁定？', 'supported'),
                           candidate('锁定多久？', refs=['example#P1'])]}),
                       StubEngine(error=RuntimeError('model unavailable'))]:
            with self.subTest(error=engine.error):
                first = await engine.ensure_question_suggestions('run', 'key', report, EVIDENCE)
                second = await engine.ensure_question_suggestions('run', 'key', report, EVIDENCE)
                self.assertEqual(first, complete_suggestions(report, EVIDENCE))
                self.assertEqual(second, first)
                self.assertEqual(len(engine.requests), 1)

    async def test_completion_cannot_cite_omitted_chunks(self):
        omitted = {'id': 'src#long', 'role': 'primary', 'text': '超长原文' * 1000}
        evidence = {**EVIDENCE, omitted['id']: omitted}
        report = {'questions': ['锁定多久？']}
        engine = StubEngine({'question_suggestions': [candidate('锁定多久？', 'supported', ['src#long'])]})
        result = await engine.ensure_question_suggestions('run', 'key', report, evidence)
        self.assertEqual(result, complete_suggestions(report, evidence))

    async def test_capacity_failure_skips_completion_and_caches_fallback(self):
        report = {'questions': ['是否锁定？']}
        engine = StubEngine(fits=False)
        result = await engine.ensure_question_suggestions('run', 'key', report, EVIDENCE)
        self.assertEqual(result, complete_suggestions(report, EVIDENCE))
        self.assertEqual(engine.requests, [])
        self.assertTrue(engine.store.cache)

    async def test_cancellation_is_not_swallowed_as_a_suggestion_failure(self):
        engine = StubEngine(error=asyncio.CancelledError())
        with self.assertRaises(asyncio.CancelledError):
            await engine.ensure_question_suggestions('run', 'key', {'questions': ['是否锁定？']}, EVIDENCE)
        self.assertFalse(engine.store.cache)

    async def test_both_gates_cover_existing_artifact_questions_and_reuse_completion(self):
        for gate_call in [namespace['node_clarification_gate'], FlowEngine.node_clarify]:
            with self.subTest(gate=gate_call.__name__):
                engine = StubEngine()
                engine.store.artifact = {'id': 'analysis', 'revision': 1,
                    'report': {'questions': ['是否锁定？', '锁定多久？']}}
                for _ in range(2):
                    with self.assertRaises(CapturedInterrupt) as caught:
                        await gate_call(engine, {'run_id': 'run', 'analysis_ref': 'analysis'})
                    payload = caught.exception.payload
                    self.assertEqual(payload['type'], 'clarification')
                    self.assertEqual(payload['questions'], ['是否锁定？', '锁定多久？'])
                    self.assertEqual([item['question'] for item in payload['question_suggestions']], payload['questions'])
                    self.assertTrue(all(item['confidence'] == 'assumption' for item in payload['question_suggestions']))
                self.assertEqual(len(engine.requests), 1)


if __name__ == '__main__':
    unittest.main()
