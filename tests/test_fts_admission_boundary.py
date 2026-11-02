"""Narrow trusted-SQLite FTS admission; all databases are synthetic."""
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import time

import pytest

import test_fts_exact_refusal as fixture
import test_fts_host_representation as host_fixture_sql
import test_recovery_snapshot as snapshot_fixture
import test_host_recovery as host_fixture

r = fixture.recovery


def test_extension_loading_is_disabled():
    db = r.sqlite_connect(':memory:')
    try:
        with pytest.raises(sqlite3.OperationalError, match='not authorized'):
            db.execute("SELECT load_extension('never-loaded')")
    finally:
        db.close()


@pytest.mark.parametrize('failure', ['missing', 'error'])
def test_connection_fails_closed_if_disabling_extensions_fails(monkeypatch, failure):
    closed = []
    class NoDisable:
        def close(self): closed.append(True)
    class DisableError(NoDisable):
        def enable_load_extension(self, enabled):
            assert enabled is False
            raise sqlite3.OperationalError('synthetic disable failure')
    monkeypatch.setattr(r.sqlite3, 'connect', lambda *args, **kwargs:
                        NoDisable() if failure == 'missing' else DisableError())
    with pytest.raises(r.Refusal, match='cannot explicitly disable SQLite extensions'):
        r.sqlite_connect(':memory:')
    assert closed == [True]


@pytest.mark.parametrize('name', list(r.FTS_SQL))
def test_each_schema_change_refused(tmp_path, name):
    path = tmp_path / 'in.db'
    db = sqlite3.connect(path)
    fixture.setup(db)
    db.execute('pragma writable_schema=on')
    db.execute("update sqlite_schema set sql=replace(sql, 'CREATE ', 'create ') where name=?", (name,))
    db.commit()
    db.close()
    with pytest.raises(r.Refusal, match='unsupported SQLite FTS schema'):
        r.sqlite_copy(path, path, tmp_path / 'out.db', tmp_path)


@pytest.mark.parametrize('name', [f'{n}_{s}' for n in fixture.NAMES for s in ('data', 'idx', 'docsize', 'config')])
def test_each_shadow_definition_change_refused(tmp_path, name):
    path = tmp_path / 'in.db'
    db = sqlite3.connect(path)
    fixture.setup(db)
    db.execute('pragma writable_schema=on')
    db.execute("update sqlite_schema set sql=replace(sql, 'CREATE TABLE', 'create table') where name=?", (name,))
    db.commit()
    db.close()
    with pytest.raises(r.Refusal, match='unsupported SQLite FTS shadow schema'):
        r.sqlite_copy(path, path, tmp_path / 'out.db', tmp_path)


def test_extra_virtual_table_is_refused(tmp_path):
    path = tmp_path / 'in.db'
    with sqlite3.connect(path) as db:
        fixture.setup(db)
        db.execute('create virtual table extra using fts5(content)')
    with pytest.raises(r.Refusal, match='virtual tables and shadow tables'):
        r.sqlite_copy(path, path, tmp_path / 'out.db', tmp_path)


def test_changed_tokenizer_is_refused(tmp_path):
    path = tmp_path / 'in.db'
    db = sqlite3.connect(path)
    fixture.setup(db)
    db.execute('pragma writable_schema=on')
    db.execute("update sqlite_schema set sql=replace(sql, ?, ?) where name='messages_fts_trigram'",
               ("tokenize='trigram'", "tokenize='porter'"))
    db.commit()
    db.close()
    with pytest.raises(r.Refusal, match='unsupported SQLite FTS schema'):
        r.sqlite_copy(path, path, tmp_path / 'out.db', tmp_path)


