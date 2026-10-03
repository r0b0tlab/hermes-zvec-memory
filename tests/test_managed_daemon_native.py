"""Real bounded F1 controls. Upstream is a retained negative control, not acceptance."""
import hashlib
import importlib
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request

import pytest
from test_provider import ZvecMemoryProvider, _mod

engine = importlib.import_module(_mod.__name__ + ".engine")
ROOT = Path(__file__).resolve().parents[1]
pytestmark = [pytest.mark.integration, pytest.mark.skipif(
    os.environ.get("ZVEC_RUN_NATIVE") != "1", reason="opt-in native daemon")]


def bind_inputs():
    entry = Path(os.environ["ZVEC_TEST_BIN"]).resolve()
    package = entry.parents[2]
    paths = [Path('/usr/bin/node'), package / 'package.json']
    paths.extend(p for p in (ROOT / 'zvec-memory').rglob('*') if p.is_file() and p.suffix in {'.py', '.mjs', '.txt'})
    paths.extend(p for p in (ROOT / 'tests').glob('*managed*') if p.is_file())
    paths.extend(p for p in (package / 'dist').rglob('*') if p.is_file() and p.suffix in {'.js', '.ts'})
    paths.extend(p for p in Path(os.environ['ZVEC_TEST_MODEL_CACHE']).rglob('*') if p.is_file())
    result = {}
    for path in sorted(set(paths)):
        digest = hashlib.sha256()
        with path.open('rb') as stream:
            for data in iter(lambda: stream.read(1024 * 1024), b''):
                digest.update(data)
        stat = path.stat()
        result[str(path)] = [digest.hexdigest(), stat.st_size, stat.st_mode, stat.st_mtime_ns, stat.st_ino]
    return result


def verify_bindings(tmp, before):
    after = bind_inputs()
    (tmp / 'input-bindings-after.json').write_text(json.dumps(after, indent=2))
    assert after == before, 'source/SDK/Node/model drift'


