"""Synthetic pager-image, WAL and bounded-resource regression controls."""
import importlib.util
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import time

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / 'scripts' / 'recovery_snapshot.py'
spec = importlib.util.spec_from_file_location('streaming_recovery', SCRIPT)
assert spec and spec.loader
recovery = importlib.util.module_from_spec(spec)
spec.loader.exec_module(recovery)


def test_storage_types_without_rowid_and_metadata(tmp_path):
    source = tmp_path / 'main.db'
    with sqlite3.connect(source) as db:
        db.executescript('''
          PRAGMA user_version=73;
          PRAGMA application_id=1234;
          CREATE TABLE items(k BLOB PRIMARY KEY, v, n) WITHOUT ROWID;
          INSERT INTO items VALUES (x'0001', NULL, 1);
          INSERT INTO items VALUES (x'FF', x'000102FF', 1.5);
          CREATE TABLE seq(id INTEGER PRIMARY KEY AUTOINCREMENT, t TEXT);
          INSERT INTO seq(t) VALUES('text');
          CREATE INDEX idx ON seq(t);
          CREATE VIEW example AS SELECT * FROM seq;
        ''')
    out = tmp_path / 'out.db'
    recovery.sqlite_copy(source, source, out, tmp_path)
    with sqlite3.connect(out) as db:
        assert db.execute('select typeof(v), typeof(n) from items order by k').fetchall() == [('null', 'integer'), ('blob', 'real')]
        assert db.execute('select seq from sqlite_sequence').fetchone() == (1,)
        assert db.execute('pragma user_version').fetchone() == (73,)
        assert db.execute('pragma application_id').fetchone() == (1234,)


def test_virtual_fts_refused_without_claiming_external_state(tmp_path):
    path = tmp_path / 'fts.db'
    with sqlite3.connect(path) as db:
        try:
            db.execute('CREATE VIRTUAL TABLE docs USING fts5(body)')
        except sqlite3.OperationalError as exc:
            pytest.skip('FTS5 unavailable: ' + str(exc))
        db.execute("insert into docs values ('synthetic words')")
    with pytest.raises(recovery.Refusal, match='virtual tables'):
        recovery.sqlite_copy(path, path, tmp_path / 'out.db', tmp_path)


@pytest.mark.parametrize('mutation', ['row', 'schema', 'metadata', 'blob'])
def test_valid_backup_tamper_rejected(tmp_path, mutation):
    source, output = tmp_path / 'in.db', tmp_path / 'out.db'
    src = sqlite3.connect(source)
    try:
        src.executescript("CREATE TABLE t(k INTEGER PRIMARY KEY, v, b BLOB); INSERT INTO t VALUES (1, NULL, x'010203');")
        dst = sqlite3.connect(output)
        src.backup(dst)
        if mutation == 'row': dst.execute("UPDATE t SET v='x'")
        elif mutation == 'schema': dst.execute('CREATE INDEX extra ON t(v)')
        elif mutation == 'metadata': dst.execute('PRAGMA user_version=1')
        else: dst.execute("UPDATE t SET b=x'010204'")
        dst.commit()
        assert dst.execute('PRAGMA integrity_check').fetchall() == [('ok',)]
        with pytest.raises(recovery.Refusal, match='logical pages'):
            recovery.compare_sqlite_pages(src, dst, source, output, tmp_path)
        dst.close()
    finally:
        src.close()


@pytest.mark.parametrize('cookie', [0, 1, 2, -2147483648, 2147483647])
def test_schema_cookie_preserved_exactly(tmp_path, cookie):
    source, output = tmp_path / 'in.db', tmp_path / 'out.db'
    with sqlite3.connect(source) as db:
        db.execute('create table t(v)')
        db.execute(f'pragma schema_version={cookie}')
    recovery.sqlite_copy(source, source, output, tmp_path)
    with sqlite3.connect(f'file:{output}?immutable=1', uri=True) as db:
        assert db.execute('pragma schema_version').fetchone() == (cookie,)


def test_wal_corruption_refused_and_original_evidence_untouched(tmp_path):
    source = tmp_path / 'main.db'
    db = sqlite3.connect(source)
    try:
        db.execute('pragma journal_mode=wal')
        db.execute('pragma wal_autocheckpoint=0')
        db.execute('create table t(v)')
        db.execute("insert into t values ('before')")
        db.commit()
        db.execute("update t set v='wal-only'")
        db.commit()
        wal = Path(str(source) + '-wal')
        shm = Path(str(source) + '-shm')
        before = source.read_bytes(), wal.read_bytes(), shm.read_bytes()
        recovery.sqlite_copy(source, source, tmp_path / 'good.db', tmp_path)
        with sqlite3.connect(tmp_path / 'good.db') as result:
            assert result.execute('select * from t').fetchall() == [('wal-only',)]
        assert (source.read_bytes(), wal.read_bytes(), shm.read_bytes()) == before
        corrupt = tmp_path / 'corrupt.db'
        corrupt.write_bytes(before[0])
        data = bytearray(before[1]); data[-1] ^= 1
        Path(str(corrupt) + '-wal').write_bytes(data)
        Path(str(corrupt) + '-shm').write_bytes(before[2])
        with pytest.raises(recovery.Refusal, match='corrupt SQLite WAL'):
            recovery.sqlite_copy(corrupt, corrupt, tmp_path / 'bad.db', tmp_path)
        assert (source.read_bytes(), wal.read_bytes(), shm.read_bytes()) == before
    finally:
        db.close()