def test_corrupted_fts_wal_refused_without_touching_evidence(tmp_path):
    path = tmp_path / 'in.db'
    db = sqlite3.connect(path)
    db.execute('pragma journal_mode=wal')
    db.execute('pragma wal_autocheckpoint=0')
    fixture.setup(db)
    db.execute("update messages set content='wal only' where id=1")
    for name in fixture.NAMES:
        db.execute(f"insert into {name}({name}) values('rebuild')")
    db.commit()
    files = [path, Path(str(path) + '-wal'), Path(str(path) + '-shm')]
    before = [p.read_bytes() for p in files]
    corrupt = tmp_path / 'corrupt.db'
    corrupt.write_bytes(before[0])
    data = bytearray(before[1]); data[-1] ^= 1
    Path(str(corrupt) + '-wal').write_bytes(data)
    Path(str(corrupt) + '-shm').write_bytes(before[2])
    with pytest.raises(r.Refusal, match='corrupt SQLite WAL'):
        r.sqlite_copy(corrupt, corrupt, tmp_path / 'out.db', tmp_path)
    assert [p.read_bytes() for p in files] == before
    db.close()


@pytest.mark.parametrize('base', ['messages', 'sessions'])
def test_view_dependency_cannot_replace_base_table(tmp_path, base):
    path = tmp_path / 'in.db'
    db = sqlite3.connect(path)
    fixture.setup(db)
    db.execute(f'DROP TABLE {base}')
    db.execute(f'CREATE VIEW {base} AS SELECT 1 AS id')
    db.commit()
    db.close()
    with pytest.raises(r.Refusal, match='content dependencies'):
        r.sqlite_copy(path, path, tmp_path / 'out.db', tmp_path)


def test_valid_modified_fts_backup_fails_page_comparison(tmp_path):
    source, out = tmp_path / 'in.db', tmp_path / 'out.db'
    db = sqlite3.connect(source)
    fixture.setup(db)
    r.sqlite_copy(source, source, out, tmp_path)
    changed = sqlite3.connect(out)
    changed.execute("update messages set content='valid changed index' where id=1")
    for name in fixture.NAMES:
        changed.execute(f"insert into {name}({name}) values('rebuild')")
    changed.commit()
    fixture.check_fts(changed)
    assert changed.execute('pragma integrity_check').fetchall() == [('ok',)]
    with pytest.raises(r.Refusal, match='logical pages'):
        r.compare_sqlite_pages(db, changed, source, out, tmp_path)
    changed.close()
    db.close()


@pytest.mark.parametrize('name', fixture.NAMES)
def test_each_index_inconsistency_is_refused(tmp_path, name):
    path = tmp_path / 'in.db'
    db = sqlite3.connect(path)
    fixture.setup(db)
    db.execute(f'DELETE FROM {name}_docsize WHERE id=1')
    db.commit()
    with pytest.raises(sqlite3.DatabaseError):
        db.execute(f"INSERT INTO {name}({name}, rank) VALUES('integrity-check', 1)")
    db.rollback()
    with pytest.raises(r.Refusal, match='FTS integrity check failed'):
        r.sqlite_copy(path, path, tmp_path / 'out.db', tmp_path)
    db.close()


@pytest.mark.parametrize('mode', ['snapshot', 'host'])
@pytest.mark.parametrize('representation', ['one-line', 'observed-host'])
def test_fts_wal_create_restore_end_to_end(tmp_path, mode, representation):
    if mode == 'snapshot':
        root, state, inbox = snapshot_fixture.fixture(tmp_path)
        path = root / 'vault' / 'fts.db'
    else:
        root, state = host_fixture.fixture(tmp_path)
        inbox = None
        path = root / 'fts.db'
    db = sqlite3.connect(path)
    try:
        db.execute('pragma journal_mode=wal')
        db.execute('pragma wal_autocheckpoint=0')
        if representation == 'observed-host':
            host_fixture_sql.setup_host(db)
            host_fixture_sql.assert_host_schema(db)
        else:
            fixture.setup(db)
        db.execute("update messages set content='committed integration needle' where id=1")
        for name in fixture.NAMES:
            db.execute(f"insert into {name}({name}) values('rebuild')")
        db.commit()
        snap, restored = tmp_path / 'snapshot', tmp_path / 'restore'
        if mode == 'snapshot':
            result = snapshot_fixture.run(*snapshot_fixture.create_args(root, snap))
            run = snapshot_fixture.run
        else:
            result = host_fixture.create(root, snap, '--sqlite', 'home/fts.db')
            run = host_fixture.run
        assert result.returncode == 0, result.stderr
        sha = result.stdout.strip().split('=')[1]
        result = run('restore', '--snapshot', snap, '--destination', restored, '--manifest-sha256', sha)
        assert result.returncode == 0, result.stderr
        if mode == 'snapshot':
            output = restored / 'vault' / 'fts.db'
        else:
            import json
            manifest = json.loads((snap / 'manifest.json').read_text())
            record = next(x for x in manifest['sqlite_backups'] if x['source'] == 'home/fts.db')
            output = restored / '.host-recovery-backups' / record['file']
        with sqlite3.connect(output) as copy:
            if representation == 'observed-host':
                host_fixture_sql.assert_host_schema(copy)
            fixture.check_fts(copy)
            for name in fixture.NAMES:
                assert copy.execute(f"select rowid from {name} where {name} match 'needle'").fetchall() == [(1,)]
    finally:
        db.close()
        state.close()
        if inbox is not None:
            inbox.close()