class Lane:
    def __init__(self, tmp, managed):
        self.tmp, self.managed = tmp, managed
        self.vault = tmp / 'vault'
        (self.vault / 'facts').mkdir(parents=True)
        (self.vault / 'sessions').mkdir()
        self.fact = self.vault / 'facts/control.md'
        self.fact.write_text('Synthetic observatory safety requires a copper canary.\n')
        self.original = self.fact.read_bytes()
        self.engine_home = tmp / 'engine-state'
        self.engine_home.mkdir(mode=0o700)
        self.token = self.engine_home / 'server.token'
        self.token.write_text('local-synthetic-daemon-test-bearer-' + 'x' * 32)
        self.token.chmod(0o600)
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0))
            port = sock.getsockname()[1]
        self.url = f'http://127.0.0.1:{port}/mcp'
        self.cache = tmp / 'models'
        shutil.copytree(os.environ['ZVEC_TEST_MODEL_CACHE'], self.cache / 'models')
        self.runtime = tmp / 'runtime'
        (self.runtime / 'runtime').mkdir(parents=True)
        (self.runtime / 'runtime/node_modules').symlink_to(Path(os.environ['ZVEC_TEST_BIN']).resolve().parents[4])
        self.config = {'runtime_dir': str(self.runtime), 'engine_home': str(self.engine_home),
                       'model_cache': str(self.cache), 'server_url': self.url, 'node_bin': '/usr/bin/node'}
        self.launcher = self.runtime / 'zg-default'
        self.log = (self.engine_home / 'daemon/logs/server.log')
        self.commands = []
        self.process = None
        self.provider = None
        if managed:
            result = engine.ensure_engine(Path(os.environ['HERMES_HOME']), self.config)
            assert result['status'] == 'updated', result
        else:
            # Retain the exact f28abfc launcher's upstream route after candidate edits.
            source = subprocess.check_output(['git', 'show', 'f28abfc:zvec-memory/engine.py'], cwd=ROOT, text=True)
            namespace = {'__name__': engine.__name__, '__package__': engine.__package__}
            exec(compile(source, '<f28abfc-engine>', 'exec'), namespace)
            self.launcher.write_text(namespace['launcher_script'](Path(os.environ['HERMES_HOME']), self.config, node='/usr/bin/node'))
            self.launcher.chmod(0o700)
        self.env = dict(os.environ)
        self.env.update(ZVEC_GREP_HOME=str(self.engine_home), ZVEC_GREP_MODEL_CACHE=str(self.cache / 'models'),
                        ZVEC_GREP_SERVER_URL=self.url, ZVEC_GREP_SERVER_TOKEN_FILE=str(self.token), ZVEC_GREP_MODE='auto')
        self.client = tmp / 'client.mjs'
        module = Path(os.environ['ZVEC_TEST_BIN']).resolve().parents[1] / 'client/daemon-client.js'
        self.client.write_text(f'import {{DaemonClient}} from {json.dumps(module.as_uri())};\n'
                               'const c = new DaemonClient({serverUrl:process.env.ZVEC_GREP_SERVER_URL});\n'
                               'console.log(JSON.stringify(await c.callTool(process.argv[2], JSON.parse(process.argv[3]))));\n')

    def command(self, args, timeout=40):
        proc = subprocess.run([str(self.launcher), *args], cwd=self.vault, env=self.env,
                              stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=timeout)
        record = {'args': args, 'rc': proc.returncode, 'out': proc.stdout, 'err': proc.stderr}
        self.commands.append(record)
        assert proc.returncode == 0, record
        return proc.stdout

    def tool(self, name, args):
        proc = subprocess.run(['/usr/bin/node', str(self.client), name, json.dumps(args)], env=self.env,
                              cwd=self.vault, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=15)
        self.commands.append({'tool': name, 'args': args, 'rc': proc.returncode, 'out': proc.stdout, 'err': proc.stderr})
        assert proc.returncode == 0, self.commands[-1]
        return json.loads(proc.stdout)

    def start(self):
        stdout = (self.tmp / ('daemon-' + str(len(self.commands)) + '.stdout')).open('w')
        stderr = (self.tmp / ('daemon-' + str(len(self.commands)) + '.stderr')).open('w')
        try:
            self.process = subprocess.Popen([str(self.launcher), 'server', 'run', '--listen',
                          self.url.split('/')[2], '--token-file', str(self.token)], env=self.env,
                          cwd=self.vault, stdin=subprocess.DEVNULL, stdout=stdout, stderr=stderr)
        finally:
            stdout.close()
            stderr.close()
        deadline = time.monotonic() + 12
        while time.monotonic() < deadline:
            try:
                with urllib.request.urlopen(self.url.replace('/mcp', '/healthz'), timeout=1) as response:
                    if response.status == 200:
                        break
            except (urllib.error.URLError, TimeoutError):
                pass
            assert self.process.poll() is None, 'daemon exited; see retained stderr'
            time.sleep(.1)
        else:
            pytest.fail('daemon readiness deadline')
        self.command(['server', 'status', '--check-ready'])
        for path in ['/mcp', '/mcp/admin', '/control/shutdown']:
            request = urllib.request.Request(self.url.replace('/mcp', path), data=b'{}', method='POST',
                                             headers={'Content-Type': 'application/json'})
            with pytest.raises(urllib.error.HTTPError) as denied:
                urllib.request.urlopen(request, timeout=2)
            assert denied.value.code == 401
        assert self.tool('zvec_grep_server_status', {})['version'] == '0.2.2'
        request = urllib.request.Request(self.url + '/admin', data=b'{}', method='POST',
                  headers={'Content-Type': 'application/json', 'Authorization': 'Bearer invalid-synthetic-bearer'})
        with pytest.raises(urllib.error.HTTPError) as denied:
            urllib.request.urlopen(request, timeout=2)
        assert denied.value.code == 401

    def stop(self):
        if self.process is not None:
            if self.process.poll() is None:
                request = urllib.request.Request(self.url.replace('/mcp', '/control/shutdown'), data=b'{}',
                       headers={'Authorization': 'Bearer ' + self.token.read_text(), 'Content-Type': 'application/json'})
                with urllib.request.urlopen(request, timeout=3) as response:
                    assert response.status == 202
                assert self.process.wait(timeout=12) == 0
            self.process = None
        assert not (self.engine_home / 'daemon/instance.lock').exists()

    def initialize(self):
        self.provider = ZvecMemoryProvider(config={'vault': str(self.vault), 'zg_bin': str(self.launcher),
            'embedding': 'local/potion-retrieval-32m', 'reindex_min_seconds': 3600, 'preview': 'full'})
        original_run = self.provider._run_zg
        def recorded(args, timeout):
            result = original_run(args, min(timeout, 50))
            self.commands.append({'provider_args': args, 'rc': result[0], 'out': result[1], 'err': result[2]})
            return result
        self.provider._run_zg = recorded
        self.provider.initialize('daemon-control', hermes_home=os.environ['HERMES_HOME'])
        assert self.provider._index_worker.drain(55)
        assert self.provider._disk_worker.drain(10)
        assert self.provider._recall_token() is not None

    def state(self):
        assert self.process is not None and self.provider is not None
        watched = []
        for fd in Path(f'/proc/{self.process.pid}/fd').iterdir():
            try:
                if 'inotify' in os.readlink(fd):
                    watched.append(fd.name)
            except FileNotFoundError:
                pass
        return {'native': self.tool('zvec_grep_index_status', {'root': str(self.vault)}),
                'inotify_fds': watched,
                'generation': self.provider._index_generation(),
                'token': self.provider._recall_token(),
                'markers': {p.name: p.read_text() for p in self.vault.iterdir() if p.name in
                    {'.index-refresh.json', '.reindex-request.json', '.mirror-delivery-failed.json'}},
                'cache': dict(self.provider._prefetch_cache)}

    def close(self):
        try:
            if self.provider:
                self.provider.shutdown()
                assert not self.provider._index_worker._thread.is_alive()
                assert not self.provider._disk_worker._thread.is_alive()
        finally:
            try:
                self.stop()
            finally:
                (self.tmp / 'commands.json').write_text(json.dumps(self.commands, indent=2))


