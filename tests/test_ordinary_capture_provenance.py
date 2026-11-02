"""Subprocess capture faults against tiny, synthetic offline sources only."""
import hashlib
import json
from pathlib import Path
import subprocess
import sys

import pytest

from test_recovery_snapshot import SCRIPT, create_args, fixture, source_bytes


CHILD = r'''
import importlib.util, os, pathlib, shutil, sys
script, stage, *args = sys.argv[1:]
spec = importlib.util.spec_from_file_location('snapshot_child', script)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
original = shutil.copyfileobj
injected = False
def copy(src, dst, *a, **kw):
    global injected
    target = pathlib.Path(dst.name)
    if target.as_posix().endswith('/vault/facts/a.md') and not injected:
        injected = True
        if stage == 'short-read':
            dst.write(src.read(2))
        elif stage == 'short-write':
            data = src.read()
            dst.write(data[:-1])
        elif stage in ('corrupt', 'corrupt-hash'):
            data = bytearray(src.read())
            data[0] ^= 1
            dst.write(data)
            if stage == 'corrupt-hash':
                real_digest = module.digest
                expected_hash = module.hashlib.sha256(pathlib.Path(src.name).read_bytes()).hexdigest()
                module.digest = lambda path: expected_hash if pathlib.Path(path) == target else real_digest(path)
        elif stage == 'source-drift':
            original(src, dst, *a, **kw)
            source = pathlib.Path(src.name)
            source.write_bytes(b'FACT')
        elif stage == 'replace':
            original(src, dst, *a, **kw)
            source = pathlib.Path(src.name)
            replacement = source.with_name('replacement.tmp')
            replacement.write_bytes(source.read_bytes())
            os.replace(replacement, source)
        else:
            raise AssertionError(stage)
        print('INJECTED ' + stage, file=sys.stderr)
    else:
        original(src, dst, *a, **kw)
shutil.copyfileobj = copy
sys.argv = [script, *args]
sys.exit(module.main())
'''


@pytest.mark.parametrize('stage', ['short-read', 'short-write', 'corrupt', 'corrupt-hash',
                                  'replace', 'source-drift'])
def test_ordinary_capture_refuses_copy_faults_and_source_replacement(tmp_path, stage):
    root, state, inbox = fixture(tmp_path)
    try:
        before = source_bytes(root)
        snap = tmp_path / 'snapshot'
        result = subprocess.run([sys.executable, '-B', '-c', CHILD, str(SCRIPT), stage,
                                 *map(str, create_args(root, snap))],
                                text=True, capture_output=True, timeout=30)
        assert 'INJECTED ' + stage in result.stderr
        assert result.returncode != 0, (result.stdout, result.stderr)
        assert 'copy drift' in result.stderr or 'source drift' in result.stderr
        assert 'manifest_sha256=' not in result.stdout
        assert not (snap / 'manifest.json').exists()
        if stage == 'source-drift':
            # This change belongs to the injected actor, not the capture process.
            before['vault/facts/a.md'] = b'FACT'
        assert source_bytes(root) == before
    finally:
        state.close()
        inbox.close()


def test_ordinary_capture_true_success_and_restore(tmp_path):
    root, state, inbox = fixture(tmp_path)
    try:
        source = root / 'vault/facts/a.md'
        source.write_bytes(bytes(range(256)) * 4097)
        (root / 'vault/facts/empty.md').write_bytes(b'')
        before = source_bytes(root)
        snap, restored = tmp_path / 'snapshot', tmp_path / 'restored'
        result = subprocess.run([sys.executable, '-B', str(SCRIPT), *map(str, create_args(root, snap))],
                                text=True, capture_output=True, timeout=30)
        assert result.returncode == 0, result.stderr
        sha = result.stdout.strip().split('=', 1)[1]
        entries = json.loads((snap / 'manifest.json').read_text())['entries']
        entry = next(e for e in entries if e['path'] == 'vault/facts/a.md')
        assert entry['size'] == len(before['vault/facts/a.md'])
        assert entry['sha256'] == hashlib.sha256(before['vault/facts/a.md']).hexdigest()
        assert (snap / entry['path']).read_bytes() == before['vault/facts/a.md']
        result = subprocess.run([sys.executable, '-B', str(SCRIPT), 'restore', '--snapshot', str(snap),
                                 '--destination', str(restored), '--manifest-sha256', sha],
                                text=True, capture_output=True, timeout=30)
        assert result.returncode == 0, result.stderr
        assert (restored / entry['path']).read_bytes() == before['vault/facts/a.md']
        empty = next(e for e in entries if e['path'] == 'vault/facts/empty.md')
        assert empty['size'] == 0 and empty['sha256'] == hashlib.sha256(b'').hexdigest()
        assert (restored / empty['path']).read_bytes() == b''
        assert source_bytes(root) == before
    finally:
        state.close()
        inbox.close()
