"""Offline live-target operation: synthetic data only, no service control."""
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest
from test_offline_mirror_reconcile import fixture
from test_provider import ZvecMemoryProvider
from zvec_memory_provider.live_reconcile import prepare, commit
from zvec_memory_provider.offline_reconcile import _open_provider


def setup_case(tmp_path, monkeypatch, isolated_home):
    profile, vault, _ = fixture(tmp_path, monkeypatch, isolated_home)
    (vault / 'facts/unrelated.md').write_bytes(b'unowned\n')
    (vault / '.hidden-unknown').write_bytes(b'private sentinel\x00')
    root = tmp_path / 'fresh-live-snapshot'
    proposal = prepare(profile, vault, root)
    return profile, vault, root, proposal


def test_commit_preserves_unknown_and_source(tmp_path, monkeypatch, isolated_home):
    profile, vault, root, proposal = setup_case(tmp_path, monkeypatch, isolated_home)
    source = (profile / 'memories/USER.md').read_bytes()
    old_map = (vault / '.mirror-map.json').read_bytes()
    old_root_inode = vault.stat().st_ino
    result = commit(root, proposal['plan_sha256'])
    assert result['status'] == 'refresh_required'
    assert vault.stat().st_ino == old_root_inode
    assert (profile / 'memories/USER.md').read_bytes() == source
    assert (vault / '.hidden-unknown').read_bytes() == b'private sentinel\x00'
    assert (vault / 'facts/unrelated.md').read_bytes() == b'unowned\n'
    assert (Path(result['archive']) / 'map.json').read_bytes() == old_map
    p = _open_provider(root, vault=vault)
    try:
        state = p._mirror_state()
        assert sorted(r['content'] for r in state['records'].values()) == ['First entry.', 'Second entry.']
        assert state['refresh_required'] and not state['pending_creates'] and not state['pending_deletes']
        assert p._recall_token() is None
    finally:
        p._mirror_inbox.close()
    with pytest.raises(ValueError):
        commit(root, proposal['plan_sha256'])


@pytest.mark.parametrize('mutation', ['source', 'unrelated', 'pending', 'wrong_plan'])
def test_commit_refuses_drift(tmp_path, monkeypatch, isolated_home, mutation):
    profile, vault, root, proposal = setup_case(tmp_path, monkeypatch, isolated_home)
    old_map = (vault / '.mirror-map.json').read_bytes()
    if mutation == 'source':
        (profile / 'memories/USER.md').write_text('Changed.\n')
    elif mutation == 'unrelated':
        (vault / '.hidden-unknown').write_bytes(b'changed')
    elif mutation == 'pending':
        p = _open_provider(root, vault=vault)
        p._mirror_inbox.append(['remove', 'user', '', {'previous_content': 'First entry.'}])
        p._mirror_inbox.close()
    expected = '0' * 64 if mutation == 'wrong_plan' else proposal['plan_sha256']
    with pytest.raises(ValueError):
        commit(root, expected)
    assert (vault / '.mirror-map.json').read_bytes() == old_map
    assert (vault / 'facts/unrelated.md').read_bytes() == b'unowned\n'
    assert not (vault / '.reconcile-archive').exists()


def test_prepare_refuses_pending(tmp_path, monkeypatch, isolated_home):
    profile, vault, _ = fixture(tmp_path, monkeypatch, isolated_home)
    p = _open_provider(tmp_path, vault=vault)
    p._mirror_inbox.append(['add', 'user', 'Pending', {}])
    p._mirror_inbox.close()
    original = (vault / '.mirror-map.json').read_bytes()
    with pytest.raises(ValueError, match='Pending notifications'):
        prepare(profile, vault, tmp_path / 'pending-snapshot')
    assert (vault / '.mirror-map.json').read_bytes() == original


