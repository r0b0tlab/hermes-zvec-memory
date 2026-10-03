# SOURCE-ONLY DRAFT: NOT_EXECUTED. Parent materializes/runs under original pt.sh shared lock/resource caps.
"""R2-L1/L3 admission finish: inherited parent cases and producer controls.

The first five cases inherit residual-regressions.py's real startup/live-config
and spawned idle-producer histories. SnapshotEngine is an explicitly offline
command-boundary double, not native model/index/performance evidence.
"""
import json
import multiprocessing
import os
import sys
import threading
from pathlib import Path

import pytest

from test_provider import ZvecMemoryProvider, make_provider, _mod
from zvec_memory_provider.maintenance import read_request, request_identity


class SnapshotEngine:
    def __init__(self, vault):
        self.vault = Path(vault)

    def __call__(self, args, timeout):
        if args == ["--version"]:
            return 0, "", ""
        assert args[0] == "index", "This diagnostic does not execute native queries"
        files = sorted(str(p.relative_to(self.vault)) for p in (self.vault / "facts").glob("*.md"))
        (self.vault / ".zvec-grep/fixture-indexed-files.json").write_text(json.dumps(files))
        return 0, "", ""


@pytest.mark.parametrize("newer_intent", [False, True])
def test_startup_observation_must_not_replace_newer_live_intent(tmp_path, monkeypatch, newer_intent):
    peer = make_provider(tmp_path, embedding="local/potion-retrieval-32m")
    old = ZvecMemoryProvider({"vault": str(peer._vault), "embedding": "local/potion-retrieval-32m"})
    # Same-model stale-state injection; no alternative runtime is executed.
    peer._zg_version_cache = "fixture-prior"
    peer._record_engine_state()
    old._zg_version_cache = "fixture-startup"
    # Freeze only later job dispatch; preserve actual startup admission.
    monkeypatch.setattr(old, "_maybe_reindex", lambda **kwargs: None)
    monkeypatch.setattr(peer, "_maybe_reindex", lambda **kwargs: None)
    monkeypatch.setattr(old, "_run_zg", SnapshotEngine(peer._vault))
    observed, release = threading.Event(), threading.Event()
    errors = []
    real_read = _mod.read_request

    def pause_after_absent_read(vault):
        raw = real_read(vault)
        if (threading.current_thread().name == "startup-repro"
                and sys._getframe(1).f_code.co_name == "_ensure_engine_identity_locked"
                and raw is None and not observed.is_set()):
            observed.set()
            assert release.wait(15), "parent did not release startup observation"
        return raw

    def initialize():
        try:
            old.initialize("startup", hermes_home=str(tmp_path))
        except BaseException as exc:
            errors.append(repr(exc))

    monkeypatch.setattr(_mod, "read_request", pause_after_absent_read)
    thread = threading.Thread(target=initialize, name="startup-repro")
    try:
        thread.start()
        assert observed.wait(5), "did not reach actual startup absent-request observation"
        acquired = peer._vault_lock.acquire(blocking=False)
        if acquired:
            peer._vault_lock.__exit__()
        assert not acquired, "initializer must hold the real vault transaction"
        if newer_intent:
            from zvec_memory_provider.maintenance import request_rebuild
            request_rebuild(peer._vault, identity={**peer._engine_identity(), "zg_version": "fixture-newer"})
            assert request_identity(read_request(peer._vault))["zg_version"] == "fixture-newer"
            peer.shutdown()  # Newer owner cannot perform the work afterward.
        release.set()
        thread.join(5)
        assert not thread.is_alive() and not errors, errors
        found = request_identity(read_request(peer._vault))
        expected = "fixture-newer" if newer_intent else "fixture-startup"
        (tmp_path / "startup-observation.json").write_text(json.dumps({
            "newer_intent": newer_intent, "expected": expected, "found": found,
            "vault_was_locked": not acquired, "errors": errors,
        }, indent=2))
        assert found["zg_version"] == expected, "stale startup admission erased newer same-model intent"
    finally:
        release.set()
        thread.join(5)
        old.shutdown()
        peer.shutdown()



