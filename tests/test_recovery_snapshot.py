"""Standalone recovery CLI tests: only synthetic, isolated fixture data."""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / 'scripts' / 'recovery_snapshot.py'


def run(*args):
    return subprocess.run([sys.executable, str(SCRIPT), *map(str, args)], text=True, capture_output=True)


def fixture(tmp_path):
    root = tmp_path / 'inputs'
    root.mkdir(mode=0o700)
    for name in ('builtins', 'vault', 'plugin', 'wiki', 'skills'):
        (root / name).mkdir()
    (root / 'builtins' / 'MEMORY.md').write_text('synthetic memory')
    (root / 'builtins' / 'USER.md').write_text('synthetic user')
    (root / 'vault' / 'facts').mkdir()
    (root / 'vault' / 'facts' / 'a.md').write_text('fact')
    (root / 'vault' / '.mirror-map.json').write_text('{"synthetic":true}')
    (root / 'vault' / '.reindex-request.json').write_text('{}')
    (root / 'vault' / '.zvec-grep').mkdir()
    (root / 'vault' / '.zvec-grep' / 'index').write_text('rebuildable')
    (root / 'plugin' / '__init__.py').write_text('# synthetic plugin')
    (root / 'plugin' / 'plugin.yaml').write_text('name: zvec-memory')
    (root / 'wiki' / 'note.md').write_text('wiki')
    (root / 'skills' / 'SKILL.md').write_text('skill')
    (root / 'config.yaml').write_text('synthetic config')
    (root / 'auth.json').write_text('synthetic-unselected-secret')
    (root / 'vault' / 'config.json').write_text('{"synthetic":true}')
    for name in ('state.db', 'vault/.mirror-inbox.sqlite3'):
        db = sqlite3.connect(root / name)
        db.execute('pragma journal_mode=wal')
        db.execute('create table messages(value text)')
        db.commit()
        db.execute("insert into messages values ('committed WAL row')")
        db.commit()
        # Keep connections open: committed row exists only in live WAL.
        if name == 'state.db': state = db
        else: inbox = db
    return root, state, inbox


def create_args(root, dst, optional=True):
    args = ['create', '--builtins', root / 'builtins', '--vault', root / 'vault',
            '--plugin', root / 'plugin', '--hermes-config', root / 'config.yaml',
            '--provider-config', root / 'vault' / 'config.json', '--state-db', root / 'state.db',
            '--destination', dst, '--writers-quiesced', '--include-sensitive-data']
    if optional: args += ['--wiki', root / 'wiki', '--skills', root / 'skills']
    return args


def source_bytes(root):
    return {p.relative_to(root).as_posix(): p.read_bytes()
            for p in root.rglob('*') if p.is_file()}


def test_sidecar_named_user_data_and_index_identity_survive_restore(tmp_path):
    root, state, inbox = fixture(tmp_path)
    try:
        # Unpaired sidecar-like names are ordinary user data.
        payloads = {
            'vault/unpaired.db-wal': b'user WAL-named data\x00',
            'vault/unpaired.db-shm': b'user SHM-named data\xff',
            'vault/.zvec-grep/.zvec-memory-state.json': b'{"identity":"do-not-rebuild"}',
            'vault/.zvec-grep/unknown/nested.bin': b'unknown index bytes\x00',
            'vault/.zvec-grep/LOCK': b'lock-file-bytes',
        }

        index_db = root / 'vault/.zvec-grep/engine.sqlite3'
        db = sqlite3.connect(index_db)
        db.execute('create table idx(v)')
        db.close()
        for name, content in payloads.items():
            path = root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
        expected_wal = (root / 'state.db-wal').read_bytes()
        raw_state = (root / 'state.db').read_bytes()
        raw_index = index_db.read_bytes()
        snap, restored = tmp_path / 'snapshot', tmp_path / 'restored'
        before = source_bytes(root)
        created = run(*create_args(root, snap))
        assert created.returncode == 0, created.stderr
        assert source_bytes(root) == before
        for name in ('state.db', 'vault/.mirror-inbox.sqlite3', 'vault/.zvec-grep/engine.sqlite3'):
            assert (snap / 'raw-database-evidence' / name).read_bytes() == before[name]
        for name, content in before.items():
            if name.endswith(('-wal', '-shm')) and 'unpaired' not in name:
                assert (snap / 'sidecar-evidence' / name).read_bytes() == content
        sha = created.stdout.strip().split('=', 1)[1]
        result = run('restore', '--snapshot', snap, '--destination', restored, '--manifest-sha256', sha)
        assert result.returncode == 0, result.stderr
        for name, content in payloads.items():
            assert (restored / name).read_bytes() == content
        assert (restored / 'sidecar-evidence/state.db-wal').read_bytes() == expected_wal
        assert (restored / 'raw-database-evidence/state.db').read_bytes() == raw_state
        assert (restored / 'raw-database-evidence/vault/.zvec-grep/engine.sqlite3').read_bytes() == raw_index

        assert not (restored / 'state.db-wal').exists()
        with sqlite3.connect(restored / 'state.db') as database:
            assert database.execute('select value from messages').fetchall() == [('committed WAL row',)]
    finally:
        state.close()
        inbox.close()


