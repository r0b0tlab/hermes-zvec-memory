"""Closed WAL-mode pager capture: synthetic databases only."""
import importlib.util
from pathlib import Path
import sqlite3

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / 'scripts' / 'recovery_snapshot.py'
spec = importlib.util.spec_from_file_location('closed_wal_recovery', SCRIPT)
assert spec and spec.loader
recovery = importlib.util.module_from_spec(spec)
spec.loader.exec_module(recovery)


def sidecar(path, suffix):
    return Path(str(path) + suffix)


def closed_wal_db(path):
    db = sqlite3.connect(path)
    assert db.execute('PRAGMA journal_mode=WAL').fetchone() == ('wal',)
    db.executescript("CREATE TABLE t(v TEXT); INSERT INTO t VALUES('synthetic');")
    db.close()
    assert path.read_bytes()[18:20] == b'\x02\x02'
    assert not any(sidecar(path, s).exists() for s in ('-wal', '-shm', '-journal'))


def test_closed_wal_database_is_copied_without_creating_staged_sidecars(tmp_path, monkeypatch):
    source, output = tmp_path / 'source.db', tmp_path / 'backup.db'
    closed_wal_db(source)
    before = source.read_bytes()
    compare = recovery.compare_sqlite_pages
    seen = []
    def inspect(src, dst, staged, dest, work):
        assert not any(sidecar(staged, s).exists() for s in ('-wal', '-shm', '-journal'))
        seen.append(staged.parent.name)
        return compare(src, dst, staged, dest, work)
    monkeypatch.setattr(recovery, 'compare_sqlite_pages', inspect)
    size, sha = recovery.sqlite_copy(source, source, output, tmp_path)
    assert seen == ['source', 'reference']
    assert source.read_bytes() == before
    assert not any(sidecar(source, s).exists() for s in ('-wal', '-shm', '-journal'))
    assert (size, sha) == (output.stat().st_size, recovery.digest(output))
    db = sqlite3.connect(f'file:{output}?mode=ro&immutable=1', uri=True)
    try:
        assert db.execute('SELECT v FROM t').fetchall() == [('synthetic',)]
        assert db.execute('PRAGMA integrity_check').fetchall() == [('ok',)]
    finally:
        db.close()


@pytest.mark.parametrize('suffix', ['-wal', '-shm', '-journal'])
def test_unadmitted_staged_sidecar_is_refused(tmp_path, monkeypatch, suffix):
    source, output = tmp_path / 'source.db', tmp_path / 'backup.db'
    closed_wal_db(source)
    before = source.read_bytes()
    compare = recovery.compare_sqlite_pages
    def inject(src, dst, staged, dest, work):
        sidecar(staged, suffix).write_bytes(b'hostile')
        return compare(src, dst, staged, dest, work)
    monkeypatch.setattr(recovery, 'compare_sqlite_pages', inject)
    with pytest.raises(recovery.Refusal, match='sidecar|logical pages'):
        recovery.sqlite_copy(source, source, output, tmp_path)
    assert source.read_bytes() == before
    assert not any(sidecar(source, s).exists() for s in ('-wal', '-shm', '-journal'))


@pytest.mark.parametrize('suffix', ['-wal', '-shm'])
def test_admitted_empty_wal_or_shm_is_not_treated_as_missing(tmp_path, suffix):
    source = tmp_path / 'source.db'
    closed_wal_db(source)
    sidecar(source, suffix).write_bytes(b'')
    before = source.read_bytes(), sidecar(source, suffix).read_bytes()
    with pytest.raises(recovery.Refusal, match='corrupt SQLite WAL'):
        recovery.sqlite_copy(source, source, tmp_path / 'backup.db', tmp_path)
    assert (source.read_bytes(), sidecar(source, suffix).read_bytes()) == before


@pytest.mark.parametrize('lane', ['source', 'reference'])
@pytest.mark.parametrize('suffix', ['-wal', '-shm', '-journal'])
@pytest.mark.parametrize('kind', ['file', 'dangling-symlink'])
def test_sidecar_appearing_during_comparison_is_refused(tmp_path, monkeypatch, lane, suffix, kind):
    source = tmp_path / 'source.db'
    closed_wal_db(source)
    original = source.read_bytes()
    compare = recovery.compare_sqlite_pages
    def inject(src, dst, staged, dest, work):
        result = compare(src, dst, staged, dest, work)
        if staged.parent.name == lane:
            path = sidecar(staged, suffix)
            if kind == 'file':
                path.write_bytes(b'hostile')
            else:
                path.symlink_to('absent')
        return result
    monkeypatch.setattr(recovery, 'compare_sqlite_pages', inject)
    with pytest.raises(recovery.Refusal, match='staged sidecar drift'):
        recovery.sqlite_copy(source, source, tmp_path / 'backup.db', tmp_path)
    assert source.read_bytes() == original


