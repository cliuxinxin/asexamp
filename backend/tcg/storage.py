"""SQLite repository. Every visible result commit rechecks run cancellation."""
import json
import os
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from .schemas import DEFAULT_PROFILE, DomainError, profile_config, validate_items


def now():
    return datetime.now(timezone.utc).isoformat()


def uid(prefix=''):
    return prefix + uuid.uuid4().hex


def dump(value):
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'))


def public(value):
    return {key: item for key, item in value.items() if not key.startswith('_')}


class DirectoryLock:
    """OS lock prevents duplicate local runners and is released on process death."""

    def __init__(self, directory):
        self.directory = Path(directory)
        self.handle = None

    def __enter__(self):
        self.directory.mkdir(parents=True, exist_ok=True)
        self.handle = (self.directory / '.server.lock').open('a+b')
        try:
            if os.name == 'nt':
                import msvcrt
                self.handle.seek(0, 2)
                if self.handle.tell() == 0:
                    self.handle.write(b'0')
                    self.handle.flush()
                self.handle.seek(0)
                msvcrt.locking(self.handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.handle.close()
            self.handle = None
            raise DomainError('此数据目录已有服务运行；请关闭已有进程，或选择另一个 TCG_DATA_DIR') from None
        return self

    def __exit__(self, *args):
        if self.handle:
            if os.name == 'nt':
                import msvcrt
                self.handle.seek(0)
                msvcrt.locking(self.handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
            self.handle.close()


class Store:
    def __init__(self, directory):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.directory / 'tcg.sqlite3', check_same_thread=False, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.lock = threading.RLock()
        self.db.executescript('''
            PRAGMA journal_mode=WAL;
            PRAGMA foreign_keys=ON;
            PRAGMA busy_timeout=5000;
            CREATE TABLE IF NOT EXISTS objects(id TEXT PRIMARY KEY,kind TEXT NOT NULL,project_id TEXT,chat_id TEXT,payload TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS object_scope ON objects(kind,project_id,chat_id);
            CREATE TABLE IF NOT EXISTS runs(id TEXT PRIMARY KEY,chat_id TEXT NOT NULL,project_id TEXT NOT NULL,status TEXT NOT NULL,payload TEXT NOT NULL);
            CREATE UNIQUE INDEX IF NOT EXISTS one_active_run ON runs(chat_id) WHERE status IN ('queued','running','waiting');
            CREATE TABLE IF NOT EXISTS revisions(artifact_id TEXT,revision INTEGER,payload TEXT NOT NULL,created_at TEXT NOT NULL,reason TEXT NOT NULL,diff TEXT NOT NULL,PRIMARY KEY(artifact_id,revision));
            CREATE TABLE IF NOT EXISTS model_outputs(run_id TEXT,call_id TEXT,content TEXT NOT NULL,PRIMARY KEY(run_id,call_id));
            CREATE TABLE IF NOT EXISTS cache(run_id TEXT,key TEXT,payload TEXT NOT NULL,PRIMARY KEY(run_id,key));
            CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY AUTOINCREMENT,run_id TEXT NOT NULL,payload TEXT NOT NULL,created_at TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS event_run ON events(run_id,id);
            CREATE TABLE IF NOT EXISTS model_requests(run_id TEXT NOT NULL,call_id TEXT NOT NULL,payload TEXT NOT NULL,PRIMARY KEY(run_id,call_id));
            CREATE TABLE IF NOT EXISTS operation_receipts(command_id TEXT PRIMARY KEY,artifact_id TEXT NOT NULL,run_id TEXT,payload TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS cancelled_commands(command_id TEXT PRIMARY KEY);
            CREATE TABLE IF NOT EXISTS artifact_dependencies(artifact_id TEXT,revision INTEGER,payload TEXT NOT NULL,PRIMARY KEY(artifact_id,revision));
            CREATE TABLE IF NOT EXISTS source_evidence_versions(source_id TEXT,version INTEGER,payload TEXT NOT NULL,PRIMARY KEY(source_id,version));
            CREATE TRIGGER IF NOT EXISTS immutable_source_evidence_update BEFORE UPDATE ON source_evidence_versions BEGIN SELECT RAISE(ABORT, 'Source evidence versions are immutable'); END;
            CREATE TRIGGER IF NOT EXISTS immutable_source_evidence_delete BEFORE DELETE ON source_evidence_versions BEGIN SELECT RAISE(ABORT, 'Source evidence versions are immutable'); END;
            CREATE TABLE IF NOT EXISTS input_versions(object_id TEXT,kind TEXT,version INTEGER,payload TEXT NOT NULL,PRIMARY KEY(object_id,kind,version));
            CREATE TRIGGER IF NOT EXISTS immutable_revision_update BEFORE UPDATE ON revisions BEGIN SELECT RAISE(ABORT, 'Artifact revisions are immutable'); END;
            CREATE TRIGGER IF NOT EXISTS immutable_revision_delete BEFORE DELETE ON revisions BEGIN SELECT RAISE(ABORT, 'Artifact revisions are immutable'); END;
            CREATE TABLE IF NOT EXISTS audit(id INTEGER PRIMARY KEY AUTOINCREMENT,object_id TEXT NOT NULL,action TEXT NOT NULL,payload TEXT NOT NULL,created_at TEXT NOT NULL);
        ''')
        if 'kind' not in {row['name'] for row in self.db.execute('PRAGMA table_info(events)')}:
            self.db.execute("ALTER TABLE events ADD COLUMN kind TEXT NOT NULL DEFAULT 'update'")
        if not self.list('project'):
            self.create_project('默认项目')

    @contextmanager
    def transaction(self):
        with self.lock:
            outer = not self.db.in_transaction
            if outer:
                self.db.execute('BEGIN IMMEDIATE')
            try:
                yield
                if outer:
                    self.db.execute('COMMIT')
            except BaseException:
                if outer:
                    self.db.execute('ROLLBACK')
                raise

    def close(self):
        with self.lock:
            self.db.close()

    def put(self, kind, value):
        with self.transaction():
            changed_chunk_source = None
            if kind == 'chunk':
                prior = self.db.execute('SELECT payload FROM objects WHERE id=? AND kind=?', (value['id'], kind)).fetchone()
                sid = value.get('source_id')
                recorded = self.db.execute('SELECT 1 FROM source_evidence_versions WHERE source_id=? LIMIT 1', (sid,)).fetchone()
                if recorded and (prior is None or json.loads(prior[0]) != value):
                    self.source_snapshot(sid)
                    changed_chunk_source = sid
            if kind == 'artifact':
                head = self.db.execute('SELECT MAX(revision) FROM revisions WHERE artifact_id=?', (value['id'],)).fetchone()[0]
                existing_head = self.db.execute('SELECT payload FROM objects WHERE id=? AND kind=?', (value['id'], 'artifact')).fetchone()
                if head is None and existing_head and value.get('revision') != json.loads(existing_head[0]).get('revision'):
                    raise DomainError('成果新版本必须通过统一提交创建', 409)
                if head is not None and value.get('revision', 0) > head:
                    raise DomainError('成果新版本必须通过统一提交创建', 409)
                if head is not None and value.get('revision', 0) < head:
                    raise DomainError('成果版本已更新，不可回退当前版本', 409)
                saved = self.db.execute('SELECT payload FROM revisions WHERE artifact_id=? AND revision=?', (value['id'], value.get('revision'))).fetchone()
                if saved:
                    baseline = json.loads(saved[0])
                    immutable_fields = ('items', 'report', '_source_ids', '_source_roles', '_profile', 'type', 'title', 'project_id', 'chat_id', '_dependencies', '_write_dependencies')
                    if any(value.get(key) != baseline.get(key) for key in immutable_fields):
                        raise DomainError('成果内容不可原地修改，请创建新版本', 409)
            if kind in ('source', 'profile'):
                from .dependencies import digest
                previous_row = self.db.execute('SELECT payload FROM objects WHERE id=? AND kind=?', (value['id'], kind)).fetchone()
                previous = json.loads(previous_row[0]) if previous_row else None
                value = dict(value)
                if kind == 'source':
                    content = {k: v for k, v in value.items() if k not in ('version', '_content_digest')}
                    changed = previous is None or content != {k: v for k, v in previous.items() if k not in ('version', '_content_digest')}
                    value['version'] = (previous.get('version', 1) + int(changed)) if previous else 1
                    value['_content_digest'] = digest(value.get('_text', ''))
                if previous:
                    self.db.execute('INSERT OR IGNORE INTO input_versions VALUES(?,?,?,?)', (previous['id'], kind, previous.get('version', 1), dump(previous)))
                self.db.execute('INSERT OR IGNORE INTO input_versions VALUES(?,?,?,?)', (value['id'], kind, value.get('version', 1), dump(value)))
            self.db.execute('INSERT INTO objects VALUES(?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET payload=excluded.payload', (value['id'], kind, value.get('project_id'), value.get('chat_id'), dump(value)))
            if changed_chunk_source:
                from .dependencies import digest
                source = self.get('source', changed_chunk_source)
                chunks = sorted((c for c in self.list('chunk', chat_id=source['chat_id']) if c['source_id'] == changed_chunk_source), key=lambda c: c['id'])
                self.put('source', {**source, '_evidence_digest': digest(chunks)})
                self.source_snapshot(changed_chunk_source)
        return value

    def get(self, kind, object_id):
        with self.lock:
            row = self.db.execute('SELECT payload FROM objects WHERE id=? AND kind=?', (object_id, kind)).fetchone()
        if row is None:
            raise DomainError('未找到请求的资源', 404)
        return json.loads(row[0])

    def list(self, kind, project_id=None, chat_id=None):
        sql, args = 'SELECT payload FROM objects WHERE kind=?', [kind]
        for key, value in (('project_id', project_id), ('chat_id', chat_id)):
            if value is not None:
                sql += f' AND {key}=?'
                args.append(value)
        with self.lock:
            return [json.loads(row[0]) for row in self.db.execute(sql, args).fetchall()]

    def audit(self, object_id, action, value):
        self.db.execute('INSERT INTO audit(object_id,action,payload,created_at) VALUES(?,?,?,?)', (object_id, action, dump(value), now()))

    def create_project(self, name):
        with self.transaction():
            project = self.put('project', {'id': uid('prj_'), 'name': name, 'created_at': now()})
            self.put('profile', {'id': uid('prof_'), 'project_id': project['id'], 'name': 'Default', 'version': 1, 'config': dict(DEFAULT_PROFILE)})
        return project

    def create_profile(self, project_id, name, config):
        self.get('project', project_id)
        config = profile_config(config)
        with self.transaction():
            return self.put('profile', {'id': uid('prof_'), 'project_id': project_id, 'name': name, 'version': 1, 'config': config})

    def update_profile(self, profile_id, name, config, version):
        config = profile_config(config)
        with self.transaction():
            previous = self.get('profile', profile_id)
            if previous['version'] != version:
                raise DomainError('Profile 已更新，请刷新后再保存', 409)
            result = {**previous, 'name': name, 'config': config, 'version': version + 1}
            self.audit(profile_id, 'profile_update', {'before': previous['config'], 'after': config})
            return self.put('profile', result)

    def create_chat(self, project_id, title):
        self.get('project', project_id)
        with self.transaction():
            return self.put('chat', {'id': uid('chat_'), 'project_id': project_id, 'title': title.strip() or '新对话', 'created_at': now(), 'updated_at': now()})

    def add_source(self, chat_id, name, role, text, chunks, file_path=None, source_id=None):
        chat = self.get('chat', chat_id)
        source_id = source_id or uid('src_')
        with self.transaction():
            source = {'id': source_id, 'project_id': chat['project_id'], 'chat_id': chat_id, 'name': name, 'role': role, 'characters': len(text), 'created_at': now(), '_text': text, '_active': True, '_file': file_path}
            source = self.put('source', source)
            for index, chunk in enumerate(chunks, 1):
                self.put('chunk', {'id': f'{source_id}#P{index}', 'project_id': chat['project_id'], 'chat_id': chat_id, 'source_id': source_id, 'role': role, **chunk})
            self.source_snapshot(source_id)
        return source

    def source_snapshot(self, source_id, version=None):
        """Capture current evidence, or read an exact immutable historical version."""
        with self.transaction():
            if version is not None:
                row = self.db.execute('SELECT payload FROM source_evidence_versions WHERE source_id=? AND version=?', (source_id, version)).fetchone()
                if row is None:
                    raise DomainError('历史来源证据版本不可用；不可用当前内容代替', 404)
                return json.loads(row[0])
            source = self.get('source', source_id)
            chunks = sorted((c for c in self.list('chunk', chat_id=source.get('chat_id')) if c['source_id'] == source_id), key=lambda c: c['id'])
            value = {'source': source, 'chunks': chunks}
            version = source.get('version', 1)
            row = self.db.execute('SELECT payload FROM source_evidence_versions WHERE source_id=? AND version=?', (source_id, version)).fetchone()
            if row and json.loads(row[0]) != value:
                raise DomainError('来源版本内容已改变，请创建新来源版本', 409)
            self.db.execute('INSERT OR IGNORE INTO source_evidence_versions VALUES(?,?,?)', (source_id, version, dump(value)))
            return value

    def evidence_version(self, source_id, version, role=None):
        snapshot = self.source_snapshot(source_id, version)
        return [{**chunk, 'role': role or snapshot['source'].get('role'), 'source_version': version}
                for chunk in snapshot['chunks']]

    def evidence(self, source_ids, role_snapshot=None):
        evidence = []
        by_chat = {}
        for source_id in source_ids:
            source = self.get('source', source_id)
            chat_id = source['chat_id']
            if chat_id not in by_chat:
                by_chat[chat_id] = self.list('chunk', chat_id=chat_id)
            role = (role_snapshot or {}).get(source_id, source['role'])
            evidence.extend({**c, 'role': role} for c in by_chat[chat_id] if c['source_id'] == source_id)
        return evidence

    def deactivate_source(self, source_id):
        with self.transaction():
            value = self.get('source', source_id)
            value['_active'] = False
            self.put('source', value)
            self.audit(source_id, 'source_deactivate', {})

    def create_run(self, chat_id, request):
        with self.transaction():
            chat = self.get('chat', chat_id)
            if getattr(self, '_workspace_action_tokens', {}).get(chat_id):
                raise DomainError('正在预览或应用项目修改，请稍候', 409)
            active = self.db.execute("SELECT id FROM runs WHERE chat_id=? AND status IN ('queued','running','waiting')", (chat_id,)).fetchone()
            if active:
                raise DomainError('此对话已有运行中的任务，请先继续、取消或等待完成', 409)
            profile = self.get('profile', request['profile_id']) if request.get('profile_id') else self.list('profile', project_id=chat['project_id'])[0]
            if profile['project_id'] != chat['project_id']:
                raise DomainError('Profile 不可跨项目使用')
            sources = request.get('source_ids')
            if sources is None:
                sources = [s['id'] for s in self.list('source', chat_id=chat_id) if s['_active']]
                from .project_context import shared_sources
                sources = list(dict.fromkeys(sources + [s['id'] for s in shared_sources(self, chat['project_id'])]))
            if not isinstance(sources, list) or len(set(sources)) != len(sources):
                raise DomainError('source_ids 必须为不重复的数组')
            for source_id in sources:
                source = self.get('source', source_id)
                if source['project_id'] != chat['project_id'] or not source['_active']:
                    raise DomainError('来源不属于当前项目或已停用')
            artifact = None
            if request.get('artifact_id'):
                artifact = self.get('artifact', request['artifact_id'])
                if artifact['project_id'] != chat['project_id'] or artifact['chat_id'] != chat_id:
                    raise DomainError('Artifact 不属于当前对话')
            elif not request.get('_fresh_after_supplement') and request['intent'] in ('auto', 'query', 'modify', 'review_case', 'learn_template'):
                visible = [a for a in self.list('artifact', chat_id=chat_id) if a.get('_visible')]
                if request['intent'] == 'review_case':
                    visible = [a for a in visible if a['type'] == 'cases']
                if visible:
                    artifact = sorted(visible, key=lambda a: a['created_at'])[-1]
                    request['artifact_id'] = artifact['id']
            if request.get('selected_ids') is not None:
                if not artifact or not set(request['selected_ids']).issubset({i['id'] for i in artifact['items']}):
                    raise DomainError('所选条目不属于当前 Artifact')
            run_id = uid('run_')
            turn_id = request.get('_conversation_turn_id')
            if turn_id:
                existing_message = self.get('message', 'input:' + turn_id)
                if existing_message['chat_id'] != chat_id:
                    raise DomainError('对话消息不属于当前任务')
                message = self.put('message', {**existing_message, 'metadata': {**existing_message.get('metadata', {}), 'run_id': run_id}})
            else:
                message = self.put('message', {'id': uid('msg_'), 'project_id': chat['project_id'], 'chat_id': chat_id, 'role': 'user', 'content': request['content'], 'created_at': now(), 'metadata': {'run_id': run_id}})
            history = sorted(self.list('message', chat_id=chat_id), key=lambda item: (item['created_at'], item['id']))
            run = {'id': run_id, 'chat_id': chat_id, 'project_id': chat['project_id'], 'status': 'queued', 'intent': request['intent'], 'mode': request['mode'], 'stage': 'queued', 'created_at': now(), 'updated_at': now(), 'artifact_ids': [], '_request': request, '_profile': profile['config'], '_profile_id': profile['id'], '_source_ids': sources, '_artifact_snapshot': artifact, '_conversation': [{'role': m['role'], 'content': m['content'], 'metadata': m['metadata']} for m in history[-12:]], '_resume': None}
            # Keep provenance separate until routing determines whether this is
            # an operation on a historical artifact or a fresh generation.
            run['_history_total'] = len(history)
            run['_artifact_source_ids'] = artifact.get('_source_ids', []) if artifact else []
            if request.get('experience') == 'reliable':
                depth = request.get('depth', 'auto')
                depth = depth if depth in ('quick', 'standard', 'deep') else 'standard'
                config = dict(profile['config'])
                if request.get('depth') not in (None, 'auto'):
                    config.update(case_level=depth, scenario_level=depth)
                if request.get('profile_override'):
                    config = profile_config({**config, **request['profile_override']})
                if request.get('case_types'):
                    config['case_types'] = list(dict.fromkeys(request['case_types']))
                run.update(experience='reliable', graph_version=7, pause_contract=2, _profile=config,
                           _memory=[m for m in self.list('memory', project_id=chat['project_id']) if m.get('active', True)],
                           _source_roles={sid: self.get('source', sid)['role'] for sid in sources},
                           progress={'phase': 'queued', 'completed': 0, 'total': 0, 'label': '准备任务'})
            if request.get('experience') == 'agent':
                run.update(experience='agent', graph_version=2, _instruction_version=0, _applied_instruction_version=0, _instructions=[],
                           agent={'depth': request.get('depth') if request.get('depth') in ('quick', 'standard', 'deep') else 'standard', 'rationale': '', 'plan': [], 'insights': [], 'pending_instructions': 0})
            self.db.execute('INSERT INTO runs VALUES(?,?,?,?,?)', (run_id, chat_id, chat['project_id'], 'queued', dump(run)))
            chat['updated_at'] = now()
            self.put('chat', chat)
            self.event(run)
        return message, run

    def run(self, run_id):
        with self.lock:
            row = self.db.execute('SELECT payload FROM runs WHERE id=?', (run_id,)).fetchone()
        if row is None:
            raise DomainError('未找到运行任务', 404)
        return json.loads(row[0])

    def runs(self, chat_id=None, statuses=None):
        sql, args = 'SELECT payload FROM runs WHERE 1=1', []
        if chat_id:
            sql += ' AND chat_id=?'
            args.append(chat_id)
        if statuses:
            sql += ' AND status IN (' + ','.join('?' for _ in statuses) + ')'
            args.extend(statuses)
        with self.lock:
            return [json.loads(row[0]) for row in self.db.execute(sql, args).fetchall()]

    def save_run(self, run):
        run['updated_at'] = now()
        try:
            self.db.execute('UPDATE runs SET status=?,payload=? WHERE id=?', (run['status'], dump(run), run['id']))
        except sqlite3.IntegrityError:
            raise DomainError('此对话已有运行中的任务', 409) from None
        self.event(run)
        return run

    def update_run(self, run_id, **changes):
        with self.transaction():
            run = self.run(run_id)
            if run['status'] == 'cancelled' and changes.get('status') != 'cancelled':
                raise DomainError('运行任务已取消', 409)
            return self.save_run({**run, **changes})

    def event(self, run):
        self.db.execute('INSERT INTO events(run_id,payload,created_at) VALUES(?,?,?)', (run['id'], dump(public(run)), now()))

    def append_model_output(self, run_id, call_id, text):
        with self.transaction():
            self.db.execute('INSERT INTO model_outputs VALUES(?,?,?) ON CONFLICT(run_id,call_id) DO UPDATE SET content=model_outputs.content||excluded.content',(run_id,call_id,text))

    def save_model_request(self, run_id, call_id, request):
        with self.transaction():
            self.run(run_id)
            self.db.execute('INSERT INTO model_requests(run_id,call_id,payload) VALUES(?,?,?)', (run_id, call_id, dump(request)))

    def model_request(self, run_id, call_id):
        with self.lock:
            self.run(run_id)
            row = self.db.execute('SELECT payload FROM model_requests WHERE run_id=? AND call_id=?', (run_id, call_id)).fetchone()
        if row is None:
            raise DomainError('本次调用没有发送内容记录；旧版本记录无法补回', 404)
        return json.loads(row['payload'])

    def append_event(self, run_id, kind, data):
        with self.transaction():
            self.run(run_id)
            row = self.db.execute('INSERT INTO events(run_id,payload,created_at,kind) VALUES(?,?,?,?)', (run_id, dump(data), now(), kind))
            return row.lastrowid

    def events(self, run_id, after=0):
        with self.lock:
            return [{'id': row['id'], 'kind': row['kind'], 'data': json.loads(row['payload'])} for row in self.db.execute('SELECT id,kind,payload FROM events WHERE run_id=? AND id>? ORDER BY id LIMIT 200', (run_id, after)).fetchall()]

    def assert_running(self, run_id):
        run = self.run(run_id)
        # A paused HITP artifact edit intentionally calls the model while the
        # workflow remains waiting.  Its private token is the lease that keeps
        # cache writes valid until the edit is saved or cancelled.
        active_waiting_edit = run['status'] == 'waiting' and bool(run.get('_edit_token'))
        if run['status'] not in ('queued', 'running') and not active_waiting_edit:
            raise DomainError('任务状态已改变，拒绝过期结果', 409)

    def cache_get(self, run_id, key):
        with self.lock:
            row = self.db.execute('SELECT payload FROM cache WHERE run_id=? AND key=?', (run_id, key)).fetchone()
        return json.loads(row[0]) if row else None

    def cache_delete(self, run_id, key):
        with self.transaction():
            self.db.execute('DELETE FROM cache WHERE run_id=? AND key=?', (run_id, key))

    def cache_set(self, run_id, key, value):
        with self.transaction():
            self.assert_running(run_id)
            self.db.execute('INSERT INTO cache VALUES(?,?,?) ON CONFLICT(run_id,key) DO UPDATE SET payload=excluded.payload', (run_id, key, dump(value)))
        return value

    def artifact(self, run_id, key, kind, title, items, report=None, *, command_id=None, dependencies=None, provenance=None):
        from .operations import create_artifact
        return create_artifact(self, run_id, key, kind, title, items, report,
                               command_id=command_id, dependencies=dependencies, provenance=provenance)

    def revise_artifact(self, artifact_id, expected_revision, items, reason='manual_edit', run_id=None,
                        cache_key=None, report=None, *, source_ids=None, source_roles=None,
                        command_id=None, dependencies=None, provenance=None):
        from .operations import revise_artifact
        return revise_artifact(self, artifact_id, expected_revision, items, reason, run_id, cache_key, report,
                               source_ids=source_ids, source_roles=source_roles,
                               command_id=command_id, dependencies=dependencies, provenance=provenance)

    def annotate_artifact(self, artifact_id, expected_revision, fields, reason='report_update', run_id=None):
        from .operations import revise_artifact
        with self.transaction():
            previous = self.get('artifact', artifact_id)
            return revise_artifact(self, artifact_id, expected_revision, previous['items'], reason, run_id,
                                   fields=fields)

    def revisions(self, artifact_id):
        self.get('artifact', artifact_id)
        with self.lock:
            return [{'revision': row['revision'], 'created_at': row['created_at'], 'reason': row['reason'], 'diff': json.loads(row['diff'])} for row in self.db.execute('SELECT * FROM revisions WHERE artifact_id=? ORDER BY revision DESC', (artifact_id,)).fetchall()]

    def revision(self, artifact_id, revision):
        with self.lock:
            row = self.db.execute('SELECT payload FROM revisions WHERE artifact_id=? AND revision=?', (artifact_id, revision)).fetchone()
        if row is None:
            raise DomainError('版本不存在', 404)
        return json.loads(row[0])

    def publish(self, run_id, artifact_ids, content='已完成。请查看下方结果。', waiting=False, proposal=None):
        with self.transaction():
            self.assert_running(run_id)
            run = self.run(run_id)
            key = 'published:' + ':'.join(artifact_ids) + (':waiting' if waiting else ':final')
            if not self.cache_get(run_id, key):
                for artifact_id in artifact_ids:
                    value = self.get('artifact', artifact_id)
                    value['_visible'] = True
                    self.put('artifact', value)
                metadata = {'run_id': run_id, 'artifact_ids': artifact_ids}
                if proposal is not None:
                    metadata['proposal'] = proposal
                self.put('message', {'id': uid('msg_'), 'project_id': run['project_id'], 'chat_id': run['chat_id'], 'role': 'assistant', 'content': content, 'created_at': now(), 'metadata': metadata})
                self.cache_set(run_id, key, {'done': True})
            run['artifact_ids'] = list(dict.fromkeys(run['artifact_ids'] + artifact_ids))
            if not waiting:
                run.update(status='completed', stage='completed')
                run.pop('interrupt', None)
                run.pop('error', None)
            self.save_run(run)
            return run