def _independent_producer(vault, ready, go, admitted, mode, response_path):
    """Spawned process: real store/admission/locks, fake native boundary only."""
    vault = Path(vault)
    q = ZvecMemoryProvider({"vault": str(vault), "embedding": "local/potion-retrieval-32m", "reindex_min_seconds": 0})
    q._run_zg = SnapshotEngine(vault)
    q.initialize("independent-producer", hermes_home=str(vault.parent))
    assert q._disk_worker.drain(5) and q._index_worker.drain(5)
    assert not q._index_running and not q._index_requested
    waiting = threading.Event()
    actual_lock = q._vault_lock

    class ObservedLock:
        def acquire(self, blocking=True):
            return actual_lock.acquire(blocking=blocking)

        def __enter__(self):
            waiting.set()
            return actual_lock.__enter__()

        def __exit__(self, *args):
            return actual_lock.__exit__(*args)

    q._vault_lock = ObservedLock()
    if mode == "rejected-exit":
        q._index_worker.submit = lambda *args: False
    ready.set()
    assert go.wait(15), "parent did not begin paused publication"
    response = json.loads(q._handle_store({"content": "Fact written strictly after the older native index returned."}))
    assert response["status"] == "stored"
    if mode != "rejected-exit":
        assert waiting.wait(5), "accepted worker did not reach the held vault lock"
    Path(response_path).write_text(json.dumps({
        "mode": mode, "store": response, "waiting_for_vault": waiting.is_set(),
        "index_running": q._index_running, "index_requested": q._index_requested,
    }, indent=2))
    admitted.set()
    if mode == "drained-control":
        assert q._index_worker.drain(10)
        q.shutdown()
    os._exit(23)  # Intentional owned process exit, no product cleanup claim.


@pytest.mark.parametrize("mode", ["waiting-exit", "rejected-exit", "drained-control"])
def test_independent_producer_refresh_survives_exit(tmp_path, monkeypatch, mode):
    p = make_provider(tmp_path, embedding="local/potion-retrieval-32m")
    monkeypatch.setattr(p, "_run_zg", SnapshotEngine(p._vault))
    ctx = multiprocessing.get_context("spawn")
    ready, go, admitted = ctx.Event(), ctx.Event(), ctx.Event()
    returned, release = threading.Event(), threading.Event()
    response_path = tmp_path / "producer-response.json"
    child = ctx.Process(target=_independent_producer,
                        args=(p._vault, ready, go, admitted, mode, response_path))
    original = p._record_engine_state
    publications = []

    def pause_publication(identity=None):
        if not publications:
            publications.append(True)
            returned.set()  # _run_zg(index) already succeeded, while vault held.
            assert release.wait(20), "parent did not release generation publication"
        return original(identity)

    monkeypatch.setattr(p, "_record_engine_state", pause_publication)
    try:
        child.start()
        assert ready.wait(8), "independent provider did not initialize idle"
        p._maybe_reindex(force=True)
        assert returned.wait(5), "old native boundary did not complete"
        indexed_path = p._vault / ".zvec-grep/fixture-indexed-files.json"
        assert json.loads(indexed_path.read_text()) == []
        go.set()
        assert admitted.wait(8), "independent source write/admission did not complete"
        response = json.loads(response_path.read_text())
        fact = Path(response["store"]["path"])
        assert fact.is_file() and str(fact.relative_to(p._vault)) not in json.loads(indexed_path.read_text())
        if mode != "drained-control":
            child.join(5)
            assert not child.is_alive() and child.exitcode == 23
        release.set()
        assert p._index_worker.drain(5)
        child.join(8)
        assert not child.is_alive() and child.exitcode == 23
        # No extra store/manual reindex: exercise surviving demand discovery.
        p._retry_pending_index()
        assert p._index_worker.drain(5)
        indexed = json.loads(indexed_path.read_text())
        pending = (p._vault / ".index-refresh.json").exists()
        (tmp_path / "continuation-observation.json").write_text(json.dumps({
            "mode": mode, "producer": response, "indexed_files": indexed,
            "later_fact": str(fact.relative_to(p._vault)), "pending_refresh": pending,
            "surviving_recall_open": p._recall_token() is not None,
        }, indent=2))
        assert str(fact.relative_to(p._vault)) in indexed, "surviving demand lost an independently admitted refresh"
    finally:
        go.set()
        release.set()
        if child.pid is not None:
            child.join(2)
            if child.is_alive():
                child.kill()
                child.join(5)
        p.shutdown()