def test_round_trip_wal_hidden_metadata_exclusions_and_fresh_restore(tmp_path):
    root, state, inbox = fixture(tmp_path)
    try:
        assert (root / 'state.db-wal').stat().st_size > 0
        # A main-file-only immutable read cannot see this committed WAL schema.
        with sqlite3.connect(f"file:{root / 'state.db'}?immutable=1", uri=True) as main_only:
            with pytest.raises(sqlite3.OperationalError, match='no such table'):
                main_only.execute('select * from messages').fetchall()
        snap, restored = tmp_path / 'snapshot', tmp_path / 'restored'
        result = run(*create_args(root, snap))
        assert result.returncode == 0, result.stderr
        sha = result.stdout.strip().split('=', 1)[1]
        assert hashlib.sha256((snap / 'manifest.json').read_bytes()).hexdigest() == sha
        manifest = json.loads((snap / 'manifest.json').read_text())
        paths = {entry['path'] for entry in manifest['entries']}
        assert {'vault/.mirror-map.json', 'vault/.reindex-request.json', 'wiki/note.md', 'skills/SKILL.md', 'hermes-config', 'provider-config', 'state.db'} <= paths
        assert (snap / 'vault' / '.zvec-grep' / 'index').read_text() == 'rebuildable'
        assert not any(p.is_file() and b'synthetic-unselected-secret' in p.read_bytes()
                       for p in snap.rglob('*'))
        assert not manifest['exclusions']
        evidence = {e['source_path']: e['path'] for e in manifest['entries'] if 'source_path' in e}
        assert evidence['vault/.mirror-inbox.sqlite3-wal'] == 'sidecar-evidence/vault/.mirror-inbox.sqlite3-wal'
        assert evidence['state.db-wal'] == 'sidecar-evidence/state.db-wal'
        assert (snap / 'provider-config').read_bytes() == (snap / 'vault' / 'config.json').read_bytes()
        assert not (snap / 'vault' / '.mirror-inbox.sqlite3-wal').exists()
        result = run('restore', '--snapshot', snap, '--destination', restored, '--manifest-sha256', sha)
        assert result.returncode == 0, result.stderr
        for path in ('state.db', 'vault/.mirror-inbox.sqlite3'):
            with sqlite3.connect(restored / path) as db:
                assert db.execute('pragma integrity_check').fetchone() == ('ok',)
                assert db.execute('select value from messages').fetchall() == [('committed WAL row',)]
        for entry in manifest['entries']:
            target = restored / entry['path']
            if entry['type'] != 'dir':
                assert hashlib.sha256(target.read_bytes()).hexdigest() == entry['sha256']
        assert (restored.stat().st_mode & 0o777) == 0o700
        assert (restored / 'hermes-config').stat().st_mode & 0o777 == 0o600
        assert (restored / 'vault' / '.zvec-grep' / 'index').read_text() == 'rebuildable'
        result = run('restore', '--snapshot', snap, '--destination', restored, '--manifest-sha256', sha)
        assert result.returncode != 0
        assert (restored / 'builtins' / 'MEMORY.md').read_text() == 'synthetic memory'
    finally:
        state.close()
        inbox.close()


