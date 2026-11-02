"""Deterministic capture-copy and backup provenance faults, synthetic data only."""
import importlib.util
import json
from pathlib import Path
import sqlite3
import subprocess
import sys

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / 'scripts/host_recovery.py'

CHILD = r'''
import importlib.util, pathlib, shutil, sqlite3, sys
script, source, snapshot, stage, suffix = sys.argv[1:]
sys.path.insert(0, str(pathlib.Path(script).parent))
spec = importlib.util.spec_from_file_location('host_provenance', script)
host = importlib.util.module_from_spec(spec)
spec.loader.exec_module(host)
real = shutil.copyfile
injected = False
def copy(src, dst, *args, **kwargs):
    global injected
    dest = pathlib.Path(dst)
    if stage == 'backup' and dest.name == 'reference.db' and not injected:
        backup = pathlib.Path(snapshot) / 'backups/00000000.db'
        with sqlite3.connect(backup) as db:
            db.execute("update t set v='CORRUPTD-RECORD'")
            assert db.execute('pragma integrity_check').fetchall() == [('ok',)]
        db.close()
        print('INJECTED backup', file=sys.stderr)
        injected = True
    result = real(src, dst, *args, **kwargs)
    names = {'raw': 'raw.db', 'source': 'source.db', 'reference': 'reference.db'}
    if stage in names and dest.name == names[stage] + suffix and not injected:
        data = dest.read_bytes()
        if not suffix and b'ORIGINAL-RECORD' in data:
            data = data.replace(b'ORIGINAL-RECORD', b'CORRUPTD-RECORD')
        else:
            data = data[:-1] + bytes([data[-1] ^ 1])
        dest.write_bytes(data)
        print('INJECTED ' + stage + suffix, file=sys.stderr)
        injected = True
    return result
shutil.copyfile = copy
if stage == 'backup-api':
    real_connect = sqlite3.connect
    class Connection:
        def __init__(self, db): self.db = db
        def __getattr__(self, name): return getattr(self.db, name)
        def backup(self, target, **kwargs):
            self.db.backup(target.db, **kwargs)
            target.db.execute("update t set v='CORRUPTD-RECORD'")
            target.db.commit()
            assert target.db.execute('pragma integrity_check').fetchall() == [('ok',)]
            print('INJECTED backup-api', file=sys.stderr)
    def connect(*args, **kwargs): return Connection(real_connect(*args, **kwargs))
    sqlite3.connect = connect
real_sync = host.sync_path
def sync(path, **kwargs):
    if stage == 'after-comparison' and pathlib.Path(path).name == '00000000.db':
        db = sqlite3.connect(path)
        db.execute("update t set v='CORRUPTD-RECORD'")
        db.commit()
        assert db.execute('pragma integrity_check').fetchall() == [('ok',)]
        db.close()
        print('INJECTED after-comparison', file=sys.stderr)
    return real_sync(path, **kwargs)
host.sync_path = sync
sys.argv = [script, 'create', '--scope', 'home=' + source, '--sqlite', 'home/db',
            '--destination', snapshot, '--writers-quiesced', '--include-sensitive-data']
sys.exit(host.main())
'''


def invoke(tmp_path, stage, suffix='', wal=True):
    source = tmp_path / 'source'
    source.mkdir(mode=0o700)
    db = sqlite3.connect(source / 'db')
    if wal:
        db.execute('pragma journal_mode=wal')
        db.execute('pragma wal_autocheckpoint=0')
    db.execute('create table t(v text)')
    db.execute("insert into t values ('ORIGINAL-RECORD')")
    db.commit()
    if not wal:
        db.close()
    before = {p.name: p.read_bytes() for p in source.iterdir()}
    snapshot = tmp_path / 'snapshot'
    try:
        result = subprocess.run([sys.executable, '-B', '-c', CHILD, str(SCRIPT), str(source),
                                 str(snapshot), stage, suffix], text=True, capture_output=True, timeout=30)
        assert {p.name: p.read_bytes() for p in source.iterdir()} == before
        return result, snapshot, before
    finally:
        if wal:
            db.close()


