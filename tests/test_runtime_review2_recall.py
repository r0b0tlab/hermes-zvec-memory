"""Idle peer recall checks compatibility without inline native validation."""
import json
import threading

import pytest

from test_provider import ZvecMemoryProvider, make_provider, _mod
from test_runtime_review import ModelEngine, peers


@pytest.mark.parametrize("entry", ["prefetch", "search", "queued"])
@pytest.mark.parametrize("mismatch", ["version", "binary", "schema"])
def test_idle_peer_refuses_known_incompatible_identity(tmp_path, monkeypatch, entry, mismatch):
    old = make_provider(tmp_path, embedding="local/old")
    old._zg_version_cache = "fixture-old"
    old._record_engine_state()
    engine = ModelEngine()
    old_calls = []
    monkeypatch.setattr(old, "_run_zg", lambda args, timeout: (old_calls.append(list(args)) or engine(args, timeout)))
    monkeypatch.setattr(old, "is_available", lambda: True)
    new = ZvecMemoryProvider({"vault": str(old._vault), "embedding": "local/old",
                              "zg_bin": "other-zg" if mismatch == "binary" else "zg"})
    new._zg_version_cache = "fixture-new" if mismatch == "version" else "fixture-old"
    monkeypatch.setattr(new, "_run_zg", engine)
    try:
        new.initialize("peer", hermes_home=str(tmp_path))
        if mismatch == "schema":
            # A future plugin schema is known incompatible; no fake native
            # version claims are needed for this bookkeeping-only subcase.
            new._record_engine_state({**new._engine_identity(), "plugin_schema": 2})
        assert new._index_worker.drain(5)
        assert not (old._vault / _mod.REINDEX_REQUEST_FILE).exists()
        before = old._engine_state_path().read_bytes()
        if entry == "prefetch":
            assert old.prefetch("which fact was stored?") == ""
        elif entry == "search":
            assert "error" in json.loads(old._handle_search({"query": "which fact was stored?"}))
        else:
            old.queue_prefetch("which fact was stored?")
            assert old._disk_worker.drain(5)
            assert old._cached_prefetch("which fact was stored?") is None
        assert old._index_worker.drain(5)
        assert not [call for call in old_calls if call[0] == "query"]
        assert old._recall_token() is None
        assert old._engine_state_path().read_bytes() == before
    finally:
        old.shutdown()
        new.shutdown()


def test_queued_query_rechecks_compatibility_before_native_dispatch(peers, monkeypatch):
    p, q, engine = peers
    q._zg_version_cache = "fixture-old"
    q._record_engine_state()
    entered, release = threading.Event(), threading.Event()
    calls = []
    monkeypatch.setattr(q, "_run_zg", lambda args, timeout: (calls.append(list(args)) or engine(args, timeout)))
    def block():
        entered.set()
        assert release.wait(5)
    try:
        assert q._disk_worker.submit(block) and entered.wait(5)
        q.queue_prefetch("which fact was stored?")
        p._zg_version_cache = "fixture-new"
        p._ensure_engine_identity()
        assert p._index_worker.drain(5)
        release.set()
        assert q._disk_worker.drain(5)
        assert not [c for c in calls if c[0] == "query"], "queued incompatible query was dispatched"
        assert q._cached_prefetch("which fact was stored?") is None
    finally:
        release.set()
        assert q._disk_worker.drain(5)


def test_unprobed_version_validation_is_deferred_and_coalesced(peers, monkeypatch):
    p, q, engine = peers
    p._zg_version_cache = "fixture-new"
    p._record_engine_state()
    q._zg_version_cache = None
    entered, release = threading.Event(), threading.Event()
    caller = threading.get_ident()
    calls = []
    def validate(args, timeout):
        calls.append((threading.get_ident(), list(args)))
        if args == ["--version"]:
            entered.set()
            assert release.wait(5)
            return 0, "fixture-old", ""
        return engine(args, timeout)
    monkeypatch.setattr(q, "_run_zg", validate)
    try:
        for _ in range(10):
            assert q.prefetch("which fact was stored?") == ""
            q.queue_prefetch("which fact was stored?")
        assert entered.wait(5), "unprobed identity did not schedule worker validation"
        assert not [c for ident, c in calls if ident == caller], "recall ran native version detection inline"
        release.set()
        assert q._index_worker.drain(5)
        assert [c for _, c in calls] == [["--version"]]
        assert q._recall_token() is None
    finally:
        release.set()
        assert q._index_worker.drain(5)


@pytest.mark.parametrize("unknown", ["version", "legacy"])
def test_unknown_identity_is_not_known_incompatibility(peers, unknown):
    p, q, engine = peers
    if unknown == "legacy":
        p._engine_state_path().unlink()
    else:
        p._zg_version_cache = "fixture-new"
        p._record_engine_state()
        q._zg_version_cache = ""  # An attempted, unavailable probe, not unprobed.
    assert "local/old" in q.prefetch("which fact was stored?")
    assert not q._index_running and not q._index_requested


def test_same_engine_stale_embedding_can_query_peer_model(tmp_path, monkeypatch):
    old = make_provider(tmp_path, embedding="local/old")
    old._zg_version_cache = "fixture-1"
    old._record_engine_state()
    engine = ModelEngine()
    new = ZvecMemoryProvider({"vault": str(old._vault), "embedding": "local/new"})
    for p in (old, new):
        monkeypatch.setattr(p, "_run_zg", engine)
        monkeypatch.setattr(p, "is_available", lambda: True)
    try:
        new.initialize("new-model", hermes_home=str(tmp_path))
        assert new._index_worker.drain(5)
        before = old._engine_state_path().read_bytes()
        assert "local/new" in old.prefetch("which fact was stored?")
        assert not old._index_requested and not old._index_running
        assert old._engine_state_path().read_bytes() == before
    finally:
        old.shutdown()
        new.shutdown()