@pytest.mark.parametrize('adjacent_to', ['state.db', 'raw-database-evidence/state.db'])
def test_refuses_adjacent_manifested_sidecar_before_restore(tmp_path, adjacent_to):
    root, state, inbox = fixture(tmp_path)
    try:
        snap = tmp_path / 'snapshot'
        result = run(*create_args(root, snap))
        assert result.returncode == 0, result.stderr
        manifest_path = snap / 'manifest.json'
        manifest = json.loads(manifest_path.read_text())
        raw = (snap / 'sidecar-evidence/state.db-wal').read_bytes()
        (snap / (adjacent_to + '-wal')).write_bytes(raw)
        manifest['entries'].append({'path': adjacent_to + '-wal', 'type': 'file',
                                    'size': len(raw), 'sha256': hashlib.sha256(raw).hexdigest()})
        manifest_path.write_text(json.dumps(manifest))
        sha = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
        out = tmp_path / 'restored'
        refused = run('restore', '--snapshot', snap, '--destination', out, '--manifest-sha256', sha)
        assert refused.returncode != 0 and 'adjacent sidecar' in refused.stderr
        assert not out.exists()
    finally:
        state.close()
        inbox.close()


def test_refuses_special_lock_file_without_losing_it(tmp_path):
    root, state, inbox = fixture(tmp_path)
    try:
        lock = root / 'vault/.zvec-grep/LOCK'
        os.mkfifo(lock)
        result = run(*create_args(root, tmp_path / 'snapshot'))
        assert result.returncode != 0 and 'unsupported filesystem entry' in result.stderr
        assert lock.exists()
        assert not (tmp_path / 'snapshot').exists()
    finally:
        state.close()
        inbox.close()


def test_refuses_real_exclusive_sqlite_lock(tmp_path):
    root, state, inbox = fixture(tmp_path)
    locked_path = root / 'vault' / 'locked.db'
    locked = sqlite3.connect(locked_path)
    try:
        locked.execute('create table data(v)')
        locked.commit()
        locked.execute('begin exclusive')
        locked.execute('insert into data values (42)')
        before = source_bytes(root)
        result = run(*create_args(root, tmp_path / 'snapshot'))
        assert result.returncode != 0 and 'journal' in result.stderr.lower()
        assert source_bytes(root) == before
        assert locked.execute('select * from data').fetchall() == [(42,)]
        assert not (tmp_path / 'snapshot/manifest.json').exists()
    finally:
        locked.rollback()
        locked.close()
        state.close()
        inbox.close()


@pytest.mark.parametrize('kind', ['shm', 'wal', 'journal', 'corrupt-wal', 'corrupt-shm',
                                  'corrupt-checkpoint', 'wrong-db-mode'])
def test_refuses_ambiguous_sidecars_without_mutating_sources(tmp_path, kind):
    root, state, inbox = fixture(tmp_path)
    try:
        dbpath = root / 'vault' / 'collision.db'
        with sqlite3.connect(dbpath) as db:
            assert db.execute('pragma journal_mode=wal').fetchone() == ('wal',)
            db.execute('create table data(v)')
        db.close()
        if kind in ('shm', 'wal', 'journal'):
            Path(str(dbpath) + '-' + kind).write_bytes(b'unrelated user data' * 4000)
        elif kind == 'wrong-db-mode':
            main = root / 'state.db'
            data = bytearray(main.read_bytes())
            data[18:20] = b'\x01\x01'
            main.write_bytes(data)
        else:
            sidecar = root / ('state.db-wal' if kind == 'corrupt-wal' else 'state.db-shm')
            data = bytearray(sidecar.read_bytes())
            offset = -1 if kind == 'corrupt-wal' else 96 if kind == 'corrupt-checkpoint' else 140
            data[offset] ^= 0x80
            sidecar.write_bytes(data)
        before = source_bytes(root)
        snap = tmp_path / 'snapshot'
        result = run(*create_args(root, snap))
        assert result.returncode != 0, result.stdout
        assert source_bytes(root) == before
        assert not (snap / 'manifest.json').exists()
        for path in ('state.db', 'vault/.mirror-inbox.sqlite3', 'vault/collision.db'):
            assert (snap / 'raw-database-evidence' / path).read_bytes() == before[path]
        for path, content in before.items():
            if path.endswith(('-wal', '-shm', '-journal')):
                assert (snap / 'sidecar-evidence' / path).read_bytes() == content
    finally:
        state.close()
        inbox.close()


