"""Local structured diagnostics. Never accept prompts, responses or raw exceptions."""
import contextvars
import json
import logging
import os
import sqlite3
import sys
import traceback
from contextlib import contextmanager
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path
from urllib.parse import urlparse

from .schemas import OutputValidationError


def error_details(exc):
    """Report exception location, chain and status without exception message/locals."""
    types, frames, seen = [], [], set()
    status = application_status = provider_status = None
    validation_error = None
    validation_errors = []
    parse_error = None
    dependency_details = {}
    while exc is not None and id(exc) not in seen and len(types) < 8:
        seen.add(id(exc))
        types.append(type(exc).__name__)
        if isinstance(exc, OutputValidationError) and validation_error is None:
            validation_error = exc.issue
        if getattr(exc,'errors',None):validation_errors=exc.errors
        if getattr(exc,'parse_error',None):parse_error=exc.parse_error
        from .dependencies import DependencyConflict
        if isinstance(exc, DependencyConflict) and not dependency_details:
            dependency_details = {key: getattr(exc, key) for key in (
                'dependency_changes', 'dependency_phase', 'dependency_task', 'dependency_kind') if hasattr(exc, key)}
        remote = getattr(exc, 'status_code', None)
        local = getattr(exc, 'status', None)
        if isinstance(remote, int):
            provider_status = remote
        if isinstance(local, int):
            application_status = local
        candidate = remote or local
        if isinstance(candidate, int):
            status = candidate
        for frame in traceback.extract_tb(exc.__traceback__)[-6:]:
            frames.append({'file': Path(frame.filename).name, 'line': frame.lineno, 'function': frame.name})
        exc = exc.__cause__ or exc.__context__
    result = {'error_types': types, 'http_status': status, 'application_status': application_status,
            'provider_http_status': provider_status,
            'http_status_source': 'provider' if provider_status is not None else 'application' if application_status is not None else None,
            'stack': frames[-12:]}
    if validation_error is not None:
        result['validation_error'] = validation_error
    if validation_errors:result['validation_errors']=validation_errors
    if parse_error:result['parse_error']=parse_error
    result.update(dependency_details)
    return result


def endpoint_origin(address):
    try:
        parsed = urlparse(address)
        host = parsed.hostname or ''
        if ':' in host:
            host = '[' + host + ']'
        return f'{parsed.scheme}://{host}' + (f':{parsed.port}' if parsed.port else '')
    except ValueError:
        return 'invalid-endpoint'


class Diagnostics:
    def __init__(self, store):
        self.store = store
        self.storage_degraded = False
        self.file_degraded = False
        self.context = contextvars.ContextVar('tcg_diagnostics', default={})
        self.path = store.directory / 'logs' / 'tcg.log'
        self.logger = logging.Logger(f'tcg.{id(self)}', level=getattr(logging, os.environ.get('TCG_LOG_LEVEL', 'INFO').upper(), logging.INFO))
        self.logger.propagate = False
        console = logging.StreamHandler(sys.stderr)
        console.setFormatter(logging.Formatter('%(message)s'))
        self.logger.addHandler(console)
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            handler = RotatingFileHandler(self.path, maxBytes=5 * 1024 * 1024, backupCount=3, encoding='utf-8')
            handler.setFormatter(logging.Formatter('%(message)s'))
            self.logger.addHandler(handler)
            try:
                os.chmod(self.path, 0o600)
            except OSError:
                pass
        except OSError as exc:
            self.file_degraded = True
            self.write_log('WARNING', json.dumps({'at': datetime.now(timezone.utc).isoformat(), 'level': 'WARNING', 'event': 'diagnostics.file_unavailable', **error_details(exc)}))
        try:
            with store.lock:
                store.db.executescript('''
                    CREATE TABLE IF NOT EXISTS diagnostics(id INTEGER PRIMARY KEY AUTOINCREMENT,run_id TEXT NOT NULL,payload TEXT NOT NULL);
                    CREATE INDEX IF NOT EXISTS diagnostics_run ON diagnostics(run_id,id);
                ''')
        except sqlite3.Error as exc:
            self.storage_failure(exc)

    @contextmanager
    def bind(self, **fields):
        token = self.context.set({**self.context.get(), **fields})
        try:
            yield
        finally:
            self.context.reset(token)

    def record(self, event, level='INFO', **fields):
        payload = {'at': datetime.now(timezone.utc).isoformat(), 'level': level, 'event': event, **self.context.get(), **fields}
        text = json.dumps(payload, ensure_ascii=False, separators=(',', ':'))
        self.write_log(level, text)
        # Diagnostics are best effort and must never block scheduling/cancellation.
        if payload.get('run_id') and level != 'DEBUG':
            try:
                self.store.append_event(payload['run_id'], 'progress', payload)
            except sqlite3.Error as exc:
                self.storage_failure(exc)
            try:
                with self.store.transaction():
                    self.store.db.execute('INSERT INTO diagnostics(run_id,payload) VALUES(?,?)', (payload['run_id'], text))
                    self.store.db.execute('DELETE FROM diagnostics WHERE run_id=? AND id NOT IN (SELECT id FROM diagnostics WHERE run_id=? ORDER BY id DESC LIMIT 500)', (payload['run_id'], payload['run_id']))
            except sqlite3.Error as exc:
                self.storage_failure(exc)
        return payload

    def write_log(self, level, text):
        try:
            self.logger.log(getattr(logging, level), text)
        except (OSError, ValueError, RuntimeError):
            # A broken output sink must not change the outcome of a business action.
            pass

    def storage_failure(self, exc):
        if not self.storage_degraded:
            self.write_log('WARNING', json.dumps({'at': datetime.now(timezone.utc).isoformat(), 'level': 'WARNING', 'event': 'diagnostics.storage_unavailable', **error_details(exc)}))
        self.storage_degraded = True

    def rows(self, run_id, limit=200):
        try:
            with self.store.lock:
                rows = self.store.db.execute('SELECT payload FROM diagnostics WHERE run_id=? ORDER BY id DESC LIMIT ?', (run_id, limit)).fetchall()
            return [json.loads(row[0]) for row in reversed(rows)]
        except sqlite3.Error as exc:
            self.storage_failure(exc)
            return []

    def latest(self, run_id):
        values = self.rows(run_id, 1)
        return values[0] if values else None

    def close(self):
        for handler in self.logger.handlers[:]:
            handler.close()
            self.logger.removeHandler(handler)