@pytest.mark.parametrize('stage', ['raw', 'source', 'reference'])
@pytest.mark.parametrize('suffix', ['', '-wal', '-shm'])
def test_every_copy_refuses_drift(tmp_path, stage, suffix):
    result, snapshot, before = invoke(tmp_path, stage, suffix)
    assert 'INJECTED ' + stage + suffix in result.stderr
    assert result.returncode != 0 and 'SQLite copy drift' in result.stderr
    assert 'manifest_sha256=' not in result.stdout
    assert not (snapshot / 'manifest.json').exists()
    payload = [p.read_bytes() for p in (snapshot / 'payload').iterdir()]
    assert all(data in payload for data in before.values())


@pytest.mark.parametrize('stage', ['raw', 'source', 'reference'])
def test_valid_equal_size_main_mutation_refused(tmp_path, stage):
    result, snapshot, _ = invoke(tmp_path, stage, wal=False)
    assert 'INJECTED ' + stage in result.stderr
    assert result.returncode != 0 and 'SQLite copy drift' in result.stderr
    assert 'manifest_sha256=' not in result.stdout
    assert not (snapshot / 'manifest.json').exists()


def test_logically_tampered_valid_backup_refused(tmp_path):
    result, snapshot, _ = invoke(tmp_path, 'backup')
    assert 'INJECTED backup' in result.stderr
    assert result.returncode != 0 and 'backup differs from admitted source' in result.stderr
    assert 'manifest_sha256=' not in result.stdout
    assert not (snapshot / 'manifest.json').exists()


def test_tamper_after_comparison_cannot_be_attested(tmp_path):
    result, snapshot, _ = invoke(tmp_path, 'after-comparison')
    assert 'INJECTED after-comparison' in result.stderr
    assert result.returncode != 0 and 'backup mismatch' in result.stderr
    assert 'manifest_sha256=' not in result.stdout
    assert not (snapshot / 'manifest.json').exists()


def test_corrupt_backup_api_is_caught_by_serialized_source(tmp_path):
    result, snapshot, _ = invoke(tmp_path, 'backup-api')
    assert 'INJECTED backup-api' in result.stderr
    assert result.returncode != 0 and 'admitted source logical pages' in result.stderr
    assert 'manifest_sha256=' not in result.stdout
    assert not (snapshot / 'manifest.json').exists()


def test_table_inventory_required_before_page_comparison(tmp_path):
    spec = importlib.util.spec_from_file_location('recovery_limit', SCRIPT.parent / 'recovery_snapshot.py')
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    class NoInventory:
        def execute(self, sql): return iter(())
    output = tmp_path / 'output'
    output.touch()
    with pytest.raises(module.Refusal, match='inventory unavailable'):
        module.compare_sqlite_pages(NoInventory(), None, tmp_path / 'input', output, tmp_path)


def test_real_wal_control_includes_committed_data_and_restores(tmp_path):
    result, snapshot, before = invoke(tmp_path, 'normal')
    assert result.returncode == 0, result.stderr
    assert before['db-wal']
    main_path = tmp_path / 'main-only.db'
    main_path.write_bytes(before['db'])
    with sqlite3.connect(f'file:{main_path}?immutable=1', uri=True) as main:
        with pytest.raises(sqlite3.OperationalError, match='no such table'):
            main.execute('select v from t').fetchall()
    manifest = json.loads((snapshot / 'manifest.json').read_text())
    entries = {x['path']: x for x in manifest['entries']}
    for name, data in before.items():
        assert (snapshot / 'payload' / entries['home/' + name]['blob']).read_bytes() == data
    backup = snapshot / 'backups' / manifest['sqlite_backups'][0]['file']
    with sqlite3.connect(f'file:{backup}?immutable=1', uri=True) as db:
        assert db.execute('select v from t').fetchall() == [('ORIGINAL-RECORD',)]
    out = tmp_path / 'restored'
    restored = subprocess.run([sys.executable, '-B', str(SCRIPT), 'restore', '--snapshot', str(snapshot),
                               '--destination', str(out), '--manifest-sha256',
                               result.stdout.strip().split('=', 1)[1]], capture_output=True, text=True, timeout=30)
    assert restored.returncode == 0, restored.stderr
    with sqlite3.connect(f'file:{out}/.host-recovery-backups/00000000.db?immutable=1', uri=True) as db:
        assert db.execute('select v from t').fetchall() == [('ORIGINAL-RECORD',)]
