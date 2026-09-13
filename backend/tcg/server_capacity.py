"""Recognize explicit provider context rejections without exposing provider text."""
import re

from .schemas import DomainError


class ContextCapacityError(DomainError):
    """The provider rejected this input; retry only after changing the request."""
    category = 'context_capacity'
    retryable = False

    def __init__(self, context_limit_tokens=None, input_tokens=None, status_code=None, *, message=None):
        self.context_limit_tokens = _positive_integer(context_limit_tokens)
        self.input_tokens = _positive_integer(input_tokens)
        self.status_code = status_code
        limit = f'（服务报告容量 {self.context_limit_tokens} tokens）' if self.context_limit_tokens else ''
        # A custom message must be caller-authored, never raw provider text.
        super().__init__(message or ('模型服务器返回上下文超限' + limit + '；需要拆分本次输入后再请求。'), 413)


def _positive_integer(value):
    if isinstance(value, bool):
        return None
    try:
        number = int(str(value).replace(',', ''))
        return number if 0 < number <= 1_000_000_000 else None
    except (ValueError, TypeError):
        return None


_CONTEXT_CODES = {
    'context_length_exceeded', 'context_window_exceeded', 'context_length_error',
    'context_window_overflow', 'prompt_too_long', 'input_too_long',
}
_CONTEXT_PATTERNS = (
    r'(?:context\s*(?:length|window|capacity)|maximum\s+model\s+(?:length|context)).{0,120}(?:exceed|too\s+(?:long|large)|limit|maximum)',
    r'(?:exceed|too\s+(?:long|large)|maximum|longer\s+than).{0,120}context\s*(?:length|window|capacity)',
    r'(?:prompt|input)\s+(?:is\s+)?too\s+long',
    r'(?:上下文|提示词).{0,60}(?:超出|超过|超限|过长).{0,60}(?:长度|容量|窗口|token|限制|上限)',
    r'输入.{0,30}(?:tokens?|词元).{0,30}(?:超出|超过|超限|过长)',
    r'(?:超出|超过|超限).{0,60}上下文',
    r'上下文.{0,40}(?:过长|超限)',
)
_LIMIT_PATTERNS = (
    r'(?:maximum\s+(?:model\s+)?(?:context\s+)?length|context\s*(?:length|window|capacity))(?:\s+(?:limit|size))?\s*(?:is|of|:|=)?\s*([\d,]+)',
    r'max(?:imum)?[_\s-]*(?:model[_\s-]*len|context[_\s-]*(?:tokens|length))\s*[:=]\s*([\d,]+)',
    r'(?:最大(?:值|长度|容量)?|上限)\s*(?:为|是|：|:|=)?\s*([\d,]+)\s*(?:tokens?|词元)?',
)


def context_capacity_error(payload, status_code=None):
    """Return a safe typed error only for explicit 400/413/422 context failures.

    A generic HTTP 413 is a transport/body-size limit, not evidence of a model
    context limit. Authentication and output-generation errors remain separate.
    Only error metadata is inspected; no provider message is retained.
    """
    if status_code not in (None, 400, 413, 422):
        return None
    texts, codes, numbers = [], [], {}
    def collect(value, depth=0):
        if depth > 5:
            return
        if isinstance(value, str):
            texts.append(value[:16000])
        elif isinstance(value, list):
            for part in value[:16]:
                collect(part, depth + 1)
        elif isinstance(value, dict):
            for key in ('code', 'type'):
                if isinstance(value.get(key), str):
                    codes.append(value[key].lower().replace('-', '_'))
            for key in ('context_limit_tokens', 'max_context_length', 'maximum_context_length',
                        'max_model_len', 'input_tokens', 'prompt_tokens'):
                if key in value:
                    numbers[key] = _positive_integer(value[key])
            for key in ('error', 'errors', 'detail', 'message'):
                if key in value:
                    collect(value[key], depth + 1)
    collect(payload)
    message = ' '.join(texts)[:64000]
    if not (_CONTEXT_CODES.intersection(codes) or any(
            re.search(pattern, message, re.IGNORECASE) for pattern in _CONTEXT_PATTERNS)):
        return None
    limit = next((numbers.get(key) for key in ('context_limit_tokens', 'max_context_length',
                    'maximum_context_length', 'max_model_len') if numbers.get(key)), None)
    if limit is None:
        for pattern in _LIMIT_PATTERNS:
            match = re.search(pattern, message, re.IGNORECASE)
            if match:
                limit = _positive_integer(match[1])
                if limit:
                    break
    return ContextCapacityError(limit, numbers.get('input_tokens') or numbers.get('prompt_tokens'), status_code)
