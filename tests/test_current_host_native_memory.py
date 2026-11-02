"""Opt-in native lifecycle using current Hermes patch semantics and isolated data."""
import json
import os
from pathlib import Path
import shutil
import sys

import pytest
from source_support import PROVIDER_ROOT

sys.path.insert(0, os.environ.get('HERMES_AGENT_DIR', str(Path.home() / '.hermes/hermes-agent')))
pytestmark = [pytest.mark.integration, pytest.mark.skipif(
    os.environ.get('ZVEC_RUN_NATIVE') != '1', reason='requires opt-in local native engine')]


def test_native_current_host_store_patch_restart_and_citations(tmp_path, monkeypatch):
    home = tmp_path / 'isolated-home'
    home.mkdir()
    monkeypatch.setenv('HOME', str(home))
    monkeypatch.setenv('HERMES_HOME', str(home))
    monkeypatch.setenv('ZVEC_GREP_MODE', 'direct')
    cache = home / 'model-cache'
    shutil.copytree(os.environ['ZVEC_TEST_MODEL_CACHE'], cache)
    monkeypatch.setenv('ZVEC_GREP_MODEL_CACHE', str(cache))
    for key in ('ZVEC_GREP_API_KEY', 'ZVEC_GREP_ENDPOINT', 'ZVEC_GREP_HOME'):
        monkeypatch.delenv(key, raising=False)
    shutil.copytree(PROVIDER_ROOT / 'zvec-memory', home / 'plugins/zvec-memory')
    vault = home / 'zvec-memory'
    vault.mkdir()
    (vault / 'config.json').write_text(json.dumps({
        'zg_bin': os.environ['ZVEC_TEST_BIN'], 'reindex_min_seconds': 0,
        'embedding': 'local/potion-retrieval-32m', 'context_chars': 2000,
    }))
    from plugins.memory import load_memory_provider
    from agent.memory_manager import MemoryManager
    from tools.memory_tool import MemoryStore, memory_tool

    p = load_memory_provider('zvec-memory', register_skills=False)
    assert p is not None
    manager = MemoryManager()
    manager.add_provider(p)
    manager.initialize_all(session_id='synthetic-origin', hermes_home=str(home))
    store = MemoryStore()
    assert store._path_for('user').resolve().is_relative_to(home)

    def refresh(provider):
        assert provider._disk_worker.drain(60)
        provider._maybe_reindex(force=True)
        assert provider._index_worker.drain(120)
        assert not (vault / '.index-refresh.json').exists()

    def mirror(action, content='', old_text=''):
        args = {'target': 'user', 'action': action}
        if content:
            args['content'] = content
        if old_text:
            args['old_text'] = old_text
        result = json.loads(memory_tool(store=store, **args))
        assert result.get('success') is True, result
        manager.notify_memory_tool_write(result, args)
        assert manager.flush_pending(timeout=60)
        refresh(p)
        return result

    def search(owner, query):
        out = json.loads(owner.handle_tool_call('memory_search', {'query': query, 'mode': 'fts', 'limit': 5}))
        assert 'error' not in out, out
        return out['results']

    original = 'Synthetic user prefers scarlet marmalade.'
    revised = 'Synthetic user prefers violet marzipan.'
    fact = 'Synthetic lunar deployment requires a copper canary gate.'
    try:
        stored = json.loads(manager.handle_tool_call('memory_store', {'content': fact, 'category': 'project'}))
        assert stored['status'] == 'stored'
        fact_path = Path(stored['path']).relative_to(vault).as_posix()
        refresh(p)
        assert fact_path in search(manager, 'copper canary')
        mirror('add', original)
        assert 'scarlet marmalade' in search(manager, 'scarlet marmalade')
        patched = mirror('patch', revised, original)
        assert patched.get('patched_entry_after') == revised
        assert revised in search(manager, 'violet marzipan')
        assert original not in search(manager, 'scarlet marmalade')
    finally:
        manager.shutdown_all()

    fresh = load_memory_provider('zvec-memory', register_skills=False)
    assert fresh is not None
    fresh.initialize('synthetic-restart', hermes_home=str(home))
    try:
        assert revised in search(fresh, 'violet marzipan')
        assert fact_path in search(fresh, 'copper canary')
        assert store.user_entries == [revised]
    finally:
        fresh.shutdown()
