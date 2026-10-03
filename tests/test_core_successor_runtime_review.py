# SOURCE-ONLY DRAFT: NOT_EXECUTED. Parent materializes/runs under original pt.sh shared lock/resource caps.
"""Independent-review regressions: command-aware offline engine, no native proof."""
import json
import threading

import pytest

from test_provider import ZvecMemoryProvider, make_provider, _mod


class ModelEngine:
    """Existing indexes retain their model unless explicitly rebuilt."""
    def __init__(self, model="local/potion-retrieval-32m"):
        self.model = model
        self.calls = []

    def __call__(self, args, timeout):
        self.calls.append(list(args))
        if args == ["--version"]:
            return 0, "fixture-1", ""
        if args[0] == "index":
            if "--rebuild" in args and "--embedding" in args:
                self.model = args[args.index("--embedding") + 1]
            return 0, "", ""
        assert args[0] == "query"
        return 0, "indexed with " + self.model, ""






@pytest.mark.parametrize("identity", [None, {}, {"plugin_schema": 1, "embedding": "local/potion-retrieval-32m", "zg_bin": "/untrusted/marker-command"}])
def test_l1_marker_never_selects_executable_or_malformed_identity(tmp_path, monkeypatch, identity):
    p = make_provider(tmp_path, embedding="local/potion-retrieval-32m")
    engine = ModelEngine()
    monkeypatch.setattr(p, "_run_zg", engine)
    marker = p._vault / _mod.REINDEX_REQUEST_FILE
    marker.write_text(json.dumps({"identity": identity}))
    before = marker.read_bytes()
    try:
        p._maybe_reindex(force=True)
        assert p._index_worker.drain(5)
        assert not engine.calls
        assert marker.read_bytes() == before
        assert p._zg() == "zg" and p._recall_token() is None
    finally:
        p.shutdown()










def rebuild(p):
    from zvec_memory_provider.maintenance import request_rebuild
    request_rebuild(p._vault)
    p._maybe_reindex(force=True)
    assert p._index_worker.drain(5)
    assert not (p._vault / _mod.REINDEX_REQUEST_FILE).exists()


@pytest.fixture
def peers(tmp_path, monkeypatch):
    p = make_provider(tmp_path, embedding="local/potion-retrieval-32m")
    q = make_provider(tmp_path, embedding="local/potion-retrieval-32m")
    engine = ModelEngine()
    for provider in (p, q):
        monkeypatch.setattr(provider, "_run_zg", engine)
        monkeypatch.setattr(provider, "is_available", lambda: True)
    try:
        yield p, q, engine
    finally:
        p.shutdown()
        q.shutdown()


def test_l3_completed_rebuild_invalidates_peer_cached_prefetch(peers, monkeypatch):
    p, q, engine = peers
    state = p._mirror_state()
    p._save_mirror_state(state)
    before = (p._vault / ".mirror-map.json").read_bytes()
    assert "local/potion-retrieval-32m" in q.prefetch("what is the stored fact?")
    rebuild(p)
    assert (p._vault / ".mirror-map.json").read_bytes() == before
    monkeypatch.setattr(q, "_run_zg", lambda args, timeout: (0, "fresh rebuild result", ""))
    assert "fresh rebuild result" in q.prefetch("what is the stored fact?")


@pytest.mark.parametrize("entry", ["prefetch", "search", "queued"])
def test_l3_completed_rebuild_fences_inflight_result(peers, monkeypatch, entry):
    p, q, engine = peers
    started, release = threading.Event(), threading.Event()
    result = []
    def query(args, timeout):
        assert args[0] == "query"
        started.set()
        assert release.wait(5)
        return 0, "old in-flight result", ""
    monkeypatch.setattr(q, "_run_zg", query)
    def run():
        if entry == "search":
            result.append(json.loads(q._handle_search({"query": "the stored fact"})))
        else:
            result.append(q.prefetch("the stored fact"))
    thread = None
    try:
        if entry == "queued":
            q.queue_prefetch("the stored fact")
        else:
            thread = threading.Thread(target=run)
            thread.start()
        assert started.wait(5)
        before = p._mirror_state()
        rebuild(p)
        assert p._mirror_state() == before
        release.set()
        if thread:
            thread.join(5)
            assert not thread.is_alive()
        else:
            assert q._disk_worker.drain(5)
        if entry == "search":
            assert result and "error" in result[0], "pre-rebuild search result escaped token fencing"
        elif entry == "prefetch":
            assert result == [""]
        else:
            assert q._cached_prefetch("the stored fact") is None
    finally:
        release.set()
        if thread:
            thread.join(5)


