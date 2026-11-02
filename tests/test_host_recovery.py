"""Synthetic host recovery tests, no ambient home or service access."""
import base64
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / 'scripts/host_recovery.py'
sys.path.insert(0, str(SCRIPT.parent))
spec = importlib.util.spec_from_file_location('host_recovery', SCRIPT)
host = importlib.util.module_from_spec(spec)
spec.loader.exec_module(host)


def run(*args):
    return subprocess.run([sys.executable, str(SCRIPT), *map(str, args)], capture_output=True, text=True)


def create(root, dst, *extra):
    return run('create', '--scope', f'home={root}', '--scope', f'target={root.parent}/external',
               '--sqlite', 'home/state.db', '--destination', dst,
               '--writers-quiesced', '--include-sensitive-data', *extra)


def fixture(tmp_path):
    root = tmp_path / 'source'
    root.mkdir(mode=0o700)
    for part in ('runtime', 'service', 'config', 'skills', 'wiki'):
        (root / part).mkdir()
        (root / part / 'entry').write_bytes((part + '\x00private').encode())
    (root / 'runtime' / 'entry').chmod(0o751)
    os.setxattr(root / 'config' / 'entry', 'user.synthetic', b'\x00private\xff')
    subprocess.run(['setfacl', '-m', 'u:12345:r--', str(root / 'config' / 'entry')], check=True)
    assert 'system.posix_acl_access' in os.listxattr(root / 'config' / 'entry')
    (tmp_path / 'external').write_bytes(b'external-target')
    (root / 'service' / 'link').symlink_to('../../external')
    (root / 'skills' / 'link').symlink_to('../wiki/entry')
    os.utime(root / 'runtime' / 'entry', ns=(1_600_000_000_123_456_789, 1_600_000_001_987_654_321))
    os.utime(root / 'service' / 'link', ns=(1_600_000_000_123_456_789, 1_600_000_001_987_654_321), follow_symlinks=False)
    db = sqlite3.connect(root / 'state.db')
    db.execute('pragma journal_mode=wal')
    db.execute('pragma wal_autocheckpoint=0')
    db.execute('create table data(v)')
    db.execute("insert into data values ('committed-in-wal')")
    db.commit()
    return root, db


def test_real_capture_and_exact_restore(tmp_path):
    root, db = fixture(tmp_path)
    try:
        snapshot, restored = tmp_path / 'snapshot', tmp_path / 'restore'
        wal = (root / 'state.db-wal').read_bytes()
        result = create(root, snapshot)
        assert result.returncode == 0, result.stderr
        sha = result.stdout.strip().split('=')[1]
        manifest = json.loads((snapshot / 'manifest.json').read_text())
        records = {e['path']: e for e in manifest['entries']}
        assert set(manifest['sources']) == {'home', 'target'}
        assert sum(manifest['scope_entry_counts'].values()) == len(records)
        assert manifest['host']['capture_uid'] == os.getuid()
        for name in ('home/runtime/entry', 'home/service/link'):
            assert records[name]['metadata']['atime_ns'] == 1_600_000_000_123_456_789
            assert records[name]['metadata']['mtime_ns'] == 1_600_000_001_987_654_321
        assert (snapshot / 'payload' / records['home/state.db-wal']['blob']).read_bytes() == wal
        backup = snapshot / 'backups' / manifest['sqlite_backups'][0]['file']
        with sqlite3.connect(f'file:{backup}?immutable=1', uri=True) as d:
            assert d.execute('select v from data').fetchall() == [('committed-in-wal',)]
        result = run('restore', '--snapshot', snapshot, '--destination', restored, '--manifest-sha256', sha)
        assert result.returncode == 0, result.stderr
        for rel, e in records.items():
            path = restored / rel
            assert host.metadata(path) == e['metadata']
            if e['type'] == 'symlink':
                assert os.readlink(path) == e['target']
            elif e['type'] == 'file':
                fd = os.open(path, os.O_RDONLY | os.O_NOATIME)
                with os.fdopen(fd, 'rb') as f:
                    assert hashlib.sha256(f.read()).hexdigest() == e['sha256']
        assert os.getxattr(restored / 'home/config/entry', 'user.synthetic') == b'\x00private\xff'
        assert os.getxattr(restored / 'home/config/entry', 'system.posix_acl_access') == os.getxattr(root / 'config/entry', 'system.posix_acl_access')
        copied_backup = restored / '.host-recovery-backups' / manifest['sqlite_backups'][0]['file']
        with sqlite3.connect(f'file:{copied_backup}?immutable=1', uri=True) as d:
            assert d.execute('select v from data').fetchall() == [('committed-in-wal',)]
    finally:
        db.close()


