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


def _independent_producer(vault, ready, go, admitted, mode, response_path):
    """Spawned process: real store/admission/locks, fake native boundary only."""
    vault = Path(vault)
    q = ZvecMemoryProvider({"vault": str(vault), "embedding": "local/M0", "reindex_min_seconds": 0})
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


def test_fact_ack_flushes_source_and_durable_admission(tmp_path, monkeypatch):
    from zvec_memory_provider.maintenance import read_refresh
    p = make_provider(tmp_path)
    p._index_worker.submit = lambda *args: False
    real = os.fsync
    observed = []
    def sync(fd):
        observed.append((os.readlink(f"/proc/self/fd/{fd}"), Path(f"/proc/self/fd/{fd}").read_text()))
        return real(fd)
    monkeypatch.setattr(_mod.os, "fsync", sync)
    try:
        response = json.loads(p._handle_store({"content": "durable flushed source and admission"}))
        assert response["status"] == "stored"
        path = Path(response["path"])
        assert any(Path(name) == path and body == path.read_text() for name, body in observed)
        raw = read_refresh(p._vault)
        assert raw is not None
        assert any(json.loads(body).get("request_id") == json.loads(raw)["request_id"]
                   for name, body in observed if Path(name) != path)
    finally:
        p.shutdown()
