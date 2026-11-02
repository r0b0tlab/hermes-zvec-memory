"""Detached snapshot reconciliation; no live profile mutation."""
import json
import hashlib
import os
from pathlib import Path
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
import pytest
from agent.memory_manager import MemoryManager
from test_provider import make_provider, ZvecMemoryProvider
from zvec_memory_provider.offline_reconcile import create_snapshot, plan, apply, _open_provider
from zvec_memory_provider import offline_reconcile


def fixture(tmp_path, monkeypatch, isolated_home, *, whole=True):
    profile = isolated_home
    memories = profile / 'memories'
    memories.mkdir(exist_ok=True)
    (memories / 'USER.md').write_text('First entry.\n§\nSecond entry.\n')
    p = make_provider(tmp_path)
    monkeypatch.setattr(p, '_maybe_reindex', lambda *a, **kw: None)
    if whole:
        p._apply_mirror('add', 'user', 'First entry.\n§\nSecond entry.', {})
        state = p._mirror_state()
        state['refresh_required'] = False  # fixture simulates an already indexed source
        p._save_mirror_state(state)
    vault = p._vault
    p.shutdown()
    root = tmp_path / 'snapshot'
    create_snapshot(profile, vault, root)
    return profile, vault, root


def test_decompose_and_real_manager_lifecycle(tmp_path, monkeypatch, isolated_home):
    from tools.memory_tool_store import MemoryStore
    profile, vault, root = fixture(tmp_path, monkeypatch, isolated_home)
    before_source = (profile / 'memories/USER.md').read_bytes()
    original_map = (vault / '.mirror-map.json').read_bytes()
    proposal = plan(root)
    assert proposal['add'] == [['user', 'First entry.'], ['user', 'Second entry.']]
    assert len(proposal['retire']) == 1
    result = apply(root, proposal['plan_sha256'])
    assert result['status'] == 'refresh_required'
    archive = Path(result['archive'])
    assert (archive / 'map.json').read_bytes() == original_map
    assert (archive / 'USER.md').read_bytes() == before_source
    assert b'First entry.\n\xc2\xa7\nSecond entry.' in (archive / 'fact-0.bin').read_bytes()
    assert (profile / 'memories/USER.md').read_bytes() == before_source
    assert (vault / '.mirror-map.json').read_bytes() == original_map
    p = _open_provider(root)
    monkeypatch.setattr(p, '_maybe_reindex', lambda *a, **k: None)
    monkeypatch.setattr(p, '_run_in_background', lambda *a, **k: True)
    p._automatic_writes = True
    p._mirror_ready = True
    assert p._recall_token() is None
    state = p._mirror_state()
    assert state['refresh_required'] and not state['pending_creates'] and not state['pending_deletes']
    assert sorted(r['content'] for r in state['records'].values()) == ['First entry.', 'Second entry.']
    assert len(list((root / 'vault/facts').glob('*.md'))) == 2
    monkeypatch.setenv('HERMES_HOME', str(root / 'profile'))
    store = MemoryStore()
    store.load_from_disk()
    manager = MemoryManager()
    manager.add_provider(p)
    operations = [
        ({'target': 'user', 'action': 'patch', 'old_text': 'First', 'new_text': 'Revised'}, lambda: store.patch('user', 'First', 'Revised')),
        ({'target': 'user', 'action': 'remove', 'old_text': 'Second'}, lambda: store.remove('user', 'Second')),
        ({'target': 'user', 'action': 'add', 'content': 'Third entry.'}, lambda: store.add('user', 'Third entry.')),
    ]
    for args, operation in operations:
        response = operation()
        assert response['success']
        manager.notify_memory_tool_write(response, args)
        item = p._mirror_inbox.first()
        assert item
        p._drain_mirror_inbox()
        state = p._mirror_state()
        p._apply_mirror(*item[1], notification_id=item[0])
        assert p._mirror_state() == state
        assert sorted(x['content'] for x in state['records'].values()) == sorted(store.user_entries)
    p._mirror_inbox.close()


