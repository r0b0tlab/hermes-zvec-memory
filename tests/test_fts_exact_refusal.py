"""Exact host schema in synthetic databases and historical identity control."""
import importlib.util
import os
from pathlib import Path
import resource
import sqlite3
import subprocess
import sys
import time

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / 'scripts' / 'recovery_snapshot.py'
spec = importlib.util.spec_from_file_location('exact_fts_recovery', SCRIPT)
assert spec and spec.loader
recovery = importlib.util.module_from_spec(spec)
spec.loader.exec_module(recovery)

VIEWS = """
CREATE VIEW messages_fts_src AS SELECT id, CASE WHEN role = 'tool' THEN substr(COALESCE(content, ''), 1, 8192) ELSE content END AS content, tool_name, tool_calls FROM messages;
CREATE VIEW messages_fts_trigram_src AS SELECT m.id, m.role, m.content, m.tool_name FROM messages AS m JOIN sessions AS s ON s.id = m.session_id WHERE m.role <> 'tool' AND s.source NOT IN ('cron', 'subagent') AND json_extract(COALESCE(s.model_config, '{}'), '$._delegate_from') IS NULL;
"""
FTS = """
CREATE VIRTUAL TABLE messages_fts USING fts5(content, tool_name, tool_calls, content='messages_fts_src', content_rowid='id');
CREATE VIRTUAL TABLE messages_fts_trigram USING fts5(content, tool_name, content='messages_fts_trigram_src', content_rowid='id', tokenize='trigram');
"""
NAMES = ('messages_fts', 'messages_fts_trigram')


def setup(db):
    db.executescript("""
    CREATE TABLE sessions(id INTEGER PRIMARY KEY, source TEXT, model_config TEXT);
    CREATE TABLE messages(id INTEGER PRIMARY KEY, session_id INTEGER, role TEXT,
                          content TEXT, tool_name TEXT, tool_calls TEXT);
    INSERT INTO sessions VALUES (1,'cli',NULL), (2,'cron',NULL),
        (3,'cli','{"_delegate_from":"parent"}');
    INSERT INTO messages VALUES (1,1,'user','synthetic alpha beta','search',NULL),
        (2,1,'tool','synthetic tool reply','runner','{}'),
        (3,2,'assistant','synthetic cron entry',NULL,NULL),
        (4,3,'assistant','synthetic delegated entry',NULL,NULL);
    """ + VIEWS + FTS + """
    INSERT INTO messages_fts(messages_fts) VALUES('rebuild');
    INSERT INTO messages_fts_trigram(messages_fts_trigram) VALUES('rebuild');
    """)
    db.commit()


def check_fts(db):
    for name in NAMES:
        db.execute(f"INSERT INTO {name}({name}, rank) VALUES('integrity-check', 1)")
    db.rollback()


def test_exact_schema_real_wal_integrity_and_admission(tmp_path):
    path = tmp_path / 'source.db'
    db = sqlite3.connect(path)
    try:
        assert db.execute('PRAGMA journal_mode=wal').fetchone() == ('wal',)
        db.execute('PRAGMA wal_autocheckpoint=0')
        setup(db)
        assert db.execute('SELECT id FROM messages_fts_trigram_src ORDER BY id').fetchall() == [(1,)]
        sql = dict(db.execute("SELECT name,sql FROM main.sqlite_schema WHERE name IN "
                              "('messages_fts','messages_fts_trigram','messages_fts_src','messages_fts_trigram_src')"))
        expected = [part.strip() for part in (VIEWS + FTS).split(';') if part.strip()]
        assert set(sql.values()) == set(expected)
        db.execute("UPDATE messages SET content='synthetic wal new text' WHERE id=1")
        for name in NAMES:
            db.execute(f"INSERT INTO {name}({name}) VALUES('rebuild')")
        db.commit()
        assert db.execute('PRAGMA integrity_check').fetchall() == [('ok',)]
        check_fts(db)
        assert db.execute("SELECT rowid FROM messages_fts WHERE messages_fts MATCH 'new'").fetchall() == [(1,)]
        assert db.execute("SELECT rowid FROM messages_fts_trigram WHERE messages_fts_trigram MATCH 'new'").fetchall() == [(1,)]
        assert {r[1] for r in db.execute('PRAGMA main.table_list') if r[2] == 'shadow'} == {
            f'{name}_{suffix}' for name in NAMES for suffix in ('data', 'idx', 'docsize', 'config')}
        inputs = [path, Path(str(path) + '-wal'), Path(str(path) + '-shm')]
        before = [(p.stat().st_size, recovery.digest(p)) for p in inputs]
        recovery.sqlite_copy(path, path, tmp_path / 'out.db', tmp_path)
        with sqlite3.connect(tmp_path / 'out.db') as restored:
            check_fts(restored)
            assert restored.execute("SELECT rowid FROM messages_fts WHERE messages_fts MATCH 'new'").fetchall() == [(1,)]
        assert [(p.stat().st_size, recovery.digest(p)) for p in inputs] == before
    finally:
        db.close()


