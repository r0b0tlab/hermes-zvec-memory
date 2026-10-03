"""Process-persistent gates for managed first/plain/rebuild mutations.

FileEngine models only command selection/model retention, not native semantics.
"""
import json
import multiprocessing
import os
import threading
from pathlib import Path

import pytest

from test_provider import ZvecMemoryProvider, make_provider, _mod
from test_runtime_review import peers
from zvec_memory_provider.maintenance import request_rebuild


class FileEngine:
    def __init__(self, vault, fail=None):
        self.vault = Path(vault)
        self.fail = fail
        self.calls = []

    def __call__(self, args, timeout):
        self.calls.append(list(args))
        root = self.vault / ".zvec-grep"
        model = root / "fixture-built-model.txt"
        if args == ["--version"]:
            return 0, "", ""  # Deliberately unknown, consistently across handles.
        if args[0] == "query":
            return 0, model.read_text() if model.exists() else "local/old", ""
        assert args[0] == "index"
        root.mkdir(exist_ok=True)
        if not (root / "manifest.json").exists() or "--rebuild" in args:
            model.write_text(args[args.index("--embedding") + 1])
        (root / "manifest.json").write_text('{"fixture_only":true}')
        if self.fail == "exit-native":
            os._exit(23)
        if self.fail == "native":
            return 1, "", "fixture native failure after mutation"
        return 0, "", ""


def attempt(vault, kind, fault):
    """Used inside the guarded pytest child too; all engine calls are fake."""
    p = ZvecMemoryProvider({"vault": str(vault), "embedding": "local/old"})
    p._zg_version_cache = ""
    engine = FileEngine(vault, fault)
    p._run_zg = engine
    schedule = p._maybe_reindex
    p._maybe_reindex = lambda **kwargs: None
    p.initialize("fault-owner", hermes_home=str(Path(vault).parent))
    p._maybe_reindex = schedule
    original = p._record_engine_state

    def publication(identity=None):
        if fault in {"after", "exit-after"}:
            original(identity)
        if fault.startswith("exit-"):
            os._exit(23)
        raise OSError("fixture generation publication fault")

    if fault not in {"native", "exit-native"}:
        p._record_engine_state = publication
    if kind == "rebuild":
        request_rebuild(vault)
    if kind == "first":
        p._build_index()
    else:
        p._maybe_reindex(force=True)
    assert p._index_worker.drain(5)
    assert p._index_requested and not p._index_running
    p.shutdown()
    return engine.calls


@pytest.mark.parametrize("kind", ["first", "plain", "rebuild"])
@pytest.mark.parametrize("fault", ["before", "after", "native", "exit-before", "exit-after", "exit-native"])
def test_uncertain_publication_survives_owner_exit_and_recovers(tmp_path, monkeypatch, kind, fault):
    vault = tmp_path / "vault"
    peer = None
    if kind != "first":
        peer = make_provider(tmp_path, embedding="local/old")
        (vault / ".zvec-grep/fixture-built-model.txt").write_text("local/old")
        monkeypatch.setattr(peer, "_run_zg", FileEngine(vault))
        monkeypatch.setattr(peer, "is_available", lambda: True)
        assert "local/old" in peer.prefetch("which fact was stored?")
    child = None
    later = matching = None
    try:
        if fault.startswith("exit-"):
            child = multiprocessing.get_context("spawn").Process(target=attempt, args=(vault, kind, fault))
            child.start()
            child.join(8)
            assert not child.is_alive() and child.exitcode == 23
        else:
            calls = attempt(vault, kind, fault)
            assert any(c[0] == "index" for c in calls)
        assert (vault / ".zvec-grep/manifest.json").exists()
        if peer is not None:
            assert peer._recall_token() is None, "uncertain mutation left the idle peer open"
            # No accidental healing while checking the cached-return fence.
            monkeypatch.setattr(peer, "_maybe_reindex", lambda **kwargs: None)
            assert peer.prefetch("which fact was stored?") == ""
        else:
            later = ZvecMemoryProvider({"vault": str(vault), "embedding": "local/new"})
            later_engine = FileEngine(vault)
            monkeypatch.setattr(later, "_run_zg", later_engine)
            later.initialize("different-model", hermes_home=str(tmp_path))
            assert later._index_worker.drain(5)
            built = (vault / ".zvec-grep/fixture-built-model.txt").read_text()
            recorded = later._engine_state()
            assert recorded.get("embedding") in (None, built), "interrupted first build was blindly adopted"
            if built == "local/old":
                assert later._recall_token() is None, "unresolved first-build intent must fence recall"
            later.shutdown()
        matching = ZvecMemoryProvider({"vault": str(vault), "embedding": "local/old"})
        recovery = FileEngine(vault)
        monkeypatch.setattr(matching, "_run_zg", recovery)
        matching.initialize("matching-recovery", hermes_home=str(tmp_path))
        assert matching._index_worker.drain(5)
        assert matching._recall_token() is not None
        assert matching._engine_state()["embedding"] == "local/old"
        commands = [c for c in recovery.calls if c[0] == "index"]
        assert commands, "no durable obligation was recovered"
        if kind == "plain":
            assert all("--rebuild" not in c for c in commands), "ordinary recovery became a model rebuild"
        else:
            assert any("--rebuild" in c for c in commands)
        assert not (vault / _mod.REINDEX_REQUEST_FILE).exists()
    finally:
        if child is not None and child.is_alive():
            child.kill()
            child.join(5)
        for p in (peer, later, matching):
            if p is not None:
                p.shutdown()