def test_seed_stale_plan_and_idempotency(tmp_path, monkeypatch, isolated_home):
    _, _, root = fixture(tmp_path, monkeypatch, isolated_home, whole=False)
    proposal = plan(root)
    source = root / 'profile/memories/USER.md'
    source.write_text('Changed.\n')
    with pytest.raises(ValueError, match='Stale'):
        apply(root, proposal['plan_sha256'])
    assert not (root / 'vault/.mirror-map.json').exists()
    source.write_text('First entry.\n§\nSecond entry.\n')
    apply(root, plan(root)['plan_sha256'])
    with pytest.raises(ValueError, match='refresh'):
        plan(root)
    p = _open_provider(root)
    state = p._mirror_state()
    state['refresh_required'] = False  # simulates completed refresh for idempotency only
    p._save_mirror_state(state)
    p._mirror_inbox.close()
    proposal = plan(root)
    assert not proposal['add'] and not proposal['retire']
    assert apply(root, proposal['plan_sha256'])['status'] == 'unchanged'


def test_unowned_fact_drift_refuses_stale_plan(tmp_path, monkeypatch, isolated_home):
    _, _, root = fixture(tmp_path, monkeypatch, isolated_home)
    unrelated = root / 'vault/facts/unowned.md'
    unrelated.write_text('Unowned original.\n')
    proposal = plan(root)
    unrelated.write_text('Unowned changed.\n')
    before = (root / 'vault/.mirror-map.json').read_bytes()
    with pytest.raises(ValueError, match='Stale'):
        apply(root, proposal['plan_sha256'])
    assert unrelated.read_text() == 'Unowned changed.\n'
    assert (root / 'vault/.mirror-map.json').read_bytes() == before


def test_fault_before_map_commit_preserves_old_and_orphan(tmp_path, monkeypatch, isolated_home):
    _, _, root = fixture(tmp_path, monkeypatch, isolated_home)
    before = (root / 'vault/.mirror-map.json').read_bytes()
    proposal = plan(root)
    def fail(self, state):
        raise OSError('before journal')
    monkeypatch.setattr(ZvecMemoryProvider, '_save_mirror_state', fail)
    with pytest.raises(OSError, match='before journal'):
        apply(root, proposal['plan_sha256'])
    assert (root / 'vault/.mirror-map.json').read_bytes() == before
    assert len(list((root / 'vault/facts').glob('*.md'))) == 1
    assert len(list((root / 'vault/.mirror-staging').glob('*.md'))) == 2
    with pytest.raises(ValueError, match='staging'):
        plan(root)


@pytest.mark.parametrize('mutation', ['pending', 'marker', 'stale', 'duplicate', 'shared'])
def test_refuse_ambiguity_and_pending(tmp_path, monkeypatch, isolated_home, mutation):
    _, _, root = fixture(tmp_path, monkeypatch, isolated_home)
    p = _open_provider(root)
    if mutation == 'pending':
        p._mirror_inbox.append(['remove', 'user', '', {'previous_content': 'First entry.'}])
    elif mutation == 'marker':
        (root / 'vault/.mirror-delivery-failed.json').write_text('{}')
    elif mutation == 'stale':
        (root / 'profile/memories/USER.md').write_text('Other.\n')
    elif mutation == 'duplicate':
        (root / 'profile/memories/USER.md').write_text('First entry.\n§\nFirst entry.\n')
    else:
        state = p._mirror_state()
        state['records']['duplicate'] = dict(next(iter(state['records'].values())))
        p._save_mirror_state(state)
    before = (root / 'vault/.mirror-map.json').read_bytes()
    p._mirror_inbox.close()
    with pytest.raises(ValueError):
        plan(root)
    assert (root / 'vault/.mirror-map.json').read_bytes() == before


@pytest.mark.parametrize('phase', ['after_commit', 'during_publish'])
def test_restart_recovery_after_publication_fault(tmp_path, monkeypatch, isolated_home, phase):
    _, _, root = fixture(tmp_path, monkeypatch, isolated_home)
    proposal = plan(root)
    real_save = ZvecMemoryProvider._save_mirror_state
    real_finish = ZvecMemoryProvider._finish_mirror_deletes
    if phase == 'after_commit':
        def interrupted(self, state):
            real_save(self, state)
            raise OSError('injected after durable journal')
        monkeypatch.setattr(ZvecMemoryProvider, '_save_mirror_state', interrupted)
    else:
        def interrupted(self, state):
            # Publication of one create is durable, but no journal progress yet.
            item = state['pending_creates'][0]
            staged = self._mirror_staged_file(item['staged'])
            path = self._mirror_file(item['path'])
            import os
            os.link(staged, path)
            self._sync_mirror_directory(path.parent)
            raise OSError('injected after link')
        monkeypatch.setattr(ZvecMemoryProvider, '_finish_mirror_deletes', interrupted)
    with pytest.raises(OSError, match='injected'):
        apply(root, proposal['plan_sha256'])
    monkeypatch.setattr(ZvecMemoryProvider, '_save_mirror_state', real_save)
    monkeypatch.setattr(ZvecMemoryProvider, '_finish_mirror_deletes', real_finish)
    p = _open_provider(root)
    p._recover_mirrors()
    state = p._mirror_state()
    assert state['refresh_required']
    assert not state['pending_creates'] and not state['pending_deletes']
    assert sorted(r['content'] for r in state['records'].values()) == ['First entry.', 'Second entry.']
    assert len(list((root / 'vault/facts').glob('*.md'))) == 2
    assert p._recall_token() is None
    p._mirror_inbox.close()


