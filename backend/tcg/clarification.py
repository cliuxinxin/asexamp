"""Normalize pending questions and explicitly submitted answers without inference."""
import copy

from .case_fields import filled
from .schemas import DomainError


def question_key(value):
    return ' '.join(value.split()) if isinstance(value, str) else ''


def pending_questions(report):
    """A present canonical questions field, including [], supersedes legacy aliases."""
    report = report or {}
    raw = report.get('questions') if 'questions' in report else (
        report.get('clarification_questions') or report.get('clarifications') or [])
    result, seen = [], set()
    for index, raw_value in enumerate(raw if isinstance(raw, list) else []):
        value = {'question': raw_value} if isinstance(raw_value, str) else raw_value
        if not isinstance(value, dict) or filled(value.get('answer')) or value.get('resolved'):
            continue
        question = value.get('question') or value.get('text')
        key = question_key(question)
        if not key or key in seen:
            continue
        seen.add(key)
        identifier = value.get('id')
        identifier = identifier.strip() if isinstance(identifier, str) and identifier.strip() else 'Q' + str(index + 1)
        result.append({**copy.deepcopy(value), 'id': identifier, 'question': question.strip()})
    return result


def submitted_answers(questions, answers):
    """Map known IDs or question text; unstructured legacy text is supplementary only."""
    if isinstance(answers, str):
        return answers.strip(), {}
    if isinstance(answers, dict):
        entries = answers.items()
    elif isinstance(answers, list) and all(isinstance(row, dict) for row in answers):
        entries = [(row.get('question') or row.get('text') or row.get('id'), row.get('answer')) for row in answers]
    else:
        raise DomainError('请提供或采用具体澄清答案')
    aliases = {}
    for question in questions:
        for value in (question['id'], question['question']):
            aliases.setdefault(question_key(value), set()).add(question['question'])
    resolved = {}
    for key, value in entries:
        matches = aliases.get(question_key(key), set())
        if len(matches) != 1:
            raise DomainError('澄清答案没有对应当前待回答的问题，请查看最新问题后重新提交')
        if not filled(value):
            continue
        if not isinstance(value, (str, bool, int, float)):
            raise DomainError('澄清答案需要是具体文字，不能是对象或列表')
        question = next(iter(matches))
        answer = str(value).strip()
        if question in resolved and resolved[question] != answer:
            raise DomainError('同一个澄清问题提交了不同答案，请保留一个明确答案')
        resolved[question] = answer
    # Canonical question order makes an ID-based retry use the same durable source.
    ordered = {question['question']: resolved[question['question']] for question in questions
               if question['question'] in resolved}
    return '\n\n'.join(question + '\n' + answer for question, answer in ordered.items()), ordered