@pytest.mark.parametrize("entry", ["prefetch", "search", "queued"])
@pytest.mark.parametrize("after_write", [False, True])
def test_plain_publication_fault_fences_inflight_peer(peers, monkeypatch, entry, after_write):
    p, q, engine = peers
    started, release = threading.Event(), threading.Event()
    result = []
    original = p._record_engine_state
    def fault(identity=None):
        if after_write:
            original(identity)
        raise OSError("plain publication fault")
    def query(args, timeout):
        assert args[0] == "query"
        started.set()
        assert release.wait(5)
        return 0, "stale in-flight result", ""
    monkeypatch.setattr(q, "_run_zg", query)
    monkeypatch.setattr(p, "_record_engine_state", fault)
    def recall():
        result.append(json.loads(q._handle_search({"query": "the stored fact"})) if entry == "search"
                      else q.prefetch("the stored fact"))
    thread = None
    try:
        if entry == "queued":
            q.queue_prefetch("the stored fact")
        else:
            thread = threading.Thread(target=recall)
            thread.start()
        assert started.wait(5)
        p._maybe_reindex(force=True)
        assert p._index_worker.drain(5)
        release.set()
        if thread is not None:
            thread.join(5)
            assert not thread.is_alive()
        else:
            assert q._disk_worker.drain(5)
        if entry == "search":
            assert "error" in result[0]
        elif entry == "prefetch":
            assert result == [""]
        else:
            assert q._cached_prefetch("the stored fact") is None
    finally:
        release.set()
        if thread is not None:
            thread.join(5)


def test_plain_continuation_rejection_retains_shared_work_after_shutdown(peers, monkeypatch):
    p, q, engine = peers
    entered, release = threading.Event(), threading.Event()
    def blocked(args, timeout):
        if args[0] == "index":
            entered.set()
            assert release.wait(5)
        return engine(args, timeout)
    monkeypatch.setattr(p, "_run_zg", blocked)
    try:
        p._maybe_reindex(force=True)
        assert entered.wait(5)
        p._maybe_reindex(force=True)
        monkeypatch.setattr(p._index_worker, "submit", lambda *args: False)
        release.set()
        assert p._index_worker.drain(5)
        assert p._index_requested and not p._index_running
        p.shutdown()
        assert q._recall_token() is None, "rejected plain continuation lost shared obligation"
        q.prefetch("which fact was stored?")
        assert q._index_worker.drain(5)
        assert q._recall_token() is not None
        commands = [c for c in engine.calls if c[0] == "index"]
        assert len(commands) == 2 and all("--rebuild" not in c for c in commands)
    finally:
        release.set()