def watcher_control(tmp_path, managed, safe_expected):
    before_inputs = bind_inputs()
    (tmp_path / 'input-bindings.json').write_text(json.dumps(before_inputs, indent=2))
    lane = Lane(tmp_path, managed)
    phases = {}
    try:
        assert subprocess.check_output(['/usr/bin/node', '--version'], text=True).strip() == 'v26.8.1'
        lane.start()
        lane.initialize()
        query = 'What safety gate does the synthetic observatory require?'
        cached = lane.provider.prefetch(query)
        assert 'copper canary' in cached and 'facts/control.md:' in cached
        phases['before'] = lane.state()
        time.sleep(2)
        phases['no_event'] = lane.state()
        assert phases['no_event']['native']['persistent']['workspace_index'] == phases['before']['native']['persistent']['workspace_index']
        assert phases['no_event']['generation'] == phases['before']['generation']
        lane.fact.write_text('Synthetic observatory safety requires a violet marzipan gate.\n')
        deadline = time.monotonic() + 12
        # Observe actual changed-path watcher completion on upstream; do not call its worker.
        while time.monotonic() < deadline:
            if not managed and lane.log.exists() and any(
                    r.get('event') == 'index.completed' and r.get('scope') == 'paths'
                    for r in map(json.loads, lane.log.read_text().splitlines())):
                break
            time.sleep(.2)
        phases['changed'] = lane.state()
        raw = lane.command(lane.provider._search_cmd('violet marzipan', 'fts', 5))
        phases['raw_changed_recall'] = raw
        phases['cached_after'] = lane.provider.prefetch(query)
        native_changed = phases['changed']['native']['persistent']['workspace_index'] != phases['before']['native']['persistent']['workspace_index']
        assert phases['changed']['generation'] == phases['before']['generation']
        assert phases['changed']['token'] == phases['before']['token']
        assert phases['changed']['markers'] == phases['before']['markers'] == {}
        assert phases['cached_after'] == cached
        phases['effect'] = {'native_changed': native_changed,
                            'new_indexed_line': '\tSynthetic observatory safety requires a violet marzipan gate.' in raw,
                            'old_plugin_cache': 'copper canary' in phases['cached_after']}
        assert native_changed is (not safe_expected), phases['effect']
        # FTS may select the changed path using live lexical matching while its
        # indexed preview remains old. A path hit is NOT a native mutation.
        assert phases['effect']['new_indexed_line'] is (not safe_expected), phases['effect']
        if managed:
            assert phases['changed']['inotify_fds'] == []
            assert phases['changed']['native']['runtime']['active_job_id'] == phases['before']['native']['runtime']['active_job_id']
            refusals = []
            for policy in ['wait', 'background']:
                rejected = subprocess.run([str(lane.launcher), 'query', '--mode', 'server', '--refresh', policy,
                    '--fts=violet marzipan'], cwd=lane.vault, env=lane.env,
                    capture_output=True, text=True, timeout=12)
                refusals.append({'policy': policy, 'rc': rejected.returncode, 'out': rejected.stdout, 'err': rejected.stderr})
                assert rejected.returncode != 0 and 'requires query refresh off' in rejected.stderr, refusals[-1]
            phases['policy_refusals'] = refusals
            assert lane.state()['native']['persistent']['workspace_index'] == phases['changed']['native']['persistent']['workspace_index']
            records = [json.loads(line) for line in lane.log.read_text().splitlines()]
            assert not any(r['event'].startswith('watcher.') for r in records)
            assert all(r.get('reason') == 'manual' for r in records if r['event'] == 'job.started')
        else:
            assert phases['before']['inotify_fds']
            records = [json.loads(line) for line in lane.log.read_text().splitlines()]
            assert any(r['event'] == 'watcher.changes' and r.get('touched_files', 0) > 0 for r in records)
            assert any(r['event'] == 'index.completed' and r['scope'] == 'paths' for r in records)
    finally:
        (tmp_path / 'phases.json').write_text(json.dumps(phases, indent=2))
        try:
            lane.close()
        finally:
            verify_bindings(tmp_path, before_inputs)


