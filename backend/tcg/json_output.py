"""Decode a single model JSON object and retain offsets into the original reply."""
import json
import re


def parse_model_object(raw):
    begin=len(raw)-len(raw.lstrip())
    end=len(raw.rstrip())
    fence=re.match(r'```[a-zA-Z]*[ \t]*\r?\n',raw[begin:end])
    if fence:
        begin+=fence.end()
        closing=re.search(r'\r?\n[ \t]*```[ \t]*$',raw[begin:end])
        if closing:end=begin+closing.start()
    payload=raw[begin:end]
    leading=len(payload)-len(payload.lstrip())
    begin+=leading;payload=payload[leading:]
    # Keep support for an explanatory prefix, without rewriting JSON strings.
    if payload and payload[0] not in '{[':
        brace=payload.find('{')
        if brace>=0:begin+=brace;payload=payload[brace:]
    try:
        result,consumed=json.JSONDecoder().raw_decode(payload)
    except json.JSONDecodeError as exc:
        raise json.JSONDecodeError(exc.msg,raw,begin+exc.pos) from None
    if not isinstance(result,dict):raise ValueError('Expected one JSON object, received '+type(result).__name__)
    tail=payload[consumed:].strip()
    if tail and (tail[0] in '},]' or '{' in tail or '[' in tail):
        raise json.JSONDecodeError('Extra content after JSON object',raw,begin+consumed)
    return result


def parse_issue(exc):
    return {'type':type(exc).__name__,'message':getattr(exc,'msg',str(exc)),
            'line':getattr(exc,'lineno',None),'column':getattr(exc,'colno',None),
            'position':getattr(exc,'pos',None)}


def close_finished_containers(raw):
    """Close containers only after complete nested values, never complete a value."""
    text=raw.rstrip()
    if not text.startswith('{') or not text.endswith(('}',']')):return None
    try:parse_model_object(text);return None
    except json.JSONDecodeError as exc:
        if exc.pos!=len(text):return None
    except ValueError:return None
    stack=[];quoted=False;escaped=False
    for char in text:
        if quoted:
            if escaped:escaped=False
            elif char=='\\':escaped=True
            elif char=='"':quoted=False
        elif char=='"':quoted=True
        elif char in '{[':stack.append('}' if char=='{' else ']')
        elif char in '}]':
            if not stack or stack.pop()!=char:return None
    if quoted or not stack or len(stack)>16:return None
    suffix=''.join(reversed(stack))
    try:return parse_model_object(text+suffix),suffix
    except ValueError:return None
