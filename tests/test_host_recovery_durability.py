"""Subprocess fault injection for host snapshot completeness and durability."""
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / 'scripts' / 'host_recovery.py'

# Instrument only actual CLI primitives in a child process, preserving real I/O.
CHILD = r'''
import importlib.util, os, pathlib, sys
script, mode, source, snap, log = sys.argv[1:6]
sys.path.insert(0, str(pathlib.Path(script).parent))
spec = importlib.util.spec_from_file_location('host_subject', script)
host = importlib.util.module_from_spec(spec)
spec.loader.exec_module(host)
real_fdopen, real_open, real_fsync = os.fdopen, pathlib.Path.open, os.fsync

def fdpath(fd):
    return os.readlink('/proc/self/fd/' + str(fd))

class Reader:
    def __init__(self, f): self.f, self.calls = f, 0
    def __enter__(self): return self
    def __exit__(self, *args): return self.f.__exit__(*args)
    def fileno(self): return self.f.fileno()
    def read(self, size):
        self.calls += 1
        if self.calls > 1: return b''
        return self.f.read(size)

def fdopen(fd, *args, **kwargs):
    f = real_fdopen(fd, *args, **kwargs)
    return Reader(f) if mode == 'short-read' and fdpath(fd) == source else f
os.fdopen = fdopen

class Writer:
    def __init__(self, f): self.f = f
    def __enter__(self): return self
    def __exit__(self, *args): return self.f.__exit__(*args)
    def __getattr__(self, name): return getattr(self.f, name)
    def write(self, data):
        n = max(1, len(data) // 2)
        self.f.write(data[:n])
        return n

def path_open(path, *args, **kwargs):
    f = real_open(path, *args, **kwargs)
    if mode == 'short-write' and str(path).startswith(snap + '/payload/') and args and args[0] == 'xb':
        return Writer(f)
    return f
pathlib.Path.open = path_open

def fsync(fd):
    target = fdpath(fd)
    with open(log, 'a', encoding='utf8') as out: out.write(target + '\n')
    if mode == 'fsync:' + target: raise OSError('injected fsync failure: ' + target)
    return real_fsync(fd)
os.fsync = fsync

real_verify = host.verify
def verify(snapshot, digest):
    if mode == 'tamper-before-verify':
        p = next((snapshot / 'payload').iterdir())
        with open(p, 'r+b') as f: f.write(b'corrupt!')
    elif mode == 'backup-before-verify':
        p = next((snapshot / 'backups').iterdir())
        with open(p, 'r+b') as f: f.write(b'corrupt!')
    elif mode == 'extra-before-verify':
        (snapshot / 'unexpected').write_bytes(b'extra')
    result = real_verify(snapshot, digest)
    with open(log, 'a', encoding='utf8') as out: out.write('VERIFIED\n')
    return result
host.verify = verify
sys.argv = [script, 'create', '--scope', 'home=' + str(pathlib.Path(source).parent),
            '--sqlite', 'home/db', '--destination', snap,
            '--writers-quiesced', '--include-sensitive-data']
sys.exit(host.main())
'''


def invoke(tmp_path, mode):
    source_dir = tmp_path / 'input'
    source_dir.mkdir(mode=0o700)
    source = source_dir / 'data'
    source.write_bytes(b'abc12345' * 200000)
    (source_dir / 'z-empty').write_bytes(b'')
    with sqlite3.connect(source_dir / 'db') as db:
        db.execute('create table x (n)')
        db.execute('insert into x values (42)')
    snap, log = tmp_path / 'snapshot', tmp_path / 'fsync.log'
    if mode.startswith('barrier:'):
        relative = mode.partition(':')[2]
        mode = 'fsync:' + str(tmp_path if relative == 'parent' else snap / relative if relative else snap)
    result = subprocess.run([sys.executable, '-B', '-c', CHILD, str(SCRIPT), mode,
                             str(source), str(snap), str(log)], text=True,
                            capture_output=True, timeout=30)
    events = log.read_text().splitlines() if log.exists() else []
    return result, snap, source, events


def test_positive_round_trip_and_barrier_order(tmp_path):
    result, snap, source, events = invoke(tmp_path, 'normal')
    assert result.returncode == 0, result.stderr
    sha = result.stdout.strip().removeprefix('manifest_sha256=')
    assert hashlib.sha256((snap / 'manifest.json').read_bytes()).hexdigest() == sha
    manifest = json.loads((snap / 'manifest.json').read_text())
    for entry in manifest['entries']:
        if entry['type'] == 'file':
            blob = snap / 'payload' / entry['blob']
            assert blob.read_bytes() == (source.parent / entry['path'].split('/', 1)[1]).read_bytes()
            assert str(blob) in events
            assert events.index(str(blob)) < events.index(str(snap / 'payload'))
    for backup in manifest['sqlite_backups']:
        path = snap / 'backups' / backup['file']
        assert str(path) in events
        assert events.index(str(path)) < events.index(str(snap / 'backups'))
        with sqlite3.connect(f'file:{path}?immutable=1', uri=True) as db:
            assert db.execute('select n from x').fetchall() == [(42,)]
    for name in ('payload', 'backups'):
        assert events.index(str(snap / name)) < events.index(str(snap / 'manifest.json'))
    assert (events.index(str(snap / 'manifest.json')) < events.index('VERIFIED')
            < events.index(str(snap)) < events.index(str(tmp_path)))
    restored = tmp_path / 'restore'
    check = subprocess.run([sys.executable, '-B', str(SCRIPT), 'restore', '--snapshot', str(snap),
                            '--destination', str(restored), '--manifest-sha256', sha],
                           capture_output=True, text=True, timeout=30)
    assert check.returncode == 0, check.stderr
    assert (restored / 'home/data').read_bytes() == source.read_bytes()


@pytest.mark.parametrize('mode,diagnostic', [
    ('short-read', 'short source read'),
    ('short-write', 'short payload write'),
    ('tamper-before-verify', 'payload mismatch'),
    ('backup-before-verify', 'backup mismatch'),
    ('extra-before-verify', 'unmanifested snapshot entry'),
])
def test_copy_and_verification_fail_without_attestation(tmp_path, mode, diagnostic):
    result, snap, _, _ = invoke(tmp_path, mode)
    assert result.returncode != 0, result.stdout
    assert diagnostic in result.stderr
    assert 'manifest_sha256=' not in result.stdout
    assert not (snap / 'manifest.json').exists()


@pytest.mark.parametrize('barrier', ['payload/00000002', 'backups/00000000.db', 'payload',
                                     'backups', 'manifest.json', '', 'parent'])
def test_each_fsync_barrier_fails_closed(tmp_path, barrier):
    result, snap, _, events = invoke(tmp_path, 'barrier:' + barrier)
    target = tmp_path if barrier == 'parent' else snap / barrier if barrier else snap
    assert str(target) in events, events
    assert result.returncode != 0, result.stdout
    assert 'injected fsync failure' in result.stderr
    assert 'manifest_sha256=' not in result.stdout
    assert not (snap / 'manifest.json').exists()
