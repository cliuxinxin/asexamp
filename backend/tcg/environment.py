"""Read local single-line .env assignments without executing or expanding them."""
import os
from pathlib import Path
import re
import shlex

from .schemas import DomainError

PROJECT_ROOT = Path(__file__).resolve().parents[2]
MODEL_ENV = {
    'TCG_MODEL_PROVIDER': 'provider', 'TCG_MODEL_BASE_URL': 'base_url',
    'TCG_MODEL_NAME': 'model', 'TCG_MODEL_TIMEOUT_SECONDS': 'timeout_seconds',
    'TCG_API_KEY': 'api_key',
    'TCG_MODEL_HEADERS_JSON': 'headers_json', 'TCG_MODEL_AUTH_MODE': 'auth_mode',
}


def model_environment(directory):
    """Use one explicit/data/project file, then allow process-level overrides."""
    explicit = os.environ.get('TCG_ENV_FILE')
    path = Path(explicit).expanduser().resolve() if explicit else next(
        (p for p in (directory / '.env', PROJECT_ROOT / '.env') if p.is_file()), None)
    values = {}
    if path is not None:
        try:
            lines = path.read_text(encoding='utf-8-sig').splitlines()
        except (OSError, UnicodeError):
            raise DomainError('无法读取指定的 .env 文件，请检查路径、权限和 UTF-8 编码') from None
        for index, line in enumerate(lines, 1):
            line = line.strip()
            if not line or line.startswith('#'):
                continue
            match = re.fullmatch(r'(?:export\s+)?([A-Za-z_][A-Za-z_0-9]*)\s*=\s*(.*)', line)
            if match is None:
                raise DomainError(f'.env 第 {index} 行格式错误，请使用 NAME=value')
            name, raw = match.groups()
            try:
                parts = shlex.split(raw, comments=True, posix=True)
                if len(parts) > 1:
                    raise ValueError()
            except ValueError:
                raise DomainError(f'.env 第 {index} 行格式错误；值含空格或 # 时请用引号，且不能跨行') from None
            if name in MODEL_ENV:
                values[MODEL_ENV[name]] = parts[0] if parts else ''
    process = {field: os.environ[name] for name, field in MODEL_ENV.items() if name in os.environ}
    return path, (values, process)
