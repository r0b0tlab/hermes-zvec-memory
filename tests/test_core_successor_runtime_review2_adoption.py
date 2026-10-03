# SOURCE-ONLY DRAFT: NOT_EXECUTED. Parent materializes/runs under original pt.sh shared lock/resource caps.
"""Legacy adoption is an identity transaction, never a stale absent snapshot."""
import threading

from test_provider import ZvecMemoryProvider, make_provider
from test_core_successor_runtime_review import ModelEngine


def test_legacy_adoption_cannot_overwrite_completed_peer_identity(tmp_path, monkeypatch):
    from zvec_memory_provider.maintenance import request_rebuild

    vault = tmp_path / "vault"
    (vault / ".zvec-grep").mkdir(parents=True)
    (vault / ".zvec-grep/manifest.json").write_text("{}")
    old = ZvecMemoryProvider({"vault": str(vault), "embedding": "local/potion-retrieval-32m"})
    new = ZvecMemoryProvider({"vault": str(vault), "embedding": "local/potion-retrieval-32m"})
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
        request_rebuild(vault, identity={"plugin_schema": 1, "embedding": "local/potion-retrieval-32m", "zg_bin": "zg"})
        new.initialize("peer", hermes_home=str(tmp_path))
        if acquired:
            assert new._disk_worker.drain(5) and new._index_worker.drain(5)
            peer_published = new._engine_state()
            assert peer_published["embedding"] == engine.model == "local/potion-retrieval-32m"
        release.set()
        thread.join(5)
        assert not thread.is_alive() and not errors, errors
        assert new._disk_worker.drain(5) and new._index_worker.drain(5)
        assert old._disk_worker.drain(5) and old._index_worker.drain(5)
        final = new._engine_state()
        assert final["embedding"] == engine.model == "local/potion-retrieval-32m", "legacy adoption relabeled a peer-built model"
        if peer_published is not None:
            assert final == peer_published, "legacy adoption replaced a completed peer generation"
    finally:
        release.set()
        thread.join(5)
        old.shutdown()
        new.shutdown()



def test_busy_legacy_adoption_defers_without_inline_version_or_write(tmp_path, monkeypatch):
    seed = make_provider(tmp_path, embedding="local/potion-retrieval-32m")
    seed.shutdown()
    seed._engine_state_path().unlink()
    p = ZvecMemoryProvider({"vault": str(seed._vault), "embedding": "local/potion-retrieval-32m"})
    engine = ModelEngine()
    done, errors = threading.Event(), []
    calls = []

    def run(args, timeout):
        calls.append((threading.get_ident(), list(args)))
        return engine(args, timeout)

    monkeypatch.setattr(p, "_run_zg", run)

    def initialize():
        try:
            p.initialize("contended-legacy", hermes_home=str(tmp_path))
        except BaseException as exc:
            errors.append(repr(exc))
        finally:
            done.set()

    thread = threading.Thread(target=initialize)
    try:
        with seed._vault_lock:
            thread.start()
            assert done.wait(1), "legacy adoption blocked startup behind native work"
            assert not errors, errors
            assert not seed._engine_state_path().exists(), "adoption published outside the transaction"
            assert not [args for ident, args in calls if ident == thread.ident], "startup performed deferred native work inline"
            assert p._recall_token() is None
            assert p._index_running or p._index_requested
        assert p._index_worker.drain(5)
        assert p._engine_state()["embedding"] == "local/potion-retrieval-32m"
        assert p._recall_token() is not None
    finally:
        thread.join(5)
        p.shutdown()
