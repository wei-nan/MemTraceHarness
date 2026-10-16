"""Operator-managed CLI identities and host-wide capacity. Contains no credentials."""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import time
import tomllib
from uuid import uuid4

PROVIDERS = ('claude', 'codex', 'antigravity', 'cursor')
ID = re.compile(r'^[a-z][a-z0-9_-]{0,63}$')


def provider_ref(provider: str, source: str | None = None) -> str:
    base, embedded = split_ref(provider)
    if source and (not isinstance(source, str) or not ID.fullmatch(source)):
        raise ValueError('invalid execution source id')
    return base + ('@' + (source or embedded) if source or embedded else '')


def split_ref(value: str) -> tuple[str, str | None]:
    if not isinstance(value, str):
        raise ValueError("provider/source must be text")
    parts = value.split('@')
    if len(parts) > 2 or parts[0] not in PROVIDERS or (len(parts) == 2 and not ID.fullmatch(parts[1])):
        raise ValueError('invalid provider/source reference')
    return parts[0], parts[1] if len(parts) == 2 else None


@dataclass(frozen=True)
class SourceBinding:
    path: Path
    source_id: str
    provider: str
    command: str
    quota_bucket: str
    revision: int
    allowed_file: Path | None = None
    authorized: bool = True


class SourceCommand(list):
    """argv with out-of-band local routing metadata, never included in the provider prompt."""
    def __init__(self, values, binding: SourceBinding):
        super().__init__(values)
        self.execution_source = binding

    def __add__(self, values):
        return SourceCommand(list(self) + list(values), self.execution_source)


class SourceUnavailable(ValueError):
    def __init__(self, message: str, category: str = 'source_unavailable'):
        super().__init__(message)
        self.category = category


def _alive(pid: int | None) -> bool:
    if not pid:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


