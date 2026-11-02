"""Observed raw host DDL represented in synthetic SQLite databases only."""
from pathlib import Path
from itertools import product
import sqlite3

import pytest

import test_fts_exact_refusal as fixture

r = fixture.recovery

# Independent transcription of the observed sqlite_master SQL; never normalize.
HOST_SQL = {
    'messages_fts_trigram': """CREATE VIRTUAL TABLE messages_fts_trigram USING fts5(
    content,
    tool_name,
    content='messages_fts_trigram_src',
    content_rowid='id',
    tokenize='trigram'
)""",
    'messages_fts': """CREATE VIRTUAL TABLE messages_fts USING fts5(
    content,
    tool_name,
    tool_calls,
    content='messages_fts_src',
    content_rowid='id'
)""",
    'messages_fts_src': """CREATE VIEW messages_fts_src AS
    SELECT id,
           CASE WHEN role = 'tool'
                THEN substr(COALESCE(content, ''), 1, 8192)
                ELSE content END AS content,
           tool_name, tool_calls
    FROM messages""",
    'messages_fts_trigram_src': """CREATE VIEW messages_fts_trigram_src AS
    SELECT m.id, m.role, m.content, m.tool_name
    FROM messages AS m
    JOIN sessions AS s ON s.id = m.session_id
    WHERE m.role <> 'tool' AND s.source NOT IN ('cron', 'subagent') AND json_extract(COALESCE(s.model_config, '{}'), '$._delegate_from') IS NULL""",
}

# All eight definitions independently compared by the parent to the immutable
# host backup. SQLite generates these definitions; they are not placeholders.
SHADOW_SUFFIXES = {
    'data': '(id INTEGER PRIMARY KEY, block BLOB)',
    'idx': '(segid, term, pgno, PRIMARY KEY(segid, term)) WITHOUT ROWID',
    'docsize': '(id INTEGER PRIMARY KEY, sz BLOB)',
    'config': '(k PRIMARY KEY, v) WITHOUT ROWID',
}
HOST_SHADOWS = {
    f'{name}_{suffix}': f"CREATE TABLE '{name}_{suffix}'{definition}"
    for name in fixture.NAMES for suffix, definition in SHADOW_SUFFIXES.items()
}


def setup_host(db):
    fixture.setup(db)
    for name in fixture.NAMES:
        db.execute(f'DROP TABLE {name}')
    for name in ('messages_fts_src', 'messages_fts_trigram_src'):
        db.execute(f'DROP VIEW {name}')
    for name in ('messages_fts_src', 'messages_fts_trigram_src',
                 'messages_fts', 'messages_fts_trigram'):
        db.execute(HOST_SQL[name])
    for name in fixture.NAMES:
        db.execute(f"INSERT INTO {name}({name}) VALUES('rebuild')")
    db.commit()


def assert_host_schema(db):
    expected = HOST_SQL | HOST_SHADOWS
    rows = dict(db.execute('SELECT name, sql FROM main.sqlite_schema'))
    assert {name: rows[name] for name in expected} == expected


def test_host_ddl_wal_backup_with_exact_views_and_shadows(tmp_path):
    path = tmp_path / 'source.db'
    db = sqlite3.connect(path)
    try:
        assert db.execute('PRAGMA journal_mode=wal').fetchone() == ('wal',)
        db.execute('PRAGMA wal_autocheckpoint=0')
        setup_host(db)
        assert_host_schema(db)
        db.execute("UPDATE messages SET content=? WHERE id=2", ('x' * 9000,))
        db.execute("UPDATE messages SET content='host wal needle' WHERE id=1")
        for name in fixture.NAMES:
            db.execute(f"INSERT INTO {name}({name}) VALUES('rebuild')")
        db.commit()
        assert db.execute('SELECT length(content) FROM messages_fts_src WHERE id=2').fetchone() == (8192,)
        assert db.execute('SELECT id FROM messages_fts_trigram_src ORDER BY id').fetchall() == [(1,)]
        fixture.check_fts(db)
        inputs = [path, Path(str(path) + '-wal'), Path(str(path) + '-shm')]
        before = [(p.stat().st_size, r.digest(p)) for p in inputs]
        out = tmp_path / 'out.db'
        r.sqlite_copy(path, path, out, tmp_path)
        assert [(p.stat().st_size, r.digest(p)) for p in inputs] == before
        with sqlite3.connect(out) as copy:
            assert_host_schema(copy)
            fixture.check_fts(copy)
            for name in fixture.NAMES:
                assert copy.execute(f"SELECT rowid FROM {name} WHERE {name} MATCH 'needle'").fetchall() == [(1,)]
    finally:
        db.close()


@pytest.mark.parametrize('host_flags', list(product((False, True), repeat=4)))
def test_exact_mixed_spellings_are_admitted(tmp_path, host_flags):
    path = tmp_path / 'mixed.db'
    db = sqlite3.connect(path)
    setup_host(db)
    db.execute('PRAGMA writable_schema=on')
    for name, use_host in zip(HOST_SQL, host_flags):
        db.execute('UPDATE sqlite_schema SET sql=? WHERE name=?',
                   (HOST_SQL[name] if use_host else r.FTS_SQL[name], name))
    db.commit()
    db.close()
    db = r.sqlite_connect(path)
    try:
        assert r.admit_sqlite_schema(db) is True
        fixture.check_fts(db)
    finally:
        db.close()


@pytest.mark.parametrize('name', sorted(HOST_SHADOWS))
def test_host_shadow_definition_change_refused(tmp_path, name):
    path = tmp_path / 'source.db'
    db = sqlite3.connect(path)
    setup_host(db)
    db.execute('PRAGMA writable_schema=on')
    db.execute('UPDATE sqlite_schema SET sql=? WHERE name=?',
               (HOST_SHADOWS[name].replace('CREATE TABLE', 'create table'), name))
    db.commit()
    db.close()
    with pytest.raises(r.Refusal, match='unsupported SQLite FTS shadow schema'):
        r.sqlite_copy(path, path, tmp_path / 'out.db', tmp_path)


@pytest.mark.parametrize('name,old,new', [
    ('messages_fts', "content='messages_fts_src'", "content='messages_fts_trigram_src'"),
    ('messages_fts_trigram', "tokenize='trigram'", "tokenize='porter'"),
    ('messages_fts_src', "role = 'tool'", "role = 'tool '"),
    ('messages_fts_src', '1, 8192', '1, 8193'),
    ('messages_fts_trigram_src', "'cron', 'subagent'", "'cron ', 'subagent'"),
    ('messages_fts_trigram_src', "'$._delegate_from'", "'$._delegate_ from'"),
    ('messages_fts_trigram_src', "s.source NOT IN", "s.source /* changed */ NOT IN"),
    ('messages_fts_trigram_src', "m.role <> 'tool' AND", "m.role <> 'tool' OR"),
])
def test_host_schema_semantic_or_comment_mutation_refused(tmp_path, name, old, new):
    path = tmp_path / 'source.db'
    db = sqlite3.connect(path)
    setup_host(db)
    assert old in HOST_SQL[name]
    db.execute('PRAGMA writable_schema=on')
    db.execute('UPDATE sqlite_schema SET sql=? WHERE name=?', (HOST_SQL[name].replace(old, new), name))
    db.commit()
    db.close()
    with pytest.raises(r.Refusal, match='unsupported SQLite FTS schema'):
        r.sqlite_copy(path, path, tmp_path / 'out.db', tmp_path)
