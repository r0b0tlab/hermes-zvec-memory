"""Managed policy/artifact regressions. Native effects live in the separate opt-in lane."""
import importlib
import json
import os
from pathlib import Path
import shutil
import subprocess

import pytest
from test_provider import _mod
from test_engine import fake_runtime, fixture_node

engine = importlib.import_module(_mod.__name__ + '.engine')
PLUGIN = Path(engine.__file__).parent


def test_policy_injection_never_dispatches_watcher_callbacks(tmp_path):
    script = tmp_path / 'policy.mjs'
    script.write_text(f'import {{publicationOnlyWatcher, managedBackend}} from {json.dumps((PLUGIN / "managed-policy.mjs").as_uri())};\n' + """
import assert from 'node:assert/strict';
let calls = 0;
const watcher = publicationOnlyWatcher({onChanges(){calls++}, onPendingChange(){calls++}, onActivity(){calls++}});
watcher.start();
await watcher.flushPending(); await watcher.refreshPaths(); await watcher.close(); await watcher.close();
assert.equal(calls, 0);
class Backend {
  constructor(options) { this.options = options; }
  async search(input) { return {policy: input}; }
  async serverStatus() { return {version:'0.2.2'}; }
}
const b = managedBackend(Backend, {watchManagerFactory(){throw Error('must not use caller watcher')}});
assert.equal(b.options.watchManagerFactory, publicationOnlyWatcher);
assert.deepEqual(await b.search({autoUpdate:false, freshness:'eventual'}), {policy:{autoUpdate:false, freshness:'eventual'}});
for (const input of [{autoUpdate:true,freshness:'eventual'}, {autoUpdate:false,freshness:'wait_for_fresh'}, {}]) {
  assert.throws(() => b.search(input), /query refresh off/);
}
assert.deepEqual(await b.serverStatus(), {version:'0.2.2',managedPolicy:'plugin-publication-v1',filesystemWatcher:false});
console.log('policy controls passed');
""")
    proc = subprocess.run(['/usr/bin/node', str(script)], capture_output=True, text=True, timeout=5)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == 'policy controls passed'


@pytest.mark.parametrize('args', [['server', 'on'], ['server', '--stdio'], ['server']])
def test_managed_launcher_refuses_unsupervised_bootstrap(tmp_path, isolated_home, args):
    launcher = tmp_path / 'launcher'
    launcher.write_text(engine.launcher_script(isolated_home, {'runtime_dir': str(tmp_path / 'runtime')}, node='/not/executed'))
    launcher.chmod(0o700)
    proc = subprocess.run([str(launcher), *args], capture_output=True, text=True, timeout=5)
    assert proc.returncode == 2
    assert 'requires foreground server run' in proc.stderr


@pytest.mark.parametrize('args', [['server', 'run', '--listen', '127.0.0.1:17999'], ['query', '--mode', 'auto', '--refresh', 'off', '--fts=literal $value']])
def test_managed_launcher_routes_only_foreground_server_to_owned_entrypoint(tmp_path, isolated_home, args):
    node = tmp_path / 'capture'
    node.write_text('#!/usr/bin/python3\nimport sys,json\nprint(json.dumps(sys.argv[1:]))\n')
    node.chmod(0o700)
    config = {'runtime_dir': str(tmp_path / 'runtime with space')}
    launcher = tmp_path / 'launcher'
    launcher.write_text(engine.launcher_script(isolated_home, config, node=node))
    launcher.chmod(0o700)
    proc = subprocess.run([str(launcher), *args], capture_output=True, text=True, timeout=5)
    assert proc.returncode == 0, proc.stderr
    expected = [str(engine.entry_path(isolated_home, config)), *args]
    if args[:2] == ['server', 'run']:
        expected.insert(0, str(engine.runtime_root(isolated_home, config) / 'managed-server.mjs'))
    assert json.loads(proc.stdout) == expected