@pytest.mark.parametrize('lane', ['source', 'reference'])
def test_staged_main_drift_after_comparison_is_refused(tmp_path, monkeypatch, lane):
    source = tmp_path / 'source.db'
    closed_wal_db(source)
    original = source.read_bytes()
    compare = recovery.compare_sqlite_pages
    def inject(src, dst, staged, dest, work):
        result = compare(src, dst, staged, dest, work)
        if staged.parent.name == lane:
            data = bytearray(staged.read_bytes())
            data[-1] ^= 1
            staged.write_bytes(data)
        return result
    monkeypatch.setattr(recovery, 'compare_sqlite_pages', inject)
    with pytest.raises(recovery.Refusal, match='SQLite evidence drift'):
        recovery.sqlite_copy(source, source, tmp_path / 'backup.db', tmp_path)
    assert source.read_bytes() == original


def test_real_committed_wal_never_uses_immutable_source(tmp_path, monkeypatch):
    source, output = tmp_path / 'source.db', tmp_path / 'backup.db'
    db = sqlite3.connect(source)
    try:
        assert db.execute('PRAGMA journal_mode=WAL').fetchone() == ('wal',)
        db.execute('PRAGMA wal_autocheckpoint=0')
        db.execute('CREATE TABLE t(v TEXT)')
        db.execute("INSERT INTO t VALUES('wal-only')")
        db.commit()
        paths = [source, sidecar(source, '-wal'), sidecar(source, '-shm')]
        before = [p.read_bytes() for p in paths]
        assert before[1]
        connect = recovery.sqlite_connect
        uris = []
        def spy(*args, **kwargs):
            if kwargs.get('uri') and ('/source/source.db?' in str(args[0]) or '/reference/reference.db?' in str(args[0])):
                uris.append(args[0])
            return connect(*args, **kwargs)
        monkeypatch.setattr(recovery, 'sqlite_connect', spy)
        recovery.sqlite_copy(source, source, output, tmp_path)
        assert len(uris) == 2 and all('immutable=1' not in uri for uri in uris)
        result = sqlite3.connect(f'file:{output}?mode=ro&immutable=1', uri=True)
        try:
            assert result.execute('SELECT v FROM t').fetchall() == [('wal-only',)]
        finally:
            result.close()
        assert [p.read_bytes() for p in paths] == before
    finally:
        db.close()


def test_closed_wal_mode_host_fts_schema_and_integrity(tmp_path):
    import test_fts_host_representation as host_fts
    source, output = tmp_path / 'source.db', tmp_path / 'backup.db'
    db = sqlite3.connect(source)
    assert db.execute('PRAGMA journal_mode=WAL').fetchone() == ('wal',)
    host_fts.setup_host(db)
    host_fts.assert_host_schema(db)
    db.close()
    assert source.read_bytes()[18:20] == b'\x02\x02'
    assert not any(sidecar(source, s).exists() for s in ('-wal', '-shm', '-journal'))
    before = source.read_bytes()
    recovery.sqlite_copy(source, source, output, tmp_path)
    db = sqlite3.connect(f'file:{output}?mode=ro&immutable=1', uri=True)
    try:
        host_fts.assert_host_schema(db)
        assert db.execute('PRAGMA integrity_check').fetchall() == [('ok',)]
    finally:
        db.close()
    assert source.read_bytes() == before


def test_closed_wal_mode_full_host_snapshot_and_restore(tmp_path):
    import json
    import subprocess
    import sys
    source = tmp_path / 'home'
    source.mkdir(mode=0o700)
    database = source / 'state.db'
    closed_wal_db(database)
    before = database.read_bytes()
    snapshot, restored = tmp_path / 'snapshot', tmp_path / 'restore'
    script = SCRIPT.parent / 'host_recovery.py'
    create = subprocess.run([sys.executable, '-B', str(script), 'create', '--scope', f'home={source}',
                             '--sqlite', 'home/state.db', '--destination', str(snapshot),
                             '--writers-quiesced', '--include-sensitive-data'], capture_output=True, text=True, timeout=30)
    assert create.returncode == 0, create.stderr
    manifest = json.loads((snapshot / 'manifest.json').read_text())
    entry = next(e for e in manifest['entries'] if e['path'] == 'home/state.db')
    assert (snapshot / 'payload' / entry['blob']).read_bytes() == before
    digest = create.stdout.strip().split('=', 1)[1]
    restore = subprocess.run([sys.executable, '-B', str(script), 'restore', '--snapshot', str(snapshot),
                              '--destination', str(restored), '--manifest-sha256', digest],
                             capture_output=True, text=True, timeout=30)
    assert restore.returncode == 0, restore.stderr
    assert (restored / 'home/state.db').read_bytes() == before
    backup = restored / '.host-recovery-backups' / manifest['sqlite_backups'][0]['file']
    db = sqlite3.connect(f'file:{backup}?mode=ro&immutable=1', uri=True)
    try:
        assert db.execute('SELECT v FROM t').fetchall() == [('synthetic',)]
    finally:
        db.close()
    assert database.read_bytes() == before
