"""One versioned clarification draft for chat, cards and workflow resumption."""
import copy
import hashlib
import json
import re

from .documents import parse_text
from .schemas import DomainError
from .storage import now, public


def _version(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()[:20]


def _stored(store, run_id):
    try:
        return store.get('clarification_draft', 'draft_' + run_id)
    except DomainError as exc:
        if exc.status != 404:
            raise
        return None


def get_draft(store, run_id):
    """Read without an edit lease; initialize only the authoritative draft record."""
    with store.transaction():
        run = store.run(run_id)
        saved = _stored(store, run_id)
        gate = run.get('interrupt', {})
        if gate.get('type') != 'clarification':
            if saved:
                return public(saved)
            raise DomainError('当前没有澄清问题', 409)
        questions = list(dict.fromkeys(q for q in gate.get('questions', []) if isinstance(q, str)))
        question_set = _version(questions)
        if saved and saved['question_set_version'] == question_set:
            return public(saved)
        evidence = {e['id'] for e in store.evidence(run['_source_ids'], run.get('_source_roles'))
                    if e['role'] != 'example'}
        suggestions = {item.get('question'): item for item in gate.get('question_suggestions', [])
                       if isinstance(item, dict)}
        previous = {q['id']: q for q in (saved or {}).get('questions', [])}
        rows = []
        for question in questions:
            qid = 'q_' + _version(question)
            suggestion = suggestions.get(question, {})
            refs = suggestion.get('refs', [])
            supported = (suggestion.get('confidence') == 'supported' and isinstance(refs, list)
                         and bool(refs) and all(isinstance(ref, str) and ref in evidence for ref in refs))
            value = {'answer': str(suggestion.get('answer') or '暂按已有明确需求设计，未明确条件保留待确认。'),
                     'basis': str(suggestion.get('basis') or '当前没有明确依据，这是待确认假设。'),
                     'refs': refs if supported else [], 'confidence': 'supported' if supported else 'assumption'}
            if not supported and not any(term in value['basis'] for term in ('待确认', '假设', '未确认')):
                value['basis'] += '（待确认假设）'
            old = previous.get(qid, {})
            rows.append({'id': qid, 'question': question, 'answer': old.get('answer', ''),
                         'suggestion': value, 'adopted': bool(old.get('adopted') and old.get('answer'))})
        draft = {'id': 'draft_' + run_id, 'run_id': run_id, 'project_id': run['project_id'],
                 'chat_id': run['chat_id'], 'revision': (saved or {}).get('revision', 0) + 1,
                 'question_set_version': question_set, 'questions': rows, 'answer': '',
                 'submitted': False, 'source_id': (saved or {}).get('source_id'), 'shared': False,
                 '_freeform_answer': '', 'updated_at': now()}
        draft['answer'] = _answer(draft)
        store.put('clarification_draft', draft)
        return public(draft)


def _answer(draft):
    values = [q['question'] + '\n' + q['answer'].strip() for q in draft['questions'] if q['answer'].strip()]
    if draft.get('_freeform_answer', '').strip():
        values.append(draft['_freeform_answer'].strip())
    return '\n\n'.join(values)


def _replace_answer(draft, answer):
    """The editor sends the whole textarea; it replaces the previous text.

    Reconcile exact question headings back into stable rows. Unstructured text
    remains authoritative freeform without silently retaining old row answers.
    """
    if answer.strip() == draft.get('answer', '').strip():
        return
    rows = {q['question']: q for q in draft['questions']}
    pattern = r'(?m)^(' + '|'.join(re.escape(question) for question in rows) + r')\s*\n' if rows else None
    matches = list(re.finditer(pattern, answer)) if pattern else []
    values = {}
    if matches:
        for index, match in enumerate(matches):
            end = matches[index + 1].start() if index + 1 < len(matches) else len(answer)
            question = match.group(1)
            if question in values:
                raise DomainError('同一澄清问题不能重复填写，请合并后保存')
            values[question] = answer[match.end():end].strip()
        draft['_freeform_answer'] = answer[:matches[0].start()].strip()
    elif len(rows) == 1:
        values[next(iter(rows))] = answer.strip()
        draft['_freeform_answer'] = ''
    else:
        draft['_freeform_answer'] = answer
    for question, row in rows.items():
        row['answer'] = values.get(question, '')
        if not row['answer']:
            row['adopted'] = False


def _current(store, run_id, args, editing=True):
    get_draft(store, run_id)
    draft = _stored(store, run_id)
    run = store.run(run_id)
    if editing and (run['status'] != 'waiting' or run.get('interrupt', {}).get('type') != 'clarification'):
        raise DomainError('澄清节点已改变，请刷新后操作', 409)
    if args.get('expected_revision') is not None and args['expected_revision'] != draft['revision']:
        raise DomainError('澄清草稿已更新，请刷新后再保存', 409)
    if args.get('question_set_version') is not None and args['question_set_version'] != draft['question_set_version']:
        raise DomainError('澄清问题已更新，请刷新问题集合', 409)
    return run, draft


def update_draft(store, run_id, args):
    with store.transaction():
        _, draft = _current(store, run_id, args)
        before = copy.deepcopy(draft)
        rows = {q['id']: q for q in draft['questions']}
        answers = args.get('answers', {})
        if not isinstance(answers, dict) or not set(answers) <= rows.keys() or any(
                not isinstance(value, str) or len(value) > 100000 for value in answers.values()):
            raise DomainError('answers 必须使用当前问题 ID 和答案文本')
        adopt = args.get('adopt_ids', [])
        if not isinstance(adopt, list) or any(not isinstance(qid, str) or qid not in rows for qid in adopt):
            raise DomainError('采用的问题 ID 不属于当前问题集合')
        if args.get('adopt_all'):
            adopt = list(rows)
        for qid in adopt:
            rows[qid].update(answer=rows[qid]['suggestion']['answer'], adopted=True)
        for qid, value in answers.items():
            rows[qid]['answer'] = value
            if not value.strip():
                rows[qid]['adopted'] = False
        if 'answer' in args:
            if not isinstance(args['answer'], str) or len(args['answer']) > 100000:
                raise DomainError('澄清答案必须为不超过 10 万字符的文本')
            _replace_answer(draft, args['answer'])
        draft['answer'] = _answer(draft)
        if draft != before:
            draft.update(revision=draft['revision'] + 1, submitted=False, shared=False, updated_at=now())
            store.put('clarification_draft', draft)
        return public(draft)


def save_draft(store, run_id, args=None):
    args = args or {}
    with store.transaction():
        run, draft = _current(store, run_id, args)
        if any(key in args for key in ('answer', 'answers', 'adopt_ids', 'adopt_all')):
            update_draft(store, run_id, args)
            draft = _stored(store, run_id)
        if draft['submitted'] and draft.get('source_id'):
            return public(draft)
        if not draft['answer'].strip():
            raise DomainError('请填写或采用澄清答案后保存')
        text = '问题：\n' + '\n'.join(q['question'] for q in draft['questions']) + '\n用户确认的回答：\n' + draft['answer']
        text, chunks = parse_text(text)
        source = store.add_source(run['chat_id'], '用户确认的澄清', 'clarification', text, chunks)
        source['_clarification_draft'] = {'id': draft['id'], 'revision': draft['revision'],
            'question_set_version': draft['question_set_version'], 'questions': copy.deepcopy(draft['questions']),
            'confirmation': '用户提交；建议和假设的原始出处保留，不代表原文事实'}
        old_id = draft.get('source_id')
        if old_id:
            old = store.get('source', old_id)
            store.put('source', {**old, '_active': False, '_project_shared': False, '_superseded_by': source['id']})
            source['_supersedes'] = old_id
        store.put('source', source)
        source_ids = [sid for sid in run['_source_ids'] if sid != old_id] + [source['id']]
        roles = {sid: role for sid, role in run.get('_source_roles', {}).items() if sid != old_id}
        version = run.get('input_version', 0) + 1
        store.update_run(run_id, _source_ids=list(dict.fromkeys(source_ids)),
            _source_roles={**roles, source['id']: 'clarification'}, input_version=version, _input_version=version)
        draft.update(revision=draft['revision'] + 1, submitted=True, source_id=source['id'], shared=False, updated_at=now())
        store.put('clarification_draft', draft)
        store.audit(draft['id'], 'clarification_saved', {'source_id': source['id'], 'revision': draft['revision']})
        return public(draft)


def share_draft(store, run_id, args=None):
    from .project_context import share_clarification
    with store.transaction():
        run, draft = _current(store, run_id, args or {}, editing=False)
        if not draft['submitted'] or not draft.get('source_id'):
            raise DomainError('请先提交保存澄清，再共享到项目')
        if not draft.get('shared'):
            share_clarification(store, draft['source_id'], run['project_id'])
            draft.update(revision=draft['revision'] + 1, shared=True, updated_at=now())
            store.put('clarification_draft', draft)
        return public(draft)


def register_routes(app, store=None, engine=None):
    from pydantic import BaseModel, Field
    class DraftUpdate(BaseModel):
        expected_revision: int = Field(ge=1)
        question_set_version: str | None = None
        answer: str | None = None
        answers: dict[str, str] | None = None
        adopt_ids: list[str] | None = None
        adopt_all: bool | None = None

    @app.get('/api/runs/{run_id}/clarification-draft')
    def read_clarification_draft(run_id: str):
        return get_draft(store or app.state.store, run_id)

    @app.patch('/api/runs/{run_id}/clarification-draft')
    def patch_clarification_draft(run_id: str, body: DraftUpdate):
        return update_draft(store or app.state.store, run_id, body.model_dump(exclude_none=True))