@pytest.mark.parametrize('asset', ['managed-server.mjs', 'managed-policy.mjs'])
def test_complete_managed_asset_generation_conflicts_before_any_creation(tmp_path, isolated_home, asset):
    root = fake_runtime(tmp_path)
    config = {'runtime_dir': str(root), 'node_bin': str(fixture_node(tmp_path / 'node'))}
    target = root / asset
    target.write_text('retained operator modification\n')
    target.chmod(0o600)
    before = {str(p): (p.read_bytes(), p.stat().st_mtime_ns) for p in tmp_path.rglob('*') if p.is_file()}
    with pytest.raises(RuntimeError, match='Managed artifact conflict: ' + asset):
        engine.ensure_engine(isolated_home, config)
    assert {str(p): (p.read_bytes(), p.stat().st_mtime_ns) for p in tmp_path.rglob('*') if p.is_file()} == before
    assert not engine.unit_path().exists()


def test_managed_assets_are_published_read_back_and_idempotent(tmp_path, isolated_home):
    root = fake_runtime(tmp_path)
    config = {'runtime_dir': str(root), 'node_bin': str(fixture_node(tmp_path / 'node'))}
    result = engine.ensure_engine(isolated_home, config)
    assert result['status'] == 'updated'
    assert set(result['writes']) == {'launcher', 'unit', 'manifest',
                                      'managed-server.mjs', 'managed-policy.mjs', 'managed-server-LICENSE.txt'}
    for name in ['managed-server.mjs', 'managed-policy.mjs', 'managed-server-LICENSE.txt']:
        assert (root / name).read_bytes() == (PLUGIN / name).read_bytes()
        assert (root / name).stat().st_mode & 0o777 == 0o600
    assets = engine._artifact_set(isolated_home, config, node=config['node_bin'], version='0.2.2')
    before = {str(path): (path.read_bytes(), path.stat().st_mode, path.stat().st_mtime_ns)
              for _, path, _, _ in assets}
    assert engine.ensure_engine(isolated_home, config)['status'] == 'present'
    assert {str(path): (path.read_bytes(), path.stat().st_mode, path.stat().st_mtime_ns)
            for _, path, _, _ in assets} == before


@pytest.mark.parametrize('fault', ['version', 'source'])
def test_managed_server_pin_refuses_before_sdk_import_or_listener(tmp_path, fault):
    # Copy only the reviewed text boundary, NOT a writable SDK/native runtime.
    source = Path(os.environ['ZVEC_TEST_BIN']).resolve().parents[2]
    pkg = tmp_path / 'sdk'
    files = ['package.json', 'dist/daemon/backend.js', 'dist/daemon/backend.d.ts',
             'dist/daemon/watch-manager.d.ts', 'dist/daemon/runtime.js', 'dist/daemon/config.js',
             'dist/daemon/http-server.js', 'dist/daemon/server-controller.js', 'dist/daemon/logger.js',
             'dist/mcp/request-state.js', 'dist/mcp/toolset.js', 'dist/cli/args.js']
    for name in files:
        dest = pkg / name
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source / name, dest)
    if fault == 'version':
        (pkg / 'package.json').write_text(json.dumps({'name': '@zvec/zvec-grep', 'version': '0.2.3'}))
    else:
        with (pkg / 'dist/daemon/backend.js').open('a') as stream:
            stream.write('\nthrow Error("unreviewed body must not execute");\n')
    proc = subprocess.run(['/usr/bin/node', str(PLUGIN / 'managed-server.mjs'), str(pkg / 'dist/cli/index.js'),
                           'server', 'run'], capture_output=True, text=True, timeout=5)
    assert proc.returncode == 1, proc
    assert ('requires pinned engine 0.2.2' if fault == 'version' else 'source mismatch: dist/daemon/backend.js') in proc.stderr
    assert 'unreviewed body' not in proc.stderr
    assert not (Path(os.environ['ZVEC_GREP_HOME']) / 'daemon/instance.lock').exists()