def test_upstream_watcher_bypass_witness(tmp_path):
    watcher_control(tmp_path, managed=False, safe_expected=False)


def test_managed_daemon_has_no_autonomous_watcher_mutation(tmp_path):
    watcher_control(tmp_path, managed=True, safe_expected=True)


def test_managed_server_refuses_unsupported_runtime_overrides(tmp_path):
    inputs = bind_inputs()
    (tmp_path / 'input-bindings.json').write_text(json.dumps(inputs, indent=2))
    lane = Lane(tmp_path, managed=True)
    try:
        stdout = (tmp_path / 'override.stdout').open('w')
        stderr = (tmp_path / 'override.stderr').open('w')
        try:
            lane.process = subprocess.Popen([str(lane.launcher), 'server', 'run', '--listen',
                lane.url.split('/')[2], '--token-file', str(lane.token), '--embedding', 'local/unsupported-model'],
                env=lane.env, cwd=lane.vault, stdin=subprocess.DEVNULL, stdout=stdout, stderr=stderr)
        finally:
            stdout.close()
            stderr.close()
        deadline = time.monotonic() + 4
        while lane.process.poll() is None and time.monotonic() < deadline:
            time.sleep(.1)
        assert lane.process.poll() == 1, 'unsupported flags silently accepted by managed server'
        assert 'unsupported managed server option' in (tmp_path / 'override.stderr').read_text().lower()
        assert not (lane.engine_home / 'daemon/instance.lock').exists()
    finally:
        try:
            lane.close()
        finally:
            verify_bindings(tmp_path, inputs)


def validate_citations(response, vault):
    sys.path.insert(0, str(ROOT / 'scripts'))
    from retrieval_evidence import cited_sections
    sections = list(cited_sections(response.removeprefix('## Zvec Memory\n')))
    assert sections, response
    for name, start, end, lines in sections:
        source = (vault / name).read_bytes().decode('utf8').split('\n')
        for number, text in lines:
            assert text == source[number - 1], (name, number, text)
    return sections


