"""Independent-review regressions: command-aware offline engine, no native proof."""
import json
import threading

import pytest

from test_provider import ZvecMemoryProvider, make_provider, _mod


class ModelEngine:
    """Existing indexes retain their model unless explicitly rebuilt."""
    def __init__(self, model="local/old"):
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


def test_l1_exited_request_owner_cannot_be_acknowledged_by_stale_peer(tmp_path, monkeypatch):
    old = make_provider(tmp_path, embedding="local/old")
    new = ZvecMemoryProvider({"vault": str(old._vault), "embedding": "local/new"})
    engine = ModelEngine()
    monkeypatch.setattr(new, "_maybe_reindex", lambda **kwargs: None)
    monkeypatch.setattr(old, "_run_zg", engine)
    try:
        new.initialize("new", hermes_home=str(tmp_path))
        marker = old._vault / _mod.REINDEX_REQUEST_FILE
        requested = marker.read_bytes()
        new.shutdown()  # No queued native dispatch by the requesting owner.
        old._maybe_reindex(force=True)
        assert old._index_worker.drain(5)
        assert engine.model == "local/new" or marker.exists(), "wrong model discharged durable work"
        if marker.exists():
            assert marker.read_bytes() == requested
            assert old._recall_token() is None
        assert old._config["embedding"] == "local/old"
    finally:
        old.shutdown()
        new.shutdown()


def test_l1_new_intent_wins_after_snapshot_before_vault_lock(tmp_path, monkeypatch):
    p = make_provider(tmp_path, embedding="local/old")
    engine = ModelEngine()
    monkeypatch.setattr(p, "_run_zg", engine)
    waiting = threading.Event()
    real_lock = p._vault_lock

    class ObservedLock:
        def acquire(self, blocking=True):
            # Adoption now uses nonwaiting admission on this same real lock.
            return real_lock.acquire(blocking=blocking)

        def __enter__(self):
            waiting.set()  # _index_job already consumed its argument snapshot.
            return real_lock.__enter__()

        def __exit__(self, *args):
            return real_lock.__exit__(*args)

    monkeypatch.setattr(p, "_vault_lock", ObservedLock())
    try:
        with real_lock:
            p._maybe_reindex(force=True, extra_args=["--rebuild", "--embedding", "local/old"])
            assert waiting.wait(5)
            monkeypatch.setattr(p._index_worker, "submit", lambda *args: False)
            p._config["embedding"] = "local/new"
            p._ensure_engine_identity()  # Replaces durable intent while old job waits.
            marker = p._vault / _mod.REINDEX_REQUEST_FILE
            requested = marker.read_bytes()
        assert p._index_worker.drain(5)
        assert engine.model == "local/new" or marker.exists(), "old snapshot erased newer intent"
        if marker.exists():
            assert marker.read_bytes() == requested
            assert p._recall_token() is None
        assert p._index_requested and not p._index_running  # Rejected continuation.
    finally:
        p.shutdown()


@pytest.mark.parametrize("identity", [None, {}, {"plugin_schema": 1, "embedding": "local/old", "zg_bin": "/untrusted/marker-command"}])
def test_l1_marker_never_selects_executable_or_malformed_identity(tmp_path, monkeypatch, identity):
    p = make_provider(tmp_path, embedding="local/old")
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


def test_l1_bound_ack_requires_identity_but_legacy_api_is_preserved(tmp_path):
    from zvec_memory_provider.maintenance import request_rebuild, read_request, acknowledge_request
    identity = {"plugin_schema": 1, "embedding": "local/new", "zg_bin": "zg"}
    path = request_rebuild(tmp_path, identity=identity)
    request = read_request(tmp_path)
    assert not acknowledge_request(tmp_path, request)
    assert not acknowledge_request(tmp_path, request, identity={**identity, "embedding": "local/old"})
    assert path.read_bytes() == request
    assert acknowledge_request(tmp_path, request, identity=identity)
    request_rebuild(tmp_path)
    assert acknowledge_request(tmp_path, read_request(tmp_path))


def test_l2_recovery_dispatch_checks_model_before_relabeling(tmp_path, monkeypatch):
    old = make_provider(tmp_path, embedding="local/old")
    state = old._mirror_state()
    state["refresh_required"] = True
    old._save_mirror_state(state)
    old.shutdown()
    engine = ModelEngine()
    p = ZvecMemoryProvider({"vault": str(old._vault), "embedding": "local/new"})
    monkeypatch.setattr(p, "_run_zg", engine)
    try:
        p.initialize("recovery", hermes_home=str(tmp_path))
        assert p._index_worker.drain(5)
        assert engine.model == "local/new", "recovery relabeled the old model without rebuilding"
        command = next(c for c in engine.calls if c[0] == "index")
        assert "--rebuild" in command and command[command.index("--embedding") + 1] == "local/new"
        assert p._engine_state()["embedding"] == engine.model
        assert p._recall_token() is not None
    finally:
        p.shutdown()