@pytest.mark.parametrize("after_write", [False, True])
def test_l3_generation_publication_failure_cannot_acknowledge(peers, monkeypatch, after_write):
    from zvec_memory_provider.maintenance import request_rebuild
    p, q, engine = peers
    marker = request_rebuild(p._vault)
    before = marker.read_bytes()
    real = _mod.atomic_json_write
    publications = []
    def fault(path, value, **kwargs):
        if path == p._engine_state_path() and "index_generation" in value:
            publications.append(value["index_generation"])
            if after_write:
                real(path, value, **kwargs)
            raise OSError("generation publication failed")
        return real(path, value, **kwargs)
    with monkeypatch.context() as m:
        m.setattr(_mod, "atomic_json_write", fault)
        p._maybe_reindex(force=True)
        assert p._index_worker.drain(5)
    assert publications, "successful rebuild published no durable index generation"
    assert marker.read_bytes() == before
    assert p._index_requested and not p._index_running
    assert p._recall_token() is None and q._recall_token() is None
    p._maybe_reindex(force=True)
    assert p._index_worker.drain(5)
    assert not marker.exists()
    assert p._recall_token() is not None and q._recall_token() is not None


@pytest.mark.parametrize("history", [0, 1])
def test_l4_live_handles_reject_impossible_history_without_vault_wait(peers, history):
    p, q, engine = peers
    assert p._recall_token() is not None and q._recall_token() is not None
    if history:
        p._mirror_inbox.append(["add", "user", "already acknowledged", {}])
        p._mirror_inbox.acknowledge(p._mirror_inbox.first()[0])
    state = p._mirror_state()
    state["last_notification"] = p._mirror_inbox.high_watermark() + 1
    p._save_mirror_state(state)
    before = (p._vault / ".mirror-map.json").read_bytes()
    result = []
    def recall():
        for handle in (p, q):
            result.append((handle.prefetch("what is the stored fact?"),
                           json.loads(handle._handle_search({"query": "the stored fact"}))))
            handle.queue_prefetch("the stored fact")
    thread = threading.Thread(target=recall)
    try:
        with p._vault_lock:  # Native/index transaction held by another thread.
            thread.start()
            thread.join(1)
            assert not thread.is_alive(), "live validation blocked behind native vault lock"
            assert len(result) == 2
            assert all(text == "" and "error" in search for text, search in result), "impossible journal served recall"
            assert all(handle._index_running or handle._index_requested for handle in (p, q)), "demand did not schedule validation"
        assert p._index_worker.drain(5) and q._index_worker.drain(5)
        assert (p._vault / ".mirror-map.json").read_bytes() == before
        assert not engine.calls
        assert all(handle._recall_token() is None for handle in (p, q))
    finally:
        thread.join(5)


def test_l4_valid_allocated_history_remains_ready(peers):
    p, q, engine = peers
    p._mirror_inbox.append(["add", "user", "already acknowledged", {}])
    number = p._mirror_inbox.first()[0]
    p._mirror_inbox.acknowledge(number)
    state = p._mirror_state()
    state["last_notification"] = number
    p._save_mirror_state(state)
    for handle in (p, q):
        assert "local/potion-retrieval-32m" in handle.prefetch("what is the stored fact?")
        assert "results" in json.loads(handle._handle_search({"query": "the stored fact"}))
        assert not handle._index_running and not handle._index_requested


def test_l3_rebuild_between_cache_lookup_and_return_is_fenced(peers, monkeypatch):
    p, q, engine = peers
    query = "what is the stored fact?"
    assert "local/potion-retrieval-32m" in q.prefetch(query)
    cached = q._cached_prefetch
    def overlap(text):
        result = cached(text)
        rebuild(p)
        return result
    monkeypatch.setattr(q, "_cached_prefetch", overlap)
    assert q.prefetch(query) == "", "cache-hit path skipped its completion token check"