@pytest.mark.parametrize('change', ['content', 'shadow'])
def test_exact_schema_real_fts_integrity_detects_corruption(tmp_path, change):
    path = tmp_path / 'source.db'
    db = sqlite3.connect(path)
    try:
        setup(db)
        check_fts(db)
        if change == 'content':
            db.execute("UPDATE messages SET content='synthetic unsynchronized change' WHERE id=1")
        else:
            db.execute('DELETE FROM messages_fts_docsize WHERE id=1')
        db.commit()
        with pytest.raises(sqlite3.DatabaseError, match='malformed|checksum mismatch'):
            check_fts(db)
        db.rollback()
        with pytest.raises(recovery.Refusal, match='FTS integrity check failed'):
            recovery.sqlite_copy(path, path, tmp_path / 'out.db', tmp_path)
    finally:
        db.close()


# A module name is not an implementation identity. This real SQLite loadable
# extension replaces the connection's fts5 registration with a different module.
FAKE_EXTENSION = r'''
#include <sqlite3ext.h>
SQLITE_EXTENSION_INIT1
static int fake(sqlite3 *db, void *p, int n, const char *const*v,
                sqlite3_vtab **out, char **err){
  (void)db; (void)p; (void)n; (void)v; (void)out;
  *err=sqlite3_mprintf("REPLACED_FTS5_IMPLEMENTATION"); return SQLITE_ERROR;
}
static int disconnect(sqlite3_vtab *v){(void)v; return SQLITE_OK;}
static sqlite3_module module = {.iVersion=1, .xCreate=fake, .xConnect=fake,
  .xDisconnect=disconnect, .xDestroy=disconnect};
#ifdef _WIN32
__declspec(dllexport)
#endif
int sqlite3_extension_init(sqlite3 *db,char **err,const sqlite3_api_routines *api){
  (void)err; SQLITE_EXTENSION_INIT2(api);
  return sqlite3_create_module(db,"fts5",&module,0);
}
'''


@pytest.mark.skip(reason='historical outside-TCB demonstration loads an extension; do not execute')
def test_module_list_does_not_prove_fts5_implementation(tmp_path):
    c, so = tmp_path / 'override.c', tmp_path / 'override.so'
    c.write_text(FAKE_EXTENSION)
    result = subprocess.run(['cc', '-shared', '-fPIC', '-o', str(so), str(c)],
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    db = sqlite3.connect(':memory:')
    try:
        db.execute('CREATE VIRTUAL TABLE before_override USING fts5(content)')
        assert ('fts5',) in db.execute('PRAGMA module_list').fetchall()
        db.enable_load_extension(True)
        db.load_extension(str(so))
        db.enable_load_extension(False)
        assert ('fts5',) in db.execute('PRAGMA module_list').fetchall()
        with pytest.raises(sqlite3.OperationalError, match='REPLACED_FTS5_IMPLEMENTATION'):
            db.execute('CREATE VIRTUAL TABLE after_override USING fts5(content)')
    finally:
        db.close()


LARGE_CHILD = r'''
import importlib.util, pathlib, resource, sqlite3, sys
root = pathlib.Path(sys.argv[1]); test = sys.argv[2]
resource.setrlimit(resource.RLIMIT_AS, (192*1024*1024, 192*1024*1024))
spec = importlib.util.spec_from_file_location('fts_fixture', test)
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
source = root / 'large.db'
db = sqlite3.connect(source)
db.execute('pragma cache_size=-2048')
m.setup(db)
db.execute('create table ballast (id integer primary key, data blob)')
for i in range(270): db.execute('insert into ballast values (?, zeroblob(1048576))', (i,))
db.commit(); db.close()
assert source.stat().st_size > 256*1024*1024
size = source.stat().st_size
db = sqlite3.connect(source)
assert db.execute('pragma journal_mode=wal').fetchone() == ('wal',)
db.execute('pragma wal_autocheckpoint=0')
db.execute("update messages set content='synthetic wal large' where id=1")
for name in m.NAMES: db.execute(f"insert into {name}({name}) values('rebuild')")
db.commit(); m.check_fts(db)
inputs = [source, pathlib.Path(str(source)+'-wal'), pathlib.Path(str(source)+'-shm')]
before = [(p.stat().st_size, m.recovery.digest(p)) for p in inputs]
m.recovery.sqlite_copy(source, source, root/'out.db', root)
copy = sqlite3.connect(root/'out.db'); m.check_fts(copy)
assert copy.execute("select rowid from messages_fts where messages_fts match 'large'").fetchall() == [(1,)]
copy.close()
assert [(p.stat().st_size, m.recovery.digest(p)) for p in inputs] == before
print('SIZE_BYTES=%d WAL_BYTES=%d RLIMIT_AS_BYTES=%d MAX_RSS_KIB=%d EXACT_FTS_ADMITTED=1' %
      (size, inputs[1].stat().st_size, resource.getrlimit(resource.RLIMIT_AS)[0],
       resource.getrusage(resource.RUSAGE_SELF).ru_maxrss))
db.close()
'''


def test_large_exact_fts_admitted_under_bounded_memory_and_disk(tmp_path):
    proc = subprocess.Popen([sys.executable, '-B', '-c', LARGE_CHILD, str(tmp_path), __file__],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=os.environ.copy())
    peak, deadline = 0, time.monotonic() + 600
    try:
        while proc.poll() is None:
            total = 0
            for p in tmp_path.rglob('*'):
                try:
                    if p.is_file(): total += p.stat().st_size
                except FileNotFoundError:
                    pass
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
    assert 'EXACT_FTS_ADMITTED=1' in stdout
    print(stdout.strip() + f' SAMPLED_PEAK_FIXTURE_BYTES={peak}')
