"""Parent reproductions of residual review findings; offline engine only."""
import json
import threading

import pytest

from test_cli import load_cli
from test_provider import ZvecMemoryProvider, make_provider, _mod
from test_runtime_review import ModelEngine, peers


def test_generic_cli_request_preserves_bound_model_obligation(tmp_path):
    from zvec_memory_provider.maintenance import request_rebuild, read_request, request_identity

    identity = {"plugin_schema": 1, "embedding": "local/new", "zg_bin": "zg"}
    request_rebuild(tmp_path, identity=identity)
    cli = load_cli()

    cli._request_reindex(tmp_path)

    assert request_identity(read_request(tmp_path)) == identity, "generic CLI erased model-bound intent"


@pytest.mark.parametrize("entry", ["prefetch", "search", "queued"])
def test_idle_old_engine_cannot_query_peer_rebuilt_index(tmp_path, monkeypatch, entry):
    old = make_provider(tmp_path, embedding="local/old")
    old._zg_version_cache = "fixture-old"
    old._record_engine_state()
    new = ZvecMemoryProvider({"vault": str(old._vault), "embedding": "local/old"})
    new._zg_version_cache = "fixture-new"
    engine = ModelEngine()
    old_calls = []

    def old_engine(args, timeout):
        old_calls.append(list(args))
        return engine(args, timeout)

    monkeypatch.setattr(old, "_run_zg", old_engine)
    monkeypatch.setattr(new, "_run_zg", engine)
    monkeypatch.setattr(old, "is_available", lambda: True)
    try:
        new.initialize("new-engine", hermes_home=str(tmp_path))
        assert new._index_worker.drain(5)
        assert not (old._vault / _mod.REINDEX_REQUEST_FILE).exists()
        assert old._engine_state()["zg_version"] == "fixture-new"
        # In particular, do not force an old-engine indexing job first.
        if entry == "prefetch":
            result = old.prefetch("which fact was stored?")
        elif entry == "search":
            result = json.loads(old._handle_search({"query": "which fact was stored?"}))
        else:
            old.queue_prefetch("which fact was stored?")
            assert old._disk_worker.drain(5)
            result = old._cached_prefetch("which fact was stored?")
        assert not [call for call in old_calls if call[0] == "query"], "idle stale engine served a query"
        if entry == "prefetch":
            assert result == ""
        elif entry == "search":
            assert "error" in result
        else:
            assert result is None
    finally:
        old.shutdown()
        new.shutdown()


@pytest.mark.parametrize("after_write", [False, True])
def test_plain_refresh_publication_failure_closes_idle_peer(peers, monkeypatch, after_write):
    p, q, engine = peers
    assert q._recall_token() is not None
    assert "local/old" in q.prefetch("which fact was stored?")
    assert not (p._vault / _mod.REINDEX_REQUEST_FILE).exists()
    assert not p._mirror_state().get("refresh_required")
    real = _mod.atomic_json_write
    publications = []

    def fault(path, value, **kwargs):
        if path == p._engine_state_path() and "index_generation" in value:
            publications.append(value["index_generation"])
            if after_write:
                real(path, value, **kwargs)
            raise OSError("plain index generation publication failed")
        return real(path, value, **kwargs)

    with monkeypatch.context() as m:
        m.setattr(_mod, "atomic_json_write", fault)
        p._maybe_reindex(force=True)
        assert p._index_worker.drain(5)
    assert publications and any(call[0] == "index" for call in engine.calls)
    assert p._index_requested and not p._index_running
    assert q._recall_token() is None, "plain refresh failure had no peer-visible recovery gate"


def test_legacy_adoption_cannot_overwrite_completed_peer_identity(tmp_path, monkeypatch):
    from zvec_memory_provider.maintenance import request_rebuild

    vault = tmp_path / "vault"
    (vault / ".zvec-grep").mkdir(parents=True)
    (vault / ".zvec-grep/manifest.json").write_text("{}")
    old = ZvecMemoryProvider({"vault": str(vault), "embedding": "local/old"})
    new = ZvecMemoryProvider({"vault": str(vault), "embedding": "local/new"})
    engine = ModelEngine()
    for handle in (old, new):
        monkeypatch.setattr(handle, "_run_zg", engine)
    observed, release = threading.Event(), threading.Event()
    real_state = old._engine_state
    errors = []

    def delayed_absence():
        state = real_state()
        if not state and not observed.is_set():
            observed.set()
            assert release.wait(5), "adoption probe was not released"
        return state

    def initialize_old():
        try:
            old.initialize("legacy", hermes_home=str(tmp_path))
        except BaseException as exc:
            errors.append(repr(exc))

    monkeypatch.setattr(old, "_engine_state", delayed_absence)
    thread = threading.Thread(target=initialize_old)
    peer_published = None
    try:
        thread.start()
        assert observed.wait(5)
        # A corrected adoption may already hold the vault transaction. Observe
        # that boundary instead of demanding a rebuild while holding its lock.
        acquired = old._vault_lock.acquire(blocking=False)
        if acquired:
            old._vault_lock.__exit__(None, None, None)
        request_rebuild(vault, identity={"plugin_schema": 1, "embedding": "local/new", "zg_bin": "zg"})
        new.initialize("peer", hermes_home=str(tmp_path))
        if acquired:
            assert new._disk_worker.drain(5) and new._index_worker.drain(5)
            peer_published = new._engine_state()
            assert peer_published["embedding"] == engine.model == "local/new"
        release.set()
        thread.join(5)
        assert not thread.is_alive() and not errors, errors
        assert new._disk_worker.drain(5) and new._index_worker.drain(5)
        assert old._disk_worker.drain(5) and old._index_worker.drain(5)
        final = new._engine_state()
        assert final["embedding"] == engine.model == "local/new", "legacy adoption relabeled a peer-built model"
        if peer_published is not None:
            assert final == peer_published, "legacy adoption replaced a completed peer generation"
    finally:
        release.set()
        thread.join(5)
        old.shutdown()
        new.shutdown()


def test_generic_cli_request_still_creates_work_when_no_bound_request(tmp_path):
    from zvec_memory_provider.maintenance import read_request

    marker = load_cli()._request_reindex(tmp_path)

    assert marker.exists() and read_request(tmp_path) is not None