@pytest.mark.parametrize('page_size,blob_size', [(65536, 90000), (4096, 17000000)])
def test_wal_page_sizes_and_multiple_index_blocks(tmp_path, page_size, blob_size):
    root, state, inbox = fixture(tmp_path)
    path = root / 'vault' / 'large.db'
    db = sqlite3.connect(path)
    try:
        db.execute(f'pragma page_size={page_size}')
        assert db.execute('pragma journal_mode=wal').fetchone() == ('wal',)
        db.execute('pragma wal_autocheckpoint=0')
        db.execute('create table content(data blob)')
        db.execute('insert into content values (zeroblob(?))', (blob_size,))
        db.commit()
        before = source_bytes(root)
        result = run(*create_args(root, tmp_path / 'snapshot'))
        assert result.returncode == 0, result.stderr
        assert source_bytes(root) == before
        with sqlite3.connect(tmp_path / 'snapshot/vault/large.db') as restored:
            assert restored.execute('select length(data) from content').fetchone() == (blob_size,)
        if blob_size > 1000000:
            assert len(before['vault/large.db-shm']) > 32768
    finally:
        db.close()
        state.close()
        inbox.close()


def test_busy_backup_refuses_immediately_without_polling(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location('snapshot_test', SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    raw = tmp_path / 'raw'
    raw.write_bytes(b'fixture')
    calls = []

    class Connection:
        def close(self): pass
        def backup(self, dest, **kwargs):
            calls.append(kwargs)
            kwargs['progress'](sqlite3.SQLITE_BUSY, 1, 1)

    monkeypatch.setattr(module.sqlite3, 'connect', lambda *a, **kw: Connection())
    with pytest.raises(module.Refusal, match='locked'):
        module.sqlite_copy(raw, tmp_path / 'no-sidecar', tmp_path / 'output', tmp_path)
    assert len(calls) == 1 and calls[0]['sleep'] == 0 and calls[0]['pages'] == -1
    assert raw.read_bytes() == b'fixture'
    assert not list(tmp_path.glob('.sqlite-work-*'))


def test_corrupt_main_sqlite_failure_preserves_source_and_evidence(tmp_path):
    root, state, inbox = fixture(tmp_path)
    try:
        (root / 'vault' / 'invalid.db').write_bytes(b'not a database')
        before = source_bytes(root)
        snap = tmp_path / 'snapshot'
        result = run(*create_args(root, snap))
        assert result.returncode != 0 and 'database' in result.stderr.lower()
        assert source_bytes(root) == before
        assert (snap / 'raw-database-evidence/vault/invalid.db').read_bytes() == before['vault/invalid.db']
        assert not (snap / 'manifest.json').exists()
        assert not list(snap.glob('.sqlite-work-*'))
    finally:
        state.close()
        inbox.close()


def test_refusals_preserve_existing_and_detect_tampering(tmp_path):
    root, state, inbox = fixture(tmp_path)
    try:
        snap = tmp_path / 'snapshot'
        created = run(*create_args(root, snap, optional=False))
        assert created.returncode == 0, created.stderr
        sha = created.stdout.strip().split('=')[1]
        assert not (snap / 'wiki').exists() and not (snap / 'skills').exists()
        assert run(*create_args(root, snap)).returncode != 0
        assert run('restore', '--snapshot', snap, '--destination', tmp_path / 'badsha', '--manifest-sha256', '0' * 64).returncode != 0
        (snap / 'builtins' / 'MEMORY.md').write_text('tamper')
        bad = run('restore', '--snapshot', snap, '--destination', tmp_path / 'bad', '--manifest-sha256', sha)
        assert bad.returncode != 0 and 'checksum mismatch' in bad.stderr
        assert not (tmp_path / 'bad').exists()
    finally:
        state.close()
        inbox.close()


def test_unmanifested_symlink_and_unsafe_destinations_refused(tmp_path):
    root, state, inbox = fixture(tmp_path)
    try:
        (root / 'vault' / 'link').symlink_to(root / 'config.yaml')
        bad = run(*create_args(root, tmp_path / 'bad'))
        assert bad.returncode != 0 and 'symlink' in bad.stderr
        assert not (tmp_path / 'bad').exists()
        (root / 'vault' / 'link').unlink()
        required = root / 'builtins' / 'USER.md'
        required.unlink()
        assert run(*create_args(root, tmp_path / 'missing-required')).returncode != 0
        assert not (tmp_path / 'missing-required').exists()
        required.write_text('synthetic user')
        insecure = tmp_path / 'insecure'
        insecure.mkdir(mode=0o755)
        assert run(*create_args(root, insecure / 'snap')).returncode != 0
        assert run(*create_args(root, root / 'vault' / 'nested')).returncode != 0
        no_ack = create_args(root, tmp_path / 'noack')
        no_ack.remove('--include-sensitive-data')
        assert run(*no_ack).returncode != 0
        no_quiescence = create_args(root, tmp_path / 'noquiescence')
        no_quiescence.remove('--writers-quiesced')
        assert run(*no_quiescence).returncode != 0
        snap = tmp_path / 'snapshot'
        created = run(*create_args(root, snap))
        assert created.returncode == 0, created.stderr
        sha = created.stdout.strip().split('=')[1]
        (snap / 'extra').write_text('unexpected')
        rejected = run('restore', '--snapshot', snap, '--destination', tmp_path / 'out', '--manifest-sha256', sha)
        assert rejected.returncode != 0 and 'unmanifested' in rejected.stderr
        assert not (tmp_path / 'out').exists()
    finally:
        state.close()
        inbox.close()


@pytest.mark.parametrize('mutation', ['path-traversal', 'sqlite-corruption', 'missing'])
def test_refuses_invalid_inventory_and_corruption(tmp_path, mutation):
    root, state, inbox = fixture(tmp_path)
    try:
        snap = tmp_path / 'snapshot'
        result = run(*create_args(root, snap))
        assert result.returncode == 0, result.stderr
        manifest_path = snap / 'manifest.json'
        manifest = json.loads(manifest_path.read_text())
        if mutation == 'path-traversal':
            manifest['entries'][0]['path'] = '../outside'
        elif mutation == 'sqlite-corruption':
            database = snap / 'state.db'
            database.write_bytes(b'not a sqlite database')
            entry = next(e for e in manifest['entries'] if e['path'] == 'state.db')
            entry['size'] = database.stat().st_size
            entry['sha256'] = hashlib.sha256(database.read_bytes()).hexdigest()
        else:
            (snap / 'builtins' / 'USER.md').unlink()
        # Rebinding the hash tests structure/SQLite, not just the manifest hash.
        manifest_path.write_text(json.dumps(manifest))
        sha = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
        out = tmp_path / 'restore'
        refused = run('restore', '--snapshot', snap, '--destination', out, '--manifest-sha256', sha)
        assert refused.returncode != 0
        if mutation == 'path-traversal':
            assert 'invalid or duplicate manifest path' in refused.stderr
        elif mutation == 'sqlite-corruption':
            assert 'database' in refused.stderr.lower()
        assert not out.exists()
    finally:
        state.close()
        inbox.close()
