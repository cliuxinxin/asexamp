"""One bounded owner for native schema, reference and business corrections."""
import copy
from contextlib import contextmanager
from contextvars import ContextVar

from jsonschema import Draft202012Validator

from .schemas import DomainError


REPAIR_LIMIT = 3
_EXTERNAL_REPAIR = ContextVar('tcg_external_generation_repair', default=False)


@contextmanager
def single_native_attempt():
    token = _EXTERNAL_REPAIR.set(True)
    try:
        yield
    finally:
        _EXTERNAL_REPAIR.reset(token)


def external_repair_active():
    return _EXTERNAL_REPAIR.get()


class NativeSubmissionError(DomainError):
    """A model-content error carrying the untouched submitted arguments."""

    def __init__(self, message, candidate, call_id=None, details=None):
        super().__init__(message)
        self.category, self.candidate = 'schema', copy.deepcopy(candidate)
        self.call_id, self.details = call_id, details or {}


def schema_issue(error):
    path = '.'.join(map(str, error.absolute_path)) or '$'
    details = {'path': path, 'constraint': error.validator}
    if error.validator == 'additionalProperties' and isinstance(error.instance, dict):
        details['unexpected_fields'] = sorted(set(error.instance) - set(error.schema.get('properties', {})))
    if error.validator == 'required' and isinstance(error.instance, dict):
        details['missing_fields'] = [field for field in error.validator_value if field not in error.instance]
    suffix = (' Unexpected fields: ' + ', '.join(details['unexpected_fields'])
              if details.get('unexpected_fields') else '')
    return f'Field {path} fails the {error.validator} constraint.{suffix} Correct the arguments using the declared schema.', details


class GenerationRepairExhausted(DomainError):
    """Unpublished candidates remain reviewable when all corrections fail."""

    def __init__(self, task, candidate, history, error, candidate_key=None):
        super().__init__(f'自动修正 {REPAIR_LIMIT} 次后仍未通过校验：{error}。生成内容已保留为待核对草稿。')
        self.task, self.candidate = task, copy.deepcopy(candidate)
        self.candidate_history = copy.deepcopy(history)
        self.attempts, self.retry_count = len(history), max(0, len(history) - 1)
        self.validation_error, self.details = str(error), copy.deepcopy(getattr(error, 'details', {}))
        self.category = getattr(error, 'category', None) or 'validation'
        self.call_id = getattr(error, 'call_id', None) or (history[-1].get('call_id') if history else None)
        self.missing_ids = self.details.get('missing_input_ids', [])
        self.item_ids = list(getattr(error, 'item_ids', []))
        self.candidate_key, self.completed_batches = candidate_key, []


