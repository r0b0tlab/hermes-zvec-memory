"""Reconciliation must parse source memory exactly like the real host store."""
import pytest

from test_offline_mirror_reconcile import fixture
from zvec_memory_provider.offline_reconcile import plan, apply, _open_provider
from agent.memory_manager import MemoryManager
from tools.memory_tool_store import MemoryStore


@pytest.mark.parametrize('newline,bom', [(b'\r\n', b''), (b'\r', b''), (b'\r\n', b'\xef\xbb\xbf')])
def test_newlines_match_host_entry_ownership_after_remove(tmp_path, monkeypatch, isolated_home, newline, bom):
    _, _, root = fixture(tmp_path, monkeypatch, isolated_home, whole=False)
    source = root / 'profile/memories/USER.md'
    raw = bom + b'First entry.' + newline + '§'.encode() + newline + b'Second entry.' + newline
    source.write_bytes(raw)
    monkeypatch.setenv('HERMES_HOME', str(root / 'profile'))
    store = MemoryStore()
    store.load_from_disk()
    assert store.user_entries == ['First entry.', 'Second entry.']
    proposal = plan(root)
    assert proposal['add'] == [['user', 'First entry.'], ['user', 'Second entry.']]
    result = apply(root, proposal['plan_sha256'])
    assert result['status'] == 'refresh_required'
    assert source.read_bytes() == raw
    provider = _open_provider(root)
    try:
        monkeypatch.setattr(provider, '_maybe_reindex', lambda *a, **k: None)
        monkeypatch.setattr(provider, '_run_in_background', lambda *a, **k: True)
        provider._automatic_writes = True
        manager = MemoryManager()
        manager.add_provider(provider)
        args = {'target': 'user', 'action': 'remove', 'old_text': 'First entry.'}
        response = store.remove('user', 'First entry.')
        assert response['success']
        manager.notify_memory_tool_write(response, args)
        assert provider._mirror_inbox.first()
        provider._drain_mirror_inbox()
        records = provider._mirror_state()['records'].values()
        assert sorted(r['content'] for r in records) == ['Second entry.']
        assert provider._mirror_inbox.first() is None
    finally:
        provider._mirror_inbox.close()