def test_process_restart_after_journal_commit(tmp_path, monkeypatch, isolated_home):
    _, _, root = fixture(tmp_path, monkeypatch, isolated_home)
    proposal = plan(root)
    real_finish = ZvecMemoryProvider._finish_mirror_deletes
    def fail(self, state):
        raise OSError('crash boundary')
    monkeypatch.setattr(ZvecMemoryProvider, '_finish_mirror_deletes', fail)
    with pytest.raises(OSError, match='crash boundary'):
        apply(root, proposal['plan_sha256'])
    monkeypatch.setattr(ZvecMemoryProvider, '_finish_mirror_deletes', real_finish)
    repo = Path(__file__).resolve().parents[1]
    code = '''import importlib.util,sys
from pathlib import Path
sys.path.insert(0, sys.argv[3])
source=Path(sys.argv[1])/"zvec-memory"/"__init__.py"
s=importlib.util.spec_from_file_location("recovered_provider",source,submodule_search_locations=[str(source.parent)])
m=importlib.util.module_from_spec(s);sys.modules[s.name]=m;s.loader.exec_module(m)
from recovered_provider.offline_reconcile import _open_provider
p=_open_provider(Path(sys.argv[2]));p._recover_mirrors();state=p._mirror_state()
assert state["refresh_required"] and not state["pending_deletes"] and not state["pending_creates"]
assert sorted(r["content"] for r in state["records"].values())==["First entry.","Second entry."]
assert p._recall_token() is None
p._mirror_inbox.close()
'''
    proc = subprocess.run([sys.executable, '-B', '-c', code, str(repo), str(root),
                           os.environ['HERMES_AGENT_DIR']], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr


def test_two_contenders_only_one_commits(tmp_path, monkeypatch, isolated_home):
    _, _, root = fixture(tmp_path, monkeypatch, isolated_home)
    proposal = plan(root)
    def run():
        try:
            return apply(root, proposal['plan_sha256'])['status']
        except ValueError:
            return 'refused'
    with ThreadPoolExecutor(max_workers=2) as pool:
        result = list(pool.map(lambda _: run(), range(2)))
    assert sorted(result) == ['refresh_required', 'refused']
    p = _open_provider(root)
    state = p._mirror_state()
    assert len(state['records']) == 2 and state['refresh_required']
    p._mirror_inbox.close()


def test_snapshot_wal_and_unowned_bytes(tmp_path, monkeypatch, isolated_home):
    p = make_provider(tmp_path)
    monkeypatch.setattr(p, '_maybe_reindex', lambda *a, **k: None)
    p._mirror_inbox.append(['add', 'user', 'Pending.', {}])
    explicit = p._vault / 'facts' / 'unowned.md'
    explicit.write_bytes(b'private unowned content\n')
    (isolated_home / 'memories').mkdir(exist_ok=True)
    root = tmp_path / 'snapshot'
    database = (p._vault / '.mirror-inbox.sqlite3').read_bytes()
    wal = (p._vault / '.mirror-inbox.sqlite3-wal').read_bytes()
    create_snapshot(isolated_home, p._vault, root)
    assert (root / '.original-inbox/database').read_bytes() == database
    assert (root / '.original-inbox/wal').read_bytes() == wal
    assert (root / 'vault/facts/unowned.md').read_bytes() == explicit.read_bytes()
    with pytest.raises(ValueError, match='Pending notifications'):
        plan(root)
    assert p._mirror_inbox.pending()
    p.shutdown()


def test_real_offline_cli(tmp_path, monkeypatch, isolated_home):
    profile, vault, _ = fixture(tmp_path, monkeypatch, isolated_home)
    root = tmp_path / 'cli-snapshot'
    script = Path(__file__).resolve().parents[1] / 'scripts/offline_mirror_reconcile.py'
    def invoke(*args):
        result = subprocess.run([sys.executable, '-B', str(script), *args],
                                capture_output=True, text=True)
        assert result.returncode == 0, result.stderr
        return json.loads(result.stdout)
    invoke('snapshot', '--profile', str(profile), '--vault', str(vault),
           '--destination', str(root), '--writers-stopped')
    proposal = invoke('plan', '--snapshot', str(root))
    assert len(proposal['add']) == 2 and len(proposal['retire']) == 1
    result = invoke('apply', '--snapshot', str(root), '--confirm-plan-sha256',
                    proposal['plan_sha256'], '--writers-stopped')
    assert result['status'] == 'refresh_required'
    assert (Path(result['archive']) / 'map.json').read_bytes() == (vault / '.mirror-map.json').read_bytes()


@pytest.mark.parametrize('mutation', ['extra_body', 'category', 'tags', 'header', 'timestamp'])
def test_refuse_noncanonical_owned_fact(tmp_path, monkeypatch, isolated_home, mutation):
    _, _, root = fixture(tmp_path, monkeypatch, isolated_home)
    p = _open_provider(root)
    record = next(iter(p._mirror_state()['records'].values()))
    p._mirror_inbox.close()
    fact = root / 'vault' / record['path']
    original = fact.read_bytes()
    if mutation == 'extra_body':
        # Preserve the entire expected suffix, including the original content.
        altered = original.replace(b'\n\nFirst entry.\n', b'\n\nUnrelated fact.\nFirst entry.\n')
    elif mutation == 'category':
        altered = original.replace(b'category: user_pref', b'category: general')
    elif mutation == 'tags':
        altered = original.replace(b'tags: mirror', b'tags: explicit')
    elif mutation == 'header':
        altered = b'unrelated\n' + original
    else:
        altered = original.replace(b'stored: ', b'stored: invalid')
    assert altered != original and altered.endswith((record['content'] + '\n').encode())
    fact.write_bytes(altered)
    with pytest.raises(ValueError, match='Owned fact does not match'):
        plan(root)
    assert fact.read_bytes() == altered


def test_whitespace_owned_record_replaced_then_real_manager_remove(tmp_path, monkeypatch, isolated_home):
    from tools.memory_tool_store import MemoryStore
    _, _, root = fixture(tmp_path, monkeypatch, isolated_home, whole=False)
    (root / 'profile/memories/USER.md').write_text('First entry.\n')
    p = _open_provider(root)
    monkeypatch.setattr(p, '_maybe_reindex', lambda *a, **k: None)
    p._apply_mirror('add', 'user', '  First entry.  ', {})
    state = p._mirror_state()
    old_path = root / 'vault' / next(iter(state['records'].values()))['path']
    state['refresh_required'] = False
    p._save_mirror_state(state)
    p._mirror_inbox.close()
    proposal = plan(root)
    assert proposal['add'] == [['user', 'First entry.']] and len(proposal['retire']) == 1
    apply(root, proposal['plan_sha256'])
    assert not old_path.exists()
    p = _open_provider(root)
    monkeypatch.setattr(p, '_maybe_reindex', lambda *a, **k: None)
    monkeypatch.setattr(p, '_run_in_background', lambda *a, **k: True)
    p._automatic_writes = p._mirror_ready = True
    monkeypatch.setenv('HERMES_HOME', str(root / 'profile'))
    store = MemoryStore()
    store.load_from_disk()
    manager = MemoryManager()
    manager.add_provider(p)
    response = store.remove('user', 'First')
    assert response['success']
    manager.notify_memory_tool_write(response, {'target': 'user', 'action': 'remove', 'old_text': 'First'})
    assert p._mirror_inbox.first()
    p._drain_mirror_inbox()
    assert not p._mirror_state()['records']
    assert not list((root / 'vault/facts').glob('*.md'))
    p._mirror_inbox.close()


def test_snapshot_large_index_and_unknown_files(tmp_path, isolated_home):
    vault = tmp_path / 'source-vault'
    index = vault / '.zvec-grep'
    index.mkdir(parents=True)
    (isolated_home / 'memories').mkdir(exist_ok=True)
    for i in range(15263):
        (index / f'{i:05d}').write_bytes(str(i).encode())
    large = index / 'large-segment'
    with large.open('wb') as f:
        f.write(b'index-start')
        f.seek(110 * 1024 * 1024 - 1)
        f.write(b'X')
    (vault / 'unknown.bin').write_bytes(b'preserve unknown\x00')
    root = tmp_path / 'large-snapshot'
    marker = create_snapshot(isolated_home, vault, root)
    inventory_bytes = (root / '.snapshot-inventory.json').read_bytes()
    assert hashlib.sha256(inventory_bytes).hexdigest() == marker['inventory_sha256']
    inventory = json.loads(inventory_bytes)
    assert len(inventory) == 15265
    assert (root / 'vault/unknown.bin').read_bytes() == b'preserve unknown\x00'
    with (root / 'vault/.zvec-grep/large-segment').open('rb') as f:
        digest = hashlib.file_digest(f, 'sha256').hexdigest()
    assert digest == inventory['.zvec-grep/large-segment']['sha256']
    assert inventory['.zvec-grep/large-segment']['size'] == 110 * 1024 * 1024
    assert len(list((root / 'vault/.zvec-grep').iterdir())) == 15264


@pytest.mark.parametrize('limit', ['bytes', 'file', 'entries', 'receipt'])
def test_snapshot_budget_refuses_without_marker(tmp_path, monkeypatch, isolated_home, limit):
    vault = tmp_path / 'source-vault'
    vault.mkdir()
    (vault / 'unknown').write_bytes(b'12345')
    (isolated_home / 'memories').mkdir(exist_ok=True)
    name = {'bytes': 'SNAPSHOT_MAX_BYTES', 'file': 'SNAPSHOT_MAX_FILE_BYTES',
            'entries': 'SNAPSHOT_MAX_ENTRIES', 'receipt': 'LIMIT'}[limit]
    monkeypatch.setattr(offline_reconcile, name, 0 if limit == 'entries' else 4)
    root = tmp_path / 'refused-snapshot'
    with pytest.raises(ValueError, match='limit'):
        create_snapshot(isolated_home, vault, root)
    assert not (root / offline_reconcile.MARKER).exists()
    assert (vault / 'unknown').read_bytes() == b'12345'


@pytest.mark.parametrize('kind', ['symlink', 'hardlink', 'transient-symlink'])
def test_snapshot_rejects_unsafe_entries(tmp_path, isolated_home, kind):
    vault = tmp_path / 'source-vault'
    vault.mkdir()
    (isolated_home / 'memories').mkdir(exist_ok=True)
    outside = tmp_path / 'outside'
    outside.write_bytes(b'preserve')
    entry = vault / ('.mirror.lock' if kind == 'transient-symlink' else 'unknown')
    if kind == 'hardlink':
        os.link(outside, entry)
    else:
        entry.symlink_to(outside)
    root = tmp_path / 'refused-snapshot'
    with pytest.raises((ValueError, OSError)):
        create_snapshot(isolated_home, vault, root)
    assert not (root / offline_reconcile.MARKER).exists()
    assert outside.read_bytes() == b'preserve'


def test_snapshot_receipt_tampering_refuses_plan(tmp_path, monkeypatch, isolated_home):
    _, _, root = fixture(tmp_path, monkeypatch, isolated_home)
    (root / '.snapshot-inventory.json').write_bytes(b'{}')
    with pytest.raises(ValueError, match='inventory receipt changed'):
        plan(root)


def test_snapshot_refuses_source_drift_during_copy(tmp_path, monkeypatch, isolated_home):
    vault = tmp_path / 'source-vault'
    vault.mkdir()
    (isolated_home / 'memories').mkdir(exist_ok=True)
    source = vault / 'unknown'
    source.write_bytes(b'original')
    source_inode = source.stat().st_ino
    real_read = os.read
    changed = False
    def read_with_drift(fd, count):
        nonlocal changed
        data = real_read(fd, count)
        if not changed and os.fstat(fd).st_ino == source_inode:
            source.write_bytes(b'different')
            changed = True
        return data
    monkeypatch.setattr(offline_reconcile.os, 'read', read_with_drift)
    root = tmp_path / 'refused-snapshot'
    with pytest.raises(ValueError, match='source changed'):
        create_snapshot(isolated_home, vault, root)
    assert changed and not (root / offline_reconcile.MARKER).exists()