def test_l2_stale_peer_incremental_refresh_preserves_shared_identity(tmp_path, monkeypatch):
    old = make_provider(tmp_path, embedding="local/old")
    engine = ModelEngine()
    new = ZvecMemoryProvider({"vault": str(old._vault), "embedding": "local/new"})
    monkeypatch.setattr(old, "_run_zg", engine)
    monkeypatch.setattr(new, "_run_zg", engine)
    try:
        new.initialize("new", hermes_home=str(tmp_path))
        assert new._index_worker.drain(5)
        assert engine.model == "local/new"
        for p in (old, new, old, new):
            count = len(engine.calls)
            p._maybe_reindex(force=True)
            assert p._index_worker.drain(5)
            assert p._engine_state()["embedding"] == engine.model == "local/new", "stale peer relabeled shared state"
            commands = [c for c in engine.calls[count:] if c[0] == "index"]
            assert commands and all("--rebuild" not in c for c in commands), "stale peers must not thrash models"
        assert old._config["embedding"] == "local/old"
    finally:
        old.shutdown()
        new.shutdown()


def test_l2_delayed_snapshot_cannot_revert_completed_peer_identity(tmp_path, monkeypatch):
    old = make_provider(tmp_path, embedding="local/old")
    new = ZvecMemoryProvider({"vault": str(old._vault), "embedding": "local/new"})
    engine = ModelEngine()
    monkeypatch.setattr(old, "_run_zg", engine)
    monkeypatch.setattr(new, "_run_zg", engine)
    waiting, release = threading.Event(), threading.Event()
    real_lock = old._vault_lock

    class DelayedLock:
        def __enter__(self):
            waiting.set()
            assert release.wait(5)
            return real_lock.__enter__()

        def __exit__(self, *args):
            return real_lock.__exit__(*args)

    monkeypatch.setattr(old, "_vault_lock", DelayedLock())
    try:
        old._maybe_reindex(force=True, extra_args=["--rebuild", "--embedding", "local/old"])
        assert waiting.wait(5)
        new.initialize("new", hermes_home=str(tmp_path))
        assert new._index_worker.drain(5)
        assert engine.model == "local/new"
        assert not (old._vault / _mod.REINDEX_REQUEST_FILE).exists()
        release.set()
        assert old._index_worker.drain(5)
        assert engine.model == old._engine_state()["embedding"] == "local/new"
    finally:
        release.set()
        old.shutdown()
        new.shutdown()


def rebuild(p):
    from zvec_memory_provider.maintenance import request_rebuild
    request_rebuild(p._vault)
    p._maybe_reindex(force=True)
    assert p._index_worker.drain(5)
    assert not (p._vault / _mod.REINDEX_REQUEST_FILE).exists()


@pytest.fixture
def peers(tmp_path, monkeypatch):
    p = make_provider(tmp_path, embedding="local/old")
    q = make_provider(tmp_path, embedding="local/old")
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
    assert "local/old" in q.prefetch("what is the stored fact?")
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
        assert "local/old" in handle.prefetch("what is the stored fact?")
        assert "results" in json.loads(handle._handle_search({"query": "the stored fact"}))
        assert not handle._index_running and not handle._index_requested


def test_l3_rebuild_between_cache_lookup_and_return_is_fenced(peers, monkeypatch):
    p, q, engine = peers
    query = "what is the stored fact?"
    assert "local/old" in q.prefetch(query)
    cached = q._cached_prefetch
    def overlap(text):
        result = cached(text)
        rebuild(p)
        return result
    monkeypatch.setattr(q, "_cached_prefetch", overlap)
    assert q.prefetch(query) == "", "cache-hit path skipped its completion token check"


@pytest.mark.parametrize("owner_exits", [True, False])
def test_l12_stale_engine_version_never_discharges_or_relabels(tmp_path, monkeypatch, owner_exits):
    old = make_provider(tmp_path, embedding="local/old")
    old._zg_version_cache = "fixture-old"
    old._record_engine_state()
    new = ZvecMemoryProvider({"vault": str(old._vault), "embedding": "local/old"})
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
    seed = make_provider(tmp_path, embedding="local/old")
    state = seed._mirror_state()
    state["refresh_required"] = True
    seed._save_mirror_state(state)
    seed.shutdown()
    p = ZvecMemoryProvider({"vault": str(seed._vault), "embedding": "local/new"})
    engine = ModelEngine()
    monkeypatch.setattr(p, "_run_zg", engine)
    newer = []
    def replaced(vault, **kwargs):
        request_rebuild(vault, identity={"plugin_schema": 1, "embedding": "local/newest", "zg_bin": "zg"})
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


def test_l2_joining_pending_request_does_not_republish_stale_configuration(tmp_path, monkeypatch):
    from zvec_memory_provider.maintenance import request_rebuild
    seed = make_provider(tmp_path, embedding="local/old")
    seed.shutdown()
    request_rebuild(seed._vault, identity={"plugin_schema": 1, "embedding": "local/new", "zg_bin": "zg"})
    old = ZvecMemoryProvider({"vault": str(seed._vault), "embedding": "local/old"})
    new = ZvecMemoryProvider({"vault": str(seed._vault), "embedding": "local/new"})
    engine = ModelEngine()
    for handle in (old, new):
        monkeypatch.setattr(handle, "_run_zg", engine)
    try:
        with monkeypatch.context() as m:
            m.setattr(old, "_maybe_reindex", lambda **kwargs: None)
            old.initialize("joining", hermes_home=str(tmp_path))
        new.initialize("owner", hermes_home=str(tmp_path))
        assert new._index_worker.drain(5)
        assert engine.model == "local/new"
        old._maybe_reindex(force=True)
        assert old._index_worker.drain(5)
        assert old._engine_state()["embedding"] == engine.model == "local/new", "joined marker was mistaken for new config intent"
    finally:
        old.shutdown()
        new.shutdown()


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