@pytest.mark.parametrize("producer", ["store", "session", "extraction"])
@pytest.mark.parametrize("scheduling", ["debounced", "rejected"])
def test_source_producers_admit_before_local_scheduling(tmp_path, monkeypatch, producer, scheduling):
    import time
    from zvec_memory_provider.maintenance import read_refresh
    p = make_provider(tmp_path, embedding="local/potion-retrieval-32m")
    try:
        p._last_reindex = time.monotonic()
        p._index_worker.submit = lambda *args: False
        # Extraction normally forces local admission; use a real running-free
        # producer and ordinary debounce for the other two actual entry points.
        if scheduling == "rejected":
            p._config["reindex_min_seconds"] = 0
        if producer == "store":
            assert json.loads(p._handle_store({"content": "durable producer fact"}))["status"] == "stored"
        elif producer == "session":
            p._append_turn("source user", "source assistant", "source-session")
        else:
            p._auto_extract([{"role": "user", "content": "I prefer durable admission for every saved fact."}])
        files = list((p._vault / "sessions" if producer == "session" else p._vault / "facts").glob("*.md"))
        assert files, "real source producer did not write"
        assert not p._index_running
        assert read_refresh(p._vault) is not None, "source mutation was only locally admitted"
        p.shutdown()
        q = ZvecMemoryProvider({"vault": str(p._vault), "embedding": "local/potion-retrieval-32m"})
        engine = SnapshotEngine(p._vault)
        monkeypatch.setattr(q, "_run_zg", engine)
        try:
            q.initialize("source-survivor", hermes_home=str(tmp_path))
            assert q._index_worker.drain(5)
            assert read_refresh(p._vault) is None
            assert q._recall_token() is not None
        finally:
            q.shutdown()
    finally:
        p.shutdown()


@pytest.mark.parametrize("newer_intent", [False, True])
def test_adoption_failure_joins_the_captured_request(tmp_path, monkeypatch, newer_intent):
    peer = make_provider(tmp_path, embedding="local/potion-retrieval-32m")
    peer._engine_state_path().unlink()  # Real legacy-adoption branch.
    old = ZvecMemoryProvider({"vault": str(peer._vault), "embedding": "local/potion-retrieval-32m"})
    monkeypatch.setattr(old, "_maybe_reindex", lambda **kwargs: None)
    monkeypatch.setattr(peer, "_maybe_reindex", lambda **kwargs: None)
    captured = []
    def failed_adoption(identity=None):
        if newer_intent:
            from zvec_memory_provider.maintenance import request_rebuild
            request_rebuild(peer._vault, identity=peer._engine_identity())
            captured.append(read_request(peer._vault))
            peer.shutdown()
        raise OSError("adoption publication refused before replace")
    monkeypatch.setattr(old, "_record_engine_state", failed_adoption)
    try:
        old.initialize("failed-adoption", hermes_home=str(tmp_path))
        raw = read_request(old._vault)
        assert request_identity(raw)["embedding"] == "local/potion-retrieval-32m"
        if newer_intent:
            assert captured and raw == captured[-1], "failed adoption replaced the newer request generation"
    finally:
        old.shutdown()
        peer.shutdown()


def test_failed_request_observation_is_not_permission_to_replace(tmp_path, monkeypatch):
    peer = make_provider(tmp_path, embedding="local/potion-retrieval-32m")
    old = ZvecMemoryProvider({"vault": str(peer._vault), "embedding": "local/potion-retrieval-32m"})
    monkeypatch.setattr(old, "_maybe_reindex", lambda **kwargs: None)
    monkeypatch.setattr(peer, "_maybe_reindex", lambda **kwargs: None)
    from zvec_memory_provider.maintenance import request_rebuild
    request_rebuild(peer._vault, identity=peer._engine_identity())
    captured = read_request(peer._vault)
    # Populate the provider locks/inbox first, then exercise the actual helper
    # with a refused observation (not fabricated absence).
    old.initialize("unknown-observation", hermes_home=str(tmp_path))
    def refused(vault):
        raise OSError("request observation unavailable")
    monkeypatch.setattr(_mod, "read_request", refused)
    try:
        old._ensure_engine_identity()
        assert read_request(peer._vault) == captured
    finally:
        old.shutdown()
        peer.shutdown()