def test_failure_boundaries(tmp_path, monkeypatch):
    root, db = fixture(tmp_path)
    try:
        (root / 'bad').symlink_to('/etc/passwd')
        dst = tmp_path / 'badlink'
        result = create(root, dst)
        assert result.returncode and 'outside selected' in result.stderr
        assert not (dst / 'manifest.json').exists()
        (root / 'bad').unlink()
        os.mkfifo(root / 'fifo')
        result = create(root, tmp_path / 'fifo-out')
        assert result.returncode and 'unsupported filesystem' in result.stderr
        assert not (tmp_path / 'fifo-out/manifest.json').exists()
        (root / 'fifo').unlink()
        os.link(root / 'wiki/entry', root / 'hardlink')
        result = create(root, tmp_path / 'hard-out')
        assert result.returncode and 'hard links unsupported' in result.stderr
        (root / 'hardlink').unlink()
        monkeypatch.setattr(host.os, 'listxattr', lambda *a, **k: (_ for _ in ()).throw(OSError('unsupported xattrs')))
        with pytest.raises(OSError, match='unsupported xattrs'):
            host.metadata(root)
    finally:
        db.close()


def test_corrupt_wal_preserves_raw_partial_without_attestation(tmp_path):
    root, db = fixture(tmp_path)
    try:
        wal = root / 'state.db-wal'
        data = bytearray(wal.read_bytes())
        data[-1] ^= 0x80
        wal.write_bytes(data)
        snap = tmp_path / 'corrupt'
        result = create(root, snap)
        assert result.returncode and 'WAL/SHM' in result.stderr
        assert not (snap / 'manifest.json').exists()
        assert 'manifest_sha256=' not in result.stdout
        assert bytes(data) in [p.read_bytes() for p in (snap / 'payload').iterdir()]
    finally:
        db.close()


def test_missing_sqlite_sidecars_refuse(tmp_path):
    root, db = fixture(tmp_path)
    try:
        snap = tmp_path / 'single-file'
        result = run('create', '--scope', f'db={root / "state.db"}', '--sqlite', 'db',
                     '--destination', snap, '--writers-quiesced', '--include-sensitive-data')
        assert result.returncode != 0 and 'sidecar outside selected scopes' in result.stderr
        assert not (snap / 'manifest.json').exists()
    finally:
        db.close()


def test_tampering_and_partial_restore_refuse(tmp_path, monkeypatch):
    root, db = fixture(tmp_path)
    try:
        snap = tmp_path / 'snap'
        result = create(root, snap)
        assert result.returncode == 0, result.stderr
        sha = result.stdout.strip().split('=')[1]
        (snap / 'extra').write_text('extra')
        out = tmp_path / 'bad'
        result = run('restore', '--snapshot', snap, '--destination', out, '--manifest-sha256', sha)
        assert result.returncode and not out.exists()
        (snap / 'extra').unlink()
        real_setxattr = host.os.setxattr
        def fail(path, name, *args, **kwargs):
            if name == 'user.synthetic':
                raise OSError('simulated unsupported metadata')
            return real_setxattr(path, name, *args, **kwargs)
        monkeypatch.setattr(host.os, 'setxattr', fail)
        args = type('Args', (), {'snapshot': snap, 'destination': out, 'manifest_sha256': sha})()
        with pytest.raises(OSError, match='simulated unsupported metadata'):
            host.restore(args)
        assert out.is_dir()  # partial, no successful verification reported
    finally:
        db.close()


def test_rehashed_manifest_parent_symlink_rejected(tmp_path):
    root, db = fixture(tmp_path)
    try:
        snap = tmp_path / 'snap'
        result = create(root, snap)
        assert result.returncode == 0, result.stderr
        path = snap / 'manifest.json'
        data = json.loads(path.read_text())
        entry = next(e for e in data['entries'] if e['path'] == 'home/wiki')
        entry['type'] = 'symlink'
        entry['target'] = str(tmp_path / 'outside')
        path.write_text(json.dumps(data))
        sha = hashlib.sha256(path.read_bytes()).hexdigest()
        outside = tmp_path / 'outside'
        outside.mkdir()
        result = run('restore', '--snapshot', snap, '--destination', tmp_path / 'out', '--manifest-sha256', sha)
        assert result.returncode != 0
        assert not (outside / 'entry').exists()
    finally:
        db.close()
