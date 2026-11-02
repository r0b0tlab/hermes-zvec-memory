"""Production-shaped FTS controls; these are not the production view SQL."""
import importlib.util
from pathlib import Path
import sqlite3

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / 'scripts' / 'recovery_snapshot.py'
spec = importlib.util.spec_from_file_location('fts_refusal_recovery', SCRIPT)
assert spec and spec.loader
recovery = importlib.util.module_from_spec(spec)
spec.loader.exec_module(recovery)


def setup_fts(db):
    # The view SQL is explicitly synthetic, not a transcription of production.
    db.executescript("""
      CREATE TABLE sessions(id INTEGER PRIMARY KEY, label TEXT);
      CREATE TABLE messages(id INTEGER PRIMARY KEY, content TEXT, tool_name TEXT,
                            tool_calls TEXT, session_id INTEGER);
      INSERT INTO sessions VALUES (1, 'fixture');
      INSERT INTO messages VALUES (1, 'alpha beta gamma', 'tool', '{"k":1}', 1);
      CREATE VIEW messages_fts_src AS
        SELECT m.id, CASE WHEN m.content IS NULL THEN '' ELSE substr(m.content, 1, 1000) END AS content,
               coalesce(m.tool_name, '') AS tool_name, json_extract(m.tool_calls, '$') AS tool_calls
          FROM messages m JOIN sessions s ON s.id = m.session_id;
      CREATE VIEW messages_fts_trigram_src AS
        SELECT id, substr(coalesce(content, ''), 1, 1000) AS content,
               coalesce(tool_name, '') AS tool_name FROM messages;
      CREATE VIRTUAL TABLE messages_fts_trigram USING fts5(content, tool_name, content='messages_fts_trigram_src', content_rowid='id', tokenize='trigram');
      CREATE VIRTUAL TABLE messages_fts USING fts5(content, tool_name, tool_calls, content='messages_fts_src', content_rowid='id');
      INSERT INTO messages_fts(messages_fts) VALUES('rebuild');
      INSERT INTO messages_fts_trigram(messages_fts_trigram) VALUES('rebuild');
    """)
    db.commit()


def fts_check(db, name):
    db.execute(f"INSERT INTO {name}({name}, rank) VALUES('integrity-check', 1)")


def test_real_fts5_view_backed_wal_refused_and_raw_evidence_preserved(tmp_path):
    path = tmp_path / 'source.db'
    db = sqlite3.connect(path)
    try:
        assert db.execute('PRAGMA journal_mode=wal').fetchone() == ('wal',)
        db.execute('PRAGMA wal_autocheckpoint=0')
        setup_fts(db)
        db.execute("UPDATE messages SET content='wal changed content' WHERE id=1")
        for name in ('messages_fts', 'messages_fts_trigram'):
            db.execute(f"INSERT INTO {name}({name}) VALUES('rebuild')")
        db.commit()
        assert db.execute("select rowid from messages_fts where messages_fts match 'changed'").fetchall() == [(1,)]
        assert db.execute("select rowid from messages_fts_trigram where messages_fts_trigram match 'change'").fetchall() == [(1,)]
        for name in ('messages_fts', 'messages_fts_trigram'):
            fts_check(db, name)
        db.rollback()
        assert sum(row[2] == 'shadow' for row in db.execute('pragma main.table_list')) == 8
        files = [path, Path(str(path) + '-wal'), Path(str(path) + '-shm')]
        before = [p.read_bytes() for p in files]
        with pytest.raises(recovery.Refusal, match='unsupported SQLite FTS schema'):
            recovery.sqlite_copy(path, path, tmp_path / 'backup.db', tmp_path)
        assert [p.read_bytes() for p in files] == before
    finally:
        db.close()


@pytest.mark.parametrize('mutation', ['content', 'shadow'])
def test_real_fts_integrity_detects_inconsistent_content_or_shadow(tmp_path, mutation):
    path = tmp_path / 'source.db'
    db = sqlite3.connect(path)
    try:
        setup_fts(db)
        fts_check(db, 'messages_fts')
        db.commit()
        if mutation == 'content':
            db.execute("UPDATE messages SET content='unsynchronized change'")
        else:
            db.execute("DELETE FROM messages_fts_docsize WHERE id=1")
        db.commit()
        with pytest.raises(sqlite3.DatabaseError, match='malformed'):
            fts_check(db, 'messages_fts')
        db.rollback()
        with pytest.raises(recovery.Refusal, match='unsupported SQLite FTS schema'):
            recovery.sqlite_copy(path, path, tmp_path / 'backup.db', tmp_path)
    finally:
        db.close()


@pytest.mark.parametrize('target', ['src', 'dst'])
@pytest.mark.parametrize('kind', ['attached', 'temp'])
def test_attached_database_refused_before_physical_comparison(tmp_path, target, kind):
    path, out, other = (tmp_path / n for n in ('source.db', 'out.db', 'other.db'))
    src, dst = sqlite3.connect(path), sqlite3.connect(out)
    try:
        src.execute('CREATE TABLE t(v)')
        src.commit()
        src.backup(dst)
        connection = src if target == 'src' else dst
        if kind == 'attached':
            connection.execute('ATTACH DATABASE ? AS extra', (str(other),))
        else:
            connection.execute('CREATE TEMP TABLE excluded(v)')
        with pytest.raises(recovery.Refusal, match='attached databases'):
            recovery.compare_sqlite_pages(src, dst, path, out, tmp_path)
    finally:
        src.close()
        dst.close()


def test_unsupported_fts_tokenizer_is_refused(tmp_path):
    source = tmp_path / 'source.db'
    with sqlite3.connect(source) as db:
        db.execute("CREATE VIRTUAL TABLE x USING fts5(content, tokenize='porter')")
    with pytest.raises(recovery.Refusal, match='virtual tables and shadow tables'):
        recovery.sqlite_copy(source, source, tmp_path / 'out.db', tmp_path)