def test_idle_validation_does_not_advance_peer_mutation(tmp_path, monkeypatch):
    from zvec_memory_provider.maintenance import begin_refresh, read_refresh, acknowledge_refresh
    peer = make_provider(tmp_path, embedding="local/potion-retrieval-32m")
    q = ZvecMemoryProvider({"vault": str(peer._vault), "embedding": "local/potion-retrieval-32m"})
    try:
        with peer._vault_lock:
            raw = begin_refresh(peer._vault, peer._engine_identity(), "refresh")
            # Native work is held elsewhere. Startup must remain nonblocking,
            # admit local validation, and not invent a new source mutation.
            q.initialize("idle-validation", hermes_home=str(tmp_path))
            assert q._index_running  # Real worker admitted, waiting on this vault.
            assert read_refresh(peer._vault) == raw
            assert acknowledge_refresh(peer._vault, raw)
        assert q._index_worker.drain(5)
        assert read_refresh(peer._vault) is None
    finally:
        q.shutdown()
        peer.shutdown()




def test_probe_failure_cannot_supersede_observed_explicit_intent(tmp_path, monkeypatch):
    peer = make_provider(tmp_path, embedding="local/potion-retrieval-32m")
    old = make_provider(tmp_path, embedding="local/potion-retrieval-32m")
    monkeypatch.setattr(old, "_maybe_reindex", lambda **kwargs: None)
    monkeypatch.setattr(peer, "_maybe_reindex", lambda **kwargs: None)
    try:
        from zvec_memory_provider.maintenance import request_rebuild
        request_rebuild(peer._vault, identity=peer._engine_identity())
        raw = read_request(peer._vault)
        old._checked_identity = None  # Automatic startup validation, not live edit.
        def failed_probe():
            raise OSError("version unavailable")
        monkeypatch.setattr(old, "_zg_version", failed_probe)
        old._ensure_engine_identity()
        assert read_request(peer._vault) == raw, "automatic failure replaced observed explicit intent"
    finally:
        old.shutdown()
        peer.shutdown()


def test_later_source_survives_older_publication_and_rejected_followup(tmp_path, monkeypatch):
    from zvec_memory_provider.maintenance import read_refresh
    from test_core_successor_runtime_review2_publication import FileEngine
    p = make_provider(tmp_path, embedding="local/potion-retrieval-32m")
    q = make_provider(tmp_path, embedding="local/potion-retrieval-32m")
    entered, release = threading.Event(), threading.Event()
    engine = FileEngine(p._vault)
    monkeypatch.setattr(p, "_run_zg", engine)
    original = p._record_engine_state
    captured = []
    def publication(identity=None):
        if not captured:
            captured.append(read_refresh(p._vault))
            entered.set()  # Native call has returned, before generation publish.
            assert release.wait(10)
        return original(identity)
    monkeypatch.setattr(p, "_record_engine_state", publication)
    try:
        p._maybe_reindex(force=True)
        assert entered.wait(5)
        q._index_worker.submit = lambda *args: False
        assert not q._index_running
        result = json.loads(q._handle_store({"content": "later independent source"}))
        assert result["status"] == "stored"
        advanced = read_refresh(p._vault)
        assert advanced != captured[0]
        assert json.loads(advanced)["request_id"] != json.loads(captured[0])["request_id"]
        q.shutdown()
        p._index_worker.submit = lambda *args: False
        release.set()
        assert p._index_worker.drain(5)
        surviving = json.loads(read_refresh(p._vault))
        assert surviving == {"request_id": json.loads(advanced)["request_id"]}
        p.shutdown()
        # Fresh stale-model configuration is only ordinary refresh demand.
        survivor = ZvecMemoryProvider({"vault": str(p._vault), "embedding": "local/potion-retrieval-32m"})
        recovery = FileEngine(p._vault)
        monkeypatch.setattr(survivor, "_run_zg", recovery)
        try:
            survivor.initialize("surviving-demand", hermes_home=str(tmp_path))
            assert survivor._index_worker.drain(5)
            commands = [c for c in recovery.calls if c[0] == "index"]
            assert commands and all("--rebuild" not in c and "--embedding" not in c for c in commands)
            assert survivor._engine_state()["embedding"] == "local/potion-retrieval-32m"
            assert read_refresh(p._vault) is None
            assert survivor._recall_token() is not None
        finally:
            survivor.shutdown()
    finally:
        release.set()
        p.shutdown()
        q.shutdown()