LARGE_WAL_CHILD = r'''
import pathlib, resource, sqlite3, sys
import test_fts_host_representation as host
import test_fts_exact_refusal as m
root = pathlib.Path(sys.argv[1])
resource.setrlimit(resource.RLIMIT_AS, (192*1024*1024, 192*1024*1024))
source = root/'in.db'; db = sqlite3.connect(source)
host.setup_host(db)
host.assert_host_schema(db)
db.execute('create table ballast(id integer primary key, data blob)')
db.execute('insert into ballast values(1, ?)', (bytes(1048576),)); db.commit()
assert db.execute('pragma journal_mode=wal').fetchone() == ('wal',)
db.execute('pragma wal_autocheckpoint=0')
# Repeat over the same pages; WAL itself exceeds 256 MiB, not just main.
for i in range(260):
    db.execute('update ballast set data=? where id=1', (bytes([1 + (i % 2)]) * 1048576,))
    db.commit()
db.execute("update messages set content='large committed wal needle' where id=1")
for name in m.NAMES: db.execute(f"insert into {name}({name}) values('rebuild')")
db.commit(); m.check_fts(db)
inputs = [source, pathlib.Path(str(source)+'-wal'), pathlib.Path(str(source)+'-shm')]
wal_size = inputs[1].stat().st_size
assert wal_size > 256*1024*1024, wal_size
before = [(p.stat().st_size, m.recovery.digest(p)) for p in inputs]
out = root/'out.db'; m.recovery.sqlite_copy(source, source, out, root)
assert [(p.stat().st_size, m.recovery.digest(p)) for p in inputs] == before
copy = sqlite3.connect(out); host.assert_host_schema(copy); m.check_fts(copy)
for name in m.NAMES:
    assert copy.execute(f"select rowid from {name} where {name} match 'needle'").fetchall() == [(1,)]
copy.close(); db.close()
print('WAL_BYTES=%d RLIMIT_AS_BYTES=%d MAX_RSS_KIB=%d HOST_FTS_WAL_ADMITTED=1' % (wal_size, resource.getrlimit(resource.RLIMIT_AS)[0], resource.getrusage(resource.RUSAGE_SELF).ru_maxrss))
'''


def test_host_representation_fts_256mib_wal_with_limits(tmp_path):
    env = os.environ.copy()
    env['PYTHONPATH'] = str(Path(__file__).parent)
    proc = subprocess.Popen([sys.executable, '-B', '-c', LARGE_WAL_CHILD, str(tmp_path)],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env)
    peak, deadline = 0, time.monotonic() + 900
    try:
        while proc.poll() is None:
            total = 0
            for path in tmp_path.rglob('*'):
                try:
                    if path.is_file():
                        total += path.stat().st_size
                except FileNotFoundError:
                    pass
            peak = max(peak, total)
            if total > 5 * 1024**3 or time.monotonic() > deadline:
                raise AssertionError(f'fixture disk/time limit exceeded: {total}')
            time.sleep(.1)
        stdout, stderr = proc.communicate(timeout=5)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.communicate()
    assert proc.returncode == 0, stdout + stderr
    assert 'HOST_FTS_WAL_ADMITTED=1' in stdout
    print(stdout.strip() + f' SAMPLED_PEAK_FIXTURE_BYTES={peak}')
