"""Whole-file ownership guard matches host newline semantics without consuming inbox."""
import pytest
from test_provider import make_provider
from agent.memory_manager import MemoryManager
from tools.memory_tool_store import MemoryStore


@pytest.mark.parametrize('left,right,bom', [('\r\n', '\r\n', ''), ('\r', '\r', ''), ('\r\n', '\r', ''), ('\n', '\r\n', ''), ('\n', '\n', '\ufeff'), ('\r\n', '\r\n', '\ufeff')])
@pytest.mark.parametrize('action', ['remove', 'patch'])
def test_legacy_whole_file_with_non_lf_newlines_stays_pending(tmp_path, monkeypatch, isolated_home, left, right, bom, action):
    source = isolated_home / 'memories' / 'USER.md'
    source.parent.mkdir(parents=True, exist_ok=True)
    whole = bom + 'First entry.' + left + '§' + right + 'Second entry.'
    source.write_bytes(whole.encode('utf-8'))
    store = MemoryStore()
    store.load_from_disk()
    assert store.user_entries == ['First entry.', 'Second entry.']
    provider = make_provider(tmp_path)
    monkeypatch.setattr(provider, '_maybe_reindex', lambda *a, **k: None)
    monkeypatch.setattr(provider, '_run_in_background', lambda *a, **k: True)
    try:
        provider._apply_mirror('add', 'user', whole, {})
        before = (provider._vault / '.mirror-map.json').read_bytes()
        manager = MemoryManager()
        manager.add_provider(provider)
        if action == 'remove':
            args = {'target': 'user', 'action': 'remove', 'old_text': 'First entry.'}
            result = store.remove('user', 'First entry.')
        else:
            args = {'target': 'user', 'action': 'patch', 'old_text': 'First', 'new_text': 'Changed'}
            result = store.patch('user', 'First', 'Changed')
        assert result['success']
        manager.notify_memory_tool_write(result, args)
        pending = provider._mirror_inbox.first()
        assert pending is not None
        with pytest.raises(ValueError, match='whole-file'):
            provider._drain_mirror_inbox()
        assert provider._mirror_inbox.first() == pending
        assert (provider._vault / '.mirror-map.json').read_bytes() == before
        assert provider._recall_token() is None
    finally:
        provider.shutdown()


@pytest.mark.parametrize('left,right,bom', [('\r\n', '\r\n', ''), ('\r', '\r', ''), ('\r\n', '\r', ''), ('\n', '\n', '\ufeff'), ('\r\n', '\r\n', '\ufeff')])
def test_detached_reconcile_retires_legacy_non_lf_whole_record(tmp_path, monkeypatch, isolated_home, left, right, bom):
    from test_offline_mirror_reconcile import fixture
    from zvec_memory_provider.offline_reconcile import plan, apply, _open_provider
    _, _, root = fixture(tmp_path, monkeypatch, isolated_home, whole=False)
    whole = bom + 'First entry.' + left + '§' + right + 'Second entry.'
    source = root / 'profile/memories/USER.md'
    source.write_bytes(whole.encode('utf-8'))
    p = _open_provider(root)
    monkeypatch.setattr(p, '_maybe_reindex', lambda *a, **kw: None)
    try:
        p._apply_mirror('add', 'user', whole, {})
        state = p._mirror_state()
        state['refresh_required'] = False  # isolated fixture: simulate earlier completed index
        p._save_mirror_state(state)
    finally:
        p._mirror_inbox.close()
    original = source.read_bytes()
    proposal = plan(root)
    assert len(proposal['retire']) == 1
    assert proposal['add'] == [['user', 'First entry.'], ['user', 'Second entry.']]
    result = apply(root, proposal['plan_sha256'])
    assert result['status'] == 'refresh_required'
    assert source.read_bytes() == original
    p = _open_provider(root)
    try:
        assert sorted(r['content'] for r in p._mirror_state()['records'].values()) == ['First entry.', 'Second entry.']
    finally:
        p._mirror_inbox.close()