def test_real_cli(tmp_path, monkeypatch, isolated_home):
    profile, vault, _ = fixture(tmp_path, monkeypatch, isolated_home)
    root = tmp_path / 'cli-live-snapshot'
    script = Path(__file__).resolve().parents[1] / 'scripts/live_mirror_reconcile.py'
    def run(*args):
        p = subprocess.run([sys.executable, '-B', str(script), *args], capture_output=True, text=True)
        assert p.returncode == 0, p.stderr
        return json.loads(p.stdout)
    proposal = run('prepare', '--profile', str(profile), '--vault', str(vault),
                   '--snapshot', str(root), '--offline-controller-active')
    result = run('commit', '--snapshot', str(root), '--confirm-plan-sha256',
                 proposal['plan_sha256'], '--offline-controller-active')
    assert result['status'] == 'refresh_required'
    p = _open_provider(root, vault=vault)
    try:
        assert len(p._mirror_state()['records']) == 2
    finally:
        p._mirror_inbox.close()


@pytest.mark.parametrize('boundary', ['staged', 'journal', 'link', 'retire'])
def test_hard_exit_and_real_journal_recovery(tmp_path, monkeypatch, isolated_home, boundary):
    profile, vault, root, proposal = setup_case(tmp_path, monkeypatch, isolated_home)
    old_map = (vault / '.mirror-map.json').read_bytes()
    code = r'''import importlib.util,sys,os
from pathlib import Path
sys.path.insert(0, os.environ['HERMES_AGENT_DIR'])
source=Path(sys.argv[1])/'zvec-memory'/'__init__.py'
s=importlib.util.spec_from_file_location('probe_provider',source,submodule_search_locations=[str(source.parent)])
m=importlib.util.module_from_spec(s);sys.modules[s.name]=m;s.loader.exec_module(m)
from probe_provider.live_reconcile import commit
P=m.ZvecMemoryProvider
phase=sys.argv[4]
if phase=='staged':
    def die(self,state): os._exit(71)
    P._save_mirror_state=die
elif phase=='journal':
    old=P._save_mirror_state
    def die(self,state): old(self,state);os._exit(72)
    P._save_mirror_state=die
elif phase=='retire':
    old=P._finish_mirror_deletes
    original=Path.unlink
    def unlink(self,*a,**kw):
        result=original(self,*a,**kw)
        if self.parent.name=='facts': os._exit(74)
        return result
    Path.unlink=unlink
else:
    def die(self,state):
        item=state['pending_creates'][0]
        os.link(self._mirror_staged_file(item['staged']),self._mirror_file(item['path']))
        self._sync_mirror_directory(self._vault/'facts')
        os._exit(73)
    P._finish_mirror_deletes=die
commit(sys.argv[2],sys.argv[3])
raise AssertionError('boundary not reached')
'''
    repo = Path(__file__).resolve().parents[1]
    result = subprocess.run([sys.executable, '-B', '-c', code, str(repo), str(root),
                             proposal['plan_sha256'], boundary], capture_output=True, text=True)
    assert result.returncode == {'staged': 71, 'journal': 72, 'link': 73, 'retire': 74}[boundary], result.stderr
    assert (vault / '.hidden-unknown').read_bytes() == b'private sentinel\x00'
    assert (vault / 'facts/unrelated.md').read_bytes() == b'unowned\n'
    assert (profile / 'memories/USER.md').read_text() == 'First entry.\n§\nSecond entry.\n'
    if boundary == 'staged':
        assert (vault / '.mirror-map.json').read_bytes() == old_map
        assert len(list((vault / '.mirror-staging').glob('*.md'))) == 2
        with pytest.raises(ValueError):
            commit(root, proposal['plan_sha256'])
        return
    p = _open_provider(root, vault=vault)
    try:
        p._recover_mirrors()
        state = p._mirror_state()
        assert state['refresh_required'] and not state['pending_creates'] and not state['pending_deletes']
        assert sorted(r['content'] for r in state['records'].values()) == ['First entry.', 'Second entry.']
        assert p._recall_token() is None
    finally:
        p._mirror_inbox.close()