@pytest.mark.parametrize("owner_exits", [True, False])
def test_l12_stale_engine_version_never_discharges_or_relabels(tmp_path, monkeypatch, owner_exits):
    old = make_provider(tmp_path, embedding="local/potion-retrieval-32m")
    old._zg_version_cache = "fixture-old"
    old._record_engine_state()
    new = ZvecMemoryProvider({"vault": str(old._vault), "embedding": "local/potion-retrieval-32m"})
    new._zg_version_cache = "fixture-new"
    engine = ModelEngine()
    monkeypatch.setattr(old, "_run_zg", engine)
    monkeypatch.setattr(new, "_run_zg", engine)
    if owner_exits:
        monkeypatch.setattr(new, "_maybe_reindex", lambda **kwargs: None)
    try:
        new.initialize("new-engine", hermes_home=str(tmp_path))
        marker = old._vault / _mod.REINDEX_REQUEST_FILE
        if owner_exits:
            requested = marker.read_bytes()
            new.shutdown()
        else:
            assert new._index_worker.drain(5)
            assert not marker.exists()
            assert old._engine_state()["zg_version"] == "fixture-new"
        calls = len(engine.calls)
        old._maybe_reindex(force=True)
        assert old._index_worker.drain(5)
        if owner_exits:
            assert marker.exists(), "stale engine acknowledged another engine's request"
            assert marker.read_bytes() == requested
        else:
            assert old._engine_state()["zg_version"] == "fixture-new", "stale engine relabeled peer state"
        assert not engine.calls[calls:], "incompatible engine must leave work to matching owner"
        assert old._recall_token() is None
    finally:
        old.shutdown()
        new.shutdown()


def test_l1_worker_identity_admission_cannot_replace_newer_request(tmp_path, monkeypatch):
    from zvec_memory_provider.maintenance import request_rebuild, read_request
    seed = make_provider(tmp_path, embedding="local/potion-retrieval-32m")
    state = seed._mirror_state()
    state["refresh_required"] = True
    seed._save_mirror_state(state)
    seed._zg_version_cache = "fixture-prior"
    seed._record_engine_state()
    seed.shutdown()
    p = ZvecMemoryProvider({"vault": str(seed._vault), "embedding": "local/potion-retrieval-32m"})
    p._zg_version_cache = "fixture-current"
    engine = ModelEngine()
    monkeypatch.setattr(p, "_run_zg", engine)
    newer = []
    def replaced(vault, **kwargs):
        request_rebuild(vault, identity={"plugin_schema": 1, "embedding": "local/potion-retrieval-32m", "zg_bin": "zg", "zg_version": "fixture-newest"})
        newer.append(read_request(vault))
        return request_rebuild(vault, **kwargs)
    monkeypatch.setattr(_mod, "request_rebuild", replaced)
    try:
        p.initialize("recover", hermes_home=str(tmp_path))
        assert p._index_worker.drain(5)
        assert newer
        assert read_request(p._vault) == newer[-1], "worker replaced intent admitted after its read"
        assert not [call for call in engine.calls if call[0] == "index"]
        assert p._recall_token() is None
    finally:
        p.shutdown()


def test_l3_unmarked_rebuild_keeps_peer_gate_on_publication_failure(peers, monkeypatch):
    p, q, engine = peers
    tokens = []
    def indexing(args, timeout):
        if args[0] == "index":
            tokens.append(q._recall_token())
        return engine(args, timeout)
    real = _mod.atomic_json_write
    def fault(path, value, **kwargs):
        if path == p._engine_state_path() and "index_generation" in value:
            raise OSError("cannot publish generation")
        return real(path, value, **kwargs)
    monkeypatch.setattr(p, "_run_zg", indexing)
    with monkeypatch.context() as m:
        m.setattr(_mod, "atomic_json_write", fault)
        p._maybe_reindex(force=True, extra_args=["--rebuild"])
        assert p._index_worker.drain(5)
    assert tokens == [None], "explicit rebuild ran without a peer-visible request gate"
    assert q._recall_token() is None
    assert (p._vault / _mod.REINDEX_REQUEST_FILE).exists()
    p._maybe_reindex(force=True)
    assert p._index_worker.drain(5)
    assert q._recall_token() is not None




@pytest.mark.parametrize("payload", [b'{"identity":', b'{"request_id":', b'[]', b'null'])
def test_l1_malformed_structured_request_is_not_legacy_intent(peers, payload):
    p, q, engine = peers
    marker = p._vault / _mod.REINDEX_REQUEST_FILE
    marker.write_bytes(payload)
    p._maybe_reindex(force=True)
    assert p._index_worker.drain(5)
    assert marker.exists(), "malformed structured request was silently downgraded to legacy"
    assert marker.read_bytes() == payload
    assert not engine.calls
    assert p._recall_token() is None and q._recall_token() is None