LARGE_CHILD = r'''
import importlib.util, os, pathlib, resource, sqlite3, sys
root = pathlib.Path(sys.argv[1]); script = sys.argv[2]
resource.setrlimit(resource.RLIMIT_AS, (192*1024*1024, 192*1024*1024))
spec = importlib.util.spec_from_file_location('recovery_scale', script)
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
source = root / 'large.db'
db = sqlite3.connect(source)
db.execute('pragma cache_size=-2048')
db.execute('create table t(k integer primary key, data blob, marker text)')
for i in range(270):
    db.execute('insert into t values (?, zeroblob(1048576), ?)', (i, 'base'))
db.commit(); db.close()
size = source.stat().st_size
assert size > 256*1024*1024, size
db = sqlite3.connect(source)
assert db.execute('pragma journal_mode=wal').fetchone()[0] == 'wal'
db.execute('pragma wal_autocheckpoint=0')
db.execute("update t set marker='committed wal' where k=269")
db.commit()
wal = pathlib.Path(str(source)+'-wal')
wal_size = wal.stat().st_size
assert wal_size > 0
out = root / 'backup.db'
m.sqlite_copy(source, source, out, root)
check = sqlite3.connect(f'file:{out}?immutable=1', uri=True)
assert check.execute('select marker,length(data) from t where k=269').fetchone() == ('committed wal', 1048576)
assert check.execute('select count(*) from t').fetchone() == (270,)
check.close()
# Alter the output without changing the source, then check independently.
changed = sqlite3.connect(out); changed.execute("update t set marker='tampered' where k=269"); changed.commit(); changed.close()
changed = sqlite3.connect(f'file:{out}?mode=ro&immutable=1', uri=True)
assert not any(pathlib.Path(str(out)+s).exists() for s in ('-wal','-shm','-journal'))
assert changed.execute('pragma integrity_check').fetchall() == [('ok',)]
assert changed.execute('select marker from t where k=269').fetchone() == ('tampered',)
try:
    m.compare_sqlite_pages(db, changed, source, out, root)
except m.Refusal as exc:
    assert 'logical pages' in str(exc), str(exc)
else:
    raise AssertionError('tampered backup accepted')
changed.close()
# Exercise the same >256 MiB, bounded-memory lane with both real FTS5 variants.
# These view definitions are synthetic and do not qualify production SQL.
db.executescript("""
 CREATE VIEW messages_fts_src AS SELECT k AS id, marker AS content, marker AS tool_name, marker AS tool_calls FROM t;
 CREATE VIEW messages_fts_trigram_src AS SELECT k AS id, marker AS content, marker AS tool_name FROM t;
 CREATE VIRTUAL TABLE messages_fts USING fts5(content, tool_name, tool_calls, content='messages_fts_src', content_rowid='id');
 CREATE VIRTUAL TABLE messages_fts_trigram USING fts5(content, tool_name, content='messages_fts_trigram_src', content_rowid='id', tokenize='trigram');
 INSERT INTO messages_fts(messages_fts) VALUES('rebuild');
 INSERT INTO messages_fts_trigram(messages_fts_trigram) VALUES('rebuild');
""")
db.commit()
inputs = [source, wal, pathlib.Path(str(source)+'-shm')]
before = [(p.stat().st_size, m.digest(p)) for p in inputs]
try:
    m.sqlite_copy(source, source, root / 'fts-refused.db', root)
except m.Refusal as exc:
    assert 'unsupported SQLite FTS schema' in str(exc), str(exc)
else:
    raise AssertionError('unqualified large FTS backup accepted')
assert [(p.stat().st_size, m.digest(p)) for p in inputs] == before
db.close()
print('SIZE_BYTES=%d WAL_BYTES=%d RLIMIT_AS_BYTES=%d MAX_RSS_KIB=%d WAL_TAMPER_REFUSED=1 LARGE_FTS_REFUSED=1' % (size, wal_size, resource.getrlimit(resource.RLIMIT_AS)[0], resource.getrusage(resource.RUSAGE_SELF).ru_maxrss))
'''


def test_real_large_sqlite_wal_with_address_space_limit(tmp_path):
    proc = subprocess.Popen([sys.executable, '-B', '-c', LARGE_CHILD, str(tmp_path), str(SCRIPT)],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=os.environ.copy())
    peak, deadline = 0, time.monotonic() + 600
    try:
        while proc.poll() is None:
            total = 0
            for path in tmp_path.rglob('*'):
                try:
                    if path.is_file(): total += path.stat().st_size
                except FileNotFoundError:
                    pass  # Owned scratch files can disappear during cleanup.
            peak = max(peak, total)
            if total > 5 * 1024**3 or time.monotonic() > deadline:
                raise AssertionError(f'fixture disk/time limit exceeded: {total}')
            time.sleep(0.05)
        stdout, stderr = proc.communicate(timeout=5)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.communicate()
    assert proc.returncode == 0, stdout + stderr
    assert 'WAL_TAMPER_REFUSED=1' in stdout
    print(stdout.strip() + f' SAMPLED_PEAK_FIXTURE_BYTES={peak}')