def test_managed_publication_gap_rejected_peer_deletion_and_cold_restart(tmp_path):
    inputs = bind_inputs()
    (tmp_path / 'input-bindings.json').write_text(json.dumps(inputs, indent=2))
    lane = Lane(tmp_path, managed=True)
    phases = {}
    try:
        lane.start()
        lane.initialize()
        p = lane.provider
        assert p is not None
        first = lane.state()
        phases['initial'] = first
        assert first['inotify_fds'] == []
        config = dict(p._config)
        injected = False
        native = p._run_zg
        def at_native_return(args, timeout):
            nonlocal injected
            result = native(args, timeout)
            if args[0] == 'index' and result[0] == 0 and not injected:
                injected = True
                # Actual native completion, before parent generation publication:
                # independent provider admits a source fact, rejects scheduling,
                # closes and exits. Never invoke the watcher/index worker here.
                receipt = tmp_path / 'peer-write.json'
                child = subprocess.run([sys.executable, '-I', '-B', str(ROOT / 'tests/managed_daemon_peer.py'),
                        'store-rejected', json.dumps(config), str(receipt)], env=lane.env,
                        capture_output=True, text=True, timeout=15)
                phases['peer_process'] = {'rc': child.returncode, 'out': child.stdout, 'err': child.stderr}
                assert child.returncode == 0, phases['peer_process']
                phases['before_publication'] = json.loads(receipt.read_text())
                assert p._index_generation() == first['generation']
                assert p._recall_token() is None
            return result
        p._run_zg = at_native_return
        stored = json.loads(p._handle_store({'content': 'The synthetic satellite requires a silver umbrella.'}))
        assert stored['status'] == 'stored', stored
        own_bytes = Path(stored['path']).read_bytes()
        p._maybe_reindex(force=True)
        assert p._index_worker.drain(65)
        assert injected
        phases['published'] = lane.state()
        assert phases['published']['generation'] != first['generation']
        assert phases['published']['markers'] == {}
        assert phases['published']['token'] is not None
        text = p.prefetch('What emergency equipment does the synthetic telescope need?')
        assert 'indigo parachute' in text, text
        phases['positive'] = {'response': text, 'citations': validate_citations(text, lane.vault)}
        peer_path = Path(phases['before_publication']['response']['path'])
        peer_bytes = peer_path.read_bytes()
        # Durable mirror deletion remains closed while native work is rejected.
        mirrored = 'Synthetic traveler prefers scarlet marmalade.'
        p.on_memory_write('add', 'user', mirrored)
        assert p._disk_worker.drain(15) and p._index_worker.drain(35)
        records = p._mirror_state()['records']
        mirror_path = next(record['path'] for record in records.values() if record['content'] == mirrored)
        assert mirrored in p.prefetch('What does the synthetic traveler prefer?')
        last = lane.state()
        submit = p._index_worker.submit
        p._index_worker.submit = lambda *args, **kwargs: False
        p.on_memory_write('remove', 'user', '', {'previous_content': mirrored})
        assert p._disk_worker.drain(15)
        assert not (lane.vault / mirror_path).exists()
        assert p._mirror_state()['refresh_required'] is True
        assert p._recall_token() is None
        assert p.prefetch('What does the synthetic traveler prefer?') == ''
        time.sleep(2)
        phases['pending_deletion'] = lane.state()
        assert phases['pending_deletion']['generation'] == last['generation']
        assert phases['pending_deletion']['native']['persistent']['workspace_index'] == last['native']['persistent']['workspace_index']
        assert phases['pending_deletion']['inotify_fds'] == []
        p._index_worker.submit = submit
        p._maybe_reindex(force=True)
        assert p._index_worker.drain(35)
        assert p._recall_token() is not None
        removed = json.loads(p._handle_search({'query': 'scarlet marmalade', 'mode': 'fts', 'globs': [mirror_path]}))
        assert 'error' not in removed and 'hits: 0' in removed['results'], removed
        phases['removed'] = removed
        assert p._mirror_inbox.pending() == 0
        assert not p._mirror_state()['pending_deletes'] and not p._mirror_state()['pending_creates']
        assert not p._mirror_state()['refresh_required']
        final_generation = p._index_generation()
        p.shutdown()
        lane.provider = None
        assert not p._index_worker._thread.is_alive() and not p._disk_worker._thread.is_alive()
        lane.stop()
        assert lane.fact.read_bytes() == lane.original
        assert Path(stored['path']).read_bytes() == own_bytes
        assert peer_path.read_bytes() == peer_bytes
        source_snapshot = {str(path.relative_to(lane.vault)): [hashlib.sha256(path.read_bytes()).hexdigest(), path.stat().st_mode]
                           for path in lane.vault.rglob('*.md')}
        phases['source_snapshot'] = source_snapshot
        lane.start()
        receipt = tmp_path / 'cold-read.json'
        cold = subprocess.run([sys.executable, '-I', '-B', str(ROOT / 'tests/managed_daemon_peer.py'),
                      'cold-read', json.dumps(config), str(receipt)], env=lane.env,
                      capture_output=True, text=True, timeout=40)
        phases['cold_process'] = {'rc': cold.returncode, 'out': cold.stdout, 'err': cold.stderr}
        assert cold.returncode == 0, phases['cold_process']
        phases['cold'] = json.loads(receipt.read_text())
        assert phases['cold']['generation'] == final_generation
        validate_citations(phases['cold']['response'], lane.vault)
        assert {str(path.relative_to(lane.vault)): [hashlib.sha256(path.read_bytes()).hexdigest(), path.stat().st_mode]
                for path in lane.vault.rglob('*.md')} == source_snapshot
        events = [json.loads(line) for line in lane.log.read_text().splitlines()]
        phases['job_reasons'] = [r['reason'] for r in events if r['event'] == 'job.started']
        assert len(phases['job_reasons']) >= 5
        assert set(phases['job_reasons']) == {'manual'}
        assert not any(r['event'].startswith('watcher.') for r in events)
        assert any(r['event'] == 'model.cache_hit' for r in events)
    finally:
        (tmp_path / 'lifecycle-phases.json').write_text(json.dumps(phases, indent=2))
        try:
            lane.close()
        finally:
            verify_bindings(tmp_path, inputs)