async def repair_submission(call, validate, task, context, schema, instruction, *,
                            save_history=None, candidate_key=None, diagnostics=None,
                            previous_state=None, retry_round=0, save_progress=None):
    """First request plus at most three corrections, including refs-only calls.

    Every response is archived before interpretation; only a fully validated
    result is returned to the successful batch cache. Transport/state failures
    escape immediately and never consume another model-content correction.
    """
    from .reference_repair import repair_reference_fields

    previous_state = previous_state or {}
    prior_history = previous_state.get('history', [])
    resumed = previous_state.get('round', 0) == retry_round
    history = copy.deepcopy(prior_history) if resumed else []
    candidate = copy.deepcopy(previous_state.get('candidate'))
    replay = history[-1] if history and not history[-1].get('validation_error') else None
    failure = None
    if prior_history and prior_history[-1].get('validation_error'):
        saved = prior_history[-1]
        failure = DomainError(saved['validation_error'])
        failure.category = saved.get('category', 'validation')
        failure.details = copy.deepcopy(saved.get('details', {}))
        failure.call_id = saved.get('call_id')

    def archive():
        if save_history:
            save_history({'history': copy.deepcopy(history), 'candidate': copy.deepcopy(candidate), 'round': retry_round})

    def record_failure(error):
        nonlocal failure
        failure = error
        if not getattr(error, 'category', None):
            issue = getattr(error, 'issue', {})
            error.category = issue.get('code', 'validation')
        details = copy.deepcopy(getattr(error, 'details', None) or getattr(error, 'issue', {}))
        error.details = details
        if history:
            error.call_id = getattr(error, 'call_id', None) or history[-1].get('call_id')
            history[-1].update(validation_error=str(error), category=error.category, details=details)
        archive()
        if diagnostics:
            diagnostics.record('batch.validation_failed', level='ERROR', task=task,
                call_id=getattr(error, 'call_id', None), errors=[str(error)],
                category=error.category, item_ids=getattr(error, 'item_ids', []),
                attempt=len(history), retry_count=max(0, len(history) - 1), **details)

    async def invoke(current_task, current_context, current_schema, current_instruction):
        nonlocal replay
        if replay is not None and replay['task'] == current_task:
            from .native_model import NativeResult
            saved, replay = replay, None
            result = copy.deepcopy(saved['result'])
            issue = next(Draft202012Validator(current_schema).iter_errors(result), None)
            if issue is not None:
                message, details = schema_issue(issue)
                raise NativeSubmissionError(message, result, saved.get('call_id'), details)
            return NativeResult(result, saved.get('call_id'))
        if current_task == 'repair_evidence_refs' and failure is None:
            error = DomainError('生成条目的证据引用缺失、无效或引用了示例；需要依据当前资料修正引用。')
            error.category = 'invalid_reference'
            error.item_ids = [row['id'] for row in current_context['items']]
            error.details = {'invalid_reference_item_ids': error.item_ids,
                             'expected': 'provided_non_example_evidence_id'}
            record_failure(error)
        if len(history) >= REPAIR_LIMIT + 1:
            raise GenerationRepairExhausted(task, candidate, history, failure, candidate_key)
        actual_context = copy.deepcopy(current_context)
        if failure is not None:
            previous = (history or prior_history)[-1]
            actual_context['validation_repair'] = {
                'attempt': len(history), 'max_retries': REPAIR_LIMIT,
                'validation_error': str(failure), 'category': getattr(failure, 'category', 'validation'),
                'details': copy.deepcopy(getattr(failure, 'details', {})),
                'previous_result': copy.deepcopy(candidate),
                'previous_submission': copy.deepcopy(previous['result']),
                'instruction': 'Correct this rejected result against the same inputs. Preserve valid rows and stable IDs. '
                    'Return the complete corrected result for the current function, not a patch. '
                    'Do not invent evidence, silently drop rows or remove invalid references just to pass.'}
            if diagnostics:
                diagnostics.record('model.content_retry', task=task, retry_count=len(history),
                    previous_call_id=previous.get('call_id'), category=getattr(failure, 'category', 'validation'))
            if save_progress:
                save_progress({'task': task, 'retry_count': len(history), 'max_retries': REPAIR_LIMIT,
                    'message': str(failure), 'call_id': previous.get('call_id')})
        try:
            with single_native_attempt():
                result = await call(current_task, actual_context, current_schema, current_instruction)
        except NativeSubmissionError as error:
            history.append({'attempt': len(history) + 1, 'task': current_task,
                'result': copy.deepcopy(error.candidate), 'call_id': error.call_id})
            archive()
            raise
        history.append({'attempt': len(history) + 1, 'task': current_task,
            'result': copy.deepcopy(result), 'call_id': getattr(result, 'call_id', None)})
        archive()
        issue = next(Draft202012Validator(current_schema).iter_errors(result), None)
        if issue is not None:
            message, details = schema_issue(issue)
            raise NativeSubmissionError(message, result, getattr(result, 'call_id', None), details)
        return result

    def retain_reference_candidate(value):
        nonlocal candidate
        candidate = copy.deepcopy(value)
        archive()

    need_generation = candidate is None or not resumed or getattr(failure, 'category', '') != 'invalid_reference'
    if replay is not None and replay['task'] == 'repair_evidence_refs':
        need_generation = False
    while True:
        if need_generation:
            try:
                candidate = await invoke(task, context, schema, instruction)
                failure = None
            except NativeSubmissionError as error:
                candidate = copy.deepcopy(error.candidate)
                record_failure(error)
                continue
        prior_count = len(history)
        try:
            candidate = await repair_reference_fields(invoke, task, context, candidate,
                save_candidate=retain_reference_candidate, diagnostics=diagnostics, strict=True)
        except NativeSubmissionError as error:
            record_failure(error)
            need_generation = False
            continue
        except DomainError as error:
            if isinstance(error, GenerationRepairExhausted) or not getattr(error, 'repairable', False):
                raise
            record_failure(error)
            need_generation = len(history) == prior_count
            continue
        try:
            validate(task, context, candidate)
        except DomainError as error:
            # Only the pure local content validator is retried here, never DB writes.
            if error.status != 400 or getattr(error, 'category', None) in (
                    'connection', 'authentication', 'configuration', 'timeout',
                    'permission', 'dependency', 'state_conflict'):
                raise
            record_failure(error)
            need_generation = True
            continue
        archive()
        if save_progress:
            save_progress(None)
        return candidate
