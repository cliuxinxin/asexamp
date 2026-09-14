"""Load editable instructions as inert text; Python schemas still enforce contracts."""
from __future__ import annotations

import hashlib
from pathlib import Path
import re

import yaml

from .schemas import DomainError


PROMPT_DIRECTORY = Path(__file__).resolve().parent / 'prompts'
MAX_CATALOG_BYTES = 128 * 1024
MAX_PROMPT_CHARACTERS = 64 * 1024
_IDENTIFIER = re.compile(r'^[a-z][a-z0-9_]*$')


class PromptText(str):
    """Retain provenance across ordinary concatenation, without interpolating text."""

    def __new__(cls, text, templates=()):
        value = super().__new__(cls, text)
        value.templates = tuple(dict(entry) for entry in templates)
        return value

    def __add__(self, other):
        if not isinstance(other, str):
            return NotImplemented
        return PromptText(str(self) + str(other), _unique(self.templates + getattr(other, 'templates', ())))

    def __radd__(self, other):
        if not isinstance(other, str):
            return NotImplemented
        return PromptText(str(other) + str(self), _unique(getattr(other, 'templates', ()) + self.templates))


def _unique(entries):
    result, seen = [], set()
    for entry in entries:
        key = (entry['file'], entry['key'], entry['sha256'])
        if key not in seen:
            seen.add(key)
            result.append(entry)
    return result


def prompt_templates(text):
    return [dict(entry) for entry in _unique(getattr(text, 'templates', ()))]


def _error(filename, detail):
    error = DomainError(f'提示词配置错误：{filename}：{detail}。请修正 YAML 后重试。')
    error.category, error.retryable = 'prompt_configuration', False
    return error


class _UniqueSafeLoader(yaml.SafeLoader):
    """Reject ambiguous duplicate mapping keys instead of silently choosing one."""

    def construct_mapping(self, node, deep=False):
        if not isinstance(node, yaml.MappingNode):
            return super().construct_mapping(node, deep)
        mapping = {}
        for key_node, value_node in node.value:
            key = self.construct_object(key_node, deep=deep)
            if not isinstance(key, (str, int, float, bool, type(None))):
                raise yaml.YAMLError('mapping key must be scalar')
            if key in mapping:
                raise yaml.YAMLError('duplicate mapping key')
            mapping[key] = self.construct_object(value_node, deep=deep)
        return mapping


def load_prompt(identifier, *, directory=None):
    """Read each requested instruction afresh; edits affect the next model request."""
    parts = identifier.split('.') if isinstance(identifier, str) else []
    if len(parts) != 2 or not all(_IDENTIFIER.fullmatch(part) for part in parts):
        raise _error('prompts', '提示词名称必须是 file.key，且只能使用小写字母、数字和下划线')
    filename, key = parts[0] + '.yaml', parts[1]
    path = Path(directory) / filename if directory is not None else PROMPT_DIRECTORY / filename
    try:
        # Bound the actual read, including files changed between stat and read.
        with path.open('rb') as stream:
            raw = stream.read(MAX_CATALOG_BYTES + 1)
        if len(raw) > MAX_CATALOG_BYTES:
            raise _error(filename, '文件大小超过 128 KiB')
        catalog = yaml.load(raw.decode('utf-8'), Loader=_UniqueSafeLoader)
    except (OSError, UnicodeError) as exc:
        raise _error(filename, '文件不存在、不可读取或不是 UTF-8 文本') from exc
    except yaml.YAMLError as exc:
        detail = 'YAML 中有重复的字段' if 'duplicate mapping key' in str(exc) else 'YAML 语法或类型标签无效'
        raise _error(filename, detail) from exc
    if not isinstance(catalog, dict) or set(catalog) != {'version', 'prompts'}:
        raise _error(filename, '顶层字段必须且只能包含 version 和 prompts')
    if type(catalog['version']) is not int or catalog['version'] != 1:
        raise _error(filename, 'version 必须为整数 1')
    entries = catalog['prompts']
    if not isinstance(entries, dict) or not entries:
        raise _error(filename, 'prompts 必须为非空的文本映射')
    for entry_key, entry_text in entries.items():
        if not isinstance(entry_key, str) or not _IDENTIFIER.fullmatch(entry_key):
            raise _error(filename, 'prompts 中包含无效的字段名')
        if not isinstance(entry_text, str) or not entry_text.strip() or len(entry_text) > MAX_PROMPT_CHARACTERS:
            raise _error(filename, f'字段 {entry_key} 必须为非空文本，且不超过 65536 字符')
    if key not in entries:
        raise _error(filename, f'缺少字段 {key}')
    text = entries[key]
    return PromptText(text, ({'file': filename, 'key': key, 'version': catalog['version'],
                             'sha256': hashlib.sha256(text.encode('utf-8')).hexdigest()},))