class SourceStore:
    def __init__(self, path: Path):
        self.path = Path(path).expanduser().resolve()
        self.store_id = hashlib.sha256(str(self.path).encode()).hexdigest()[:16]

    @contextmanager
    def connection(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.path, timeout=15)
        conn.row_factory = sqlite3.Row
        try:
            conn.executescript('''
                CREATE TABLE IF NOT EXISTS sources (
                    id TEXT PRIMARY KEY, provider TEXT NOT NULL, label TEXT NOT NULL,
                    command TEXT NOT NULL, quota_bucket TEXT NOT NULL, max_concurrency INTEGER NOT NULL,
                    enabled INTEGER NOT NULL, revision INTEGER NOT NULL, updated_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS source_revisions (
                    sequence INTEGER PRIMARY KEY, source_id TEXT, revision INTEGER,
                    entry_json TEXT NOT NULL, changed_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS source_leases (
                    id TEXT PRIMARY KEY, source_id TEXT NOT NULL, quota_bucket TEXT NOT NULL,
                    owner_pid INTEGER NOT NULL, child_pid INTEGER, expires_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS source_cooldowns (
                    key TEXT PRIMARY KEY, category TEXT NOT NULL, until_at REAL NOT NULL
                );
            ''')
            with conn:
                yield conn
        finally:
            conn.close()

    def list(self) -> list[dict]:
        if not self.path.exists():
            return []
        with self.connection() as conn:
            rows = [dict(row) for row in conn.execute('SELECT * FROM sources ORDER BY label,id')]
            now = time.time()
            cooldowns = {row['key']: dict(row) for row in conn.execute('SELECT * FROM source_cooldowns WHERE until_at>?', (now,))}
            for row in rows:
                row['enabled'] = bool(row['enabled'])
                row['active'] = conn.execute('SELECT count(*) FROM source_leases WHERE quota_bucket=?', (row['quota_bucket'],)).fetchone()[0]
                row['cooldown'] = cooldowns.get('source:' + row['id']) or cooldowns.get('bucket:' + row['quota_bucket'])
            return rows

    def get(self, source_id: str) -> dict:
        entry = next((row for row in self.list() if row['id'] == source_id), None)
        if entry is None:
            raise SourceUnavailable('execution source is not registered')
        return entry

    def change(self, data: dict) -> dict:
        action = data.get('action')
        if action not in ('save', 'toggle', 'reset'):
            raise ValueError('unsupported execution source action')
        source_id = data.get('id')
        if not isinstance(source_id, str) or not ID.fullmatch(source_id):
            raise ValueError('source id must use lowercase letters, digits, - or _')
        with self.connection() as conn:
            conn.execute('BEGIN IMMEDIATE')
            row = conn.execute('SELECT * FROM sources WHERE id=?', (source_id,)).fetchone()
            if row and (type(data.get('revision')) is not int or data['revision'] != row['revision']):
                raise ValueError('execution source changed; refresh before saving')
            if not row and action != 'save':
                raise ValueError('execution source not found')
            if action == 'save':
                provider, label, command = data.get('provider'), data.get('label'), data.get('command')
                bucket, maximum = data.get('quota_bucket') or source_id, data.get('max_concurrency', 1)
                if provider not in PROVIDERS:
                    raise ValueError('unsupported provider')
                if row and provider != row['provider']:
                    raise ValueError('a source provider cannot change; create a new source id')
                if not isinstance(label, str) or not label.strip() or len(label) > 80:
                    raise ValueError('source label needs 1–80 characters')
                if not isinstance(command, str) or not command or len(command) > 1024 or any(ord(c) < 32 for c in command):
                    raise ValueError('command must be one executable path, without arguments or credentials')
                if Path(command).expanduser().is_absolute() and re.search(r'\s', command) and not Path(command).expanduser().is_file():
                    raise ValueError('command must be an executable path, without shell arguments')
                if not (Path(command).expanduser().is_absolute() or re.fullmatch(r'[A-Za-z0-9_.-]+', command)):
                    raise ValueError('use an absolute executable path or a binary name; shell aliases/arguments are unsupported')
                if not isinstance(bucket, str) or not ID.fullmatch(bucket):
                    raise ValueError('invalid quota bucket id')
                if type(maximum) is not int or not 1 <= maximum <= 32:
                    raise ValueError('max_concurrency must be 1–32')
                if not row and conn.execute('SELECT count(*) FROM sources').fetchone()[0] >= 64:
                    raise ValueError('at most 64 execution sources')
                revision = row['revision'] + 1 if row else 1
                conn.execute('INSERT OR REPLACE INTO sources VALUES (?,?,?,?,?,?,?,?,?)',
                    (source_id, provider, label.strip(), command, bucket, maximum, row['enabled'] if row else 1, revision, time.time()))
            elif action == 'toggle':
                if type(data.get('enabled')) is not bool:
                    raise ValueError('enabled must be a boolean')
                conn.execute('UPDATE sources SET enabled=?,revision=revision+1,updated_at=? WHERE id=?',
                             (int(data['enabled']), time.time(), source_id))
            else:
                conn.execute('DELETE FROM source_cooldowns WHERE key IN (?,?)', ('source:' + source_id, 'bucket:' + row['quota_bucket']))
                conn.execute('UPDATE sources SET revision=revision+1,updated_at=? WHERE id=?', (time.time(), source_id))
            entry = dict(conn.execute('SELECT * FROM sources WHERE id=?', (source_id,)).fetchone())
            conn.execute('INSERT INTO source_revisions (source_id,revision,entry_json,changed_at) VALUES (?,?,?,?)',
                         (source_id, entry['revision'], json.dumps(entry), time.time()))
        return self.get(source_id)

    def acquire(self, binding: SourceBinding, timeout_seconds: float) -> str:
        if not binding.authorized:
            raise SourceUnavailable("execution source is not allowed for this Harness")
        if binding.allowed_file is not None:
            try:
                with binding.allowed_file.open('rb') as handle:
                    allowed = tomllib.load(handle).get('execution_sources', {}).get('allowed', [])
            except (OSError, ValueError):
                raise SourceUnavailable('Harness execution-source permissions could not be read') from None
            if not isinstance(allowed, list) or any(not isinstance(item, str) for item in allowed) or binding.source_id not in allowed:
                raise SourceUnavailable('execution source is no longer allowed for this Harness')
        with self.connection() as conn:
            conn.execute('BEGIN IMMEDIATE')
            row = conn.execute('SELECT * FROM sources WHERE id=?', (binding.source_id,)).fetchone()
            if not row or not row['enabled']:
                raise SourceUnavailable('execution source is disabled or missing')
            if any(row[key] != getattr(binding, field) for key, field in
                   [('provider','provider'), ('command','command'), ('quota_bucket','quota_bucket')]):
                raise SourceUnavailable('execution source changed before invocation; rebuild the call')
            now = time.time()
            for lease in conn.execute('SELECT * FROM source_leases WHERE expires_at<?', (now,)).fetchall():
                # An orphaned CLI still using the account must not free its capacity prematurely.
                if not _alive(lease['child_pid']):
                    conn.execute('DELETE FROM source_leases WHERE id=?', (lease['id'],))
            cool = conn.execute('SELECT category FROM source_cooldowns WHERE key IN (?,?) AND until_at>?',
                               ('source:' + row['id'], 'bucket:' + row['quota_bucket'], now)).fetchone()
            if cool:
                raise SourceUnavailable('execution source is in cooldown', cool['category'])
            maximum = conn.execute('SELECT min(max_concurrency) FROM sources WHERE quota_bucket=? AND enabled=1', (row['quota_bucket'],)).fetchone()[0]
            active = conn.execute('SELECT count(*) FROM source_leases WHERE quota_bucket=?', (row['quota_bucket'],)).fetchone()[0]
            if active >= maximum:
                raise SourceUnavailable('execution source is busy', 'source_busy')
            lease_id = uuid4().hex
            conn.execute('INSERT INTO source_leases VALUES (?,?,?,?,NULL,?)',
                         (lease_id, row['id'], row['quota_bucket'], os.getpid(), now + max(timeout_seconds, 1) + 60))
            return lease_id

    def attach(self, lease: str, pid: int) -> None:
        with self.connection() as conn:
            conn.execute('UPDATE source_leases SET child_pid=? WHERE id=?', (pid, lease))

    def release(self, lease: str) -> None:
        with self.connection() as conn:
            conn.execute('DELETE FROM source_leases WHERE id=?', (lease,))

    def leased_commands(self) -> dict[int, str]:
        if not self.path.exists():
            return {}
        with self.connection() as conn:
            rows = conn.execute('SELECT l.child_pid,s.command FROM source_leases l JOIN sources s ON s.id=l.source_id WHERE l.child_pid IS NOT NULL').fetchall()
        return {row[0]: row[1] for row in rows}

    def record(self, binding: SourceBinding, category: str) -> None:
        if category not in ('quota_exhausted', 'rate_limit', 'provider_overloaded', 'authentication', 'cli_unavailable'):
            return
        shared = category in ('quota_exhausted', 'rate_limit', 'provider_overloaded')
        key = ('bucket:' + binding.quota_bucket) if shared else ('source:' + binding.source_id)
        seconds = 1800 if shared else 300
        with self.connection() as conn:
            conn.execute('INSERT INTO source_cooldowns VALUES (?,?,?) ON CONFLICT(key) DO UPDATE SET category=excluded.category,until_at=max(until_at,excluded.until_at)',
                         (key, category, time.time() + seconds))


def source_status(config) -> dict:
    path = config.execution_sources_path
    if path is None:
        return {'available': False, 'entries': []}
    store = SourceStore(path)
    try:
        return {'available': True, 'store_id': store.store_id, 'entries': store.list(), 'allowed': config.allowed_execution_sources()}
    except (OSError, sqlite3.Error):
        return {'available': False, 'entries': [], 'error': '執行來源暫時無法讀取'}
