"""Demand recovery using real cross-process locks and durable journals."""
import multiprocessing
import os
import sqlite3
import threading
import time
from pathlib import Path

import pytest

from test_provider import ZvecMemoryProvider, make_provider
from test_process_safety import _hold_engine_lock


@pytest.mark.parametrize("lock_kind", ["flock", "sqlite"])
def test_startup_defers_contended_recovery_then_demand_reopens(tmp_path, monkeypatch, lock_kind):
    seed = make_provider(tmp_path)
    seed.shutdown()
    ctx = multiprocessing.get_context("spawn")
    entered, release = ctx.Event(), ctx.Event()
    target, path = ((_hold_engine_lock, seed._vault / ".mirror.lock") if lock_kind == "flock"
                    else (_hold_sqlite, seed._mirror_inbox.path))
    child = ctx.Process(target=target, args=(str(path), entered, release))
    child.start()
    assert entered.wait(5)
    p = ZvecMemoryProvider(config={"vault": str(seed._vault)})
    calls, errors = [], []
    monkeypatch.setattr(p, "_run_zg", lambda cmd, timeout: (calls.append(cmd) or (0, "healed", "")))
    monkeypatch.setattr(p, "is_available", lambda: True)
    done = threading.Event()
    def initialize():
        try:
            p.initialize("contended", hermes_home=str(tmp_path))
        except Exception as exc:
            errors.append(exc)
        finally:
            done.set()
    thread = threading.Thread(target=initialize)
    thread.start()
    try:
        assert done.wait(0.8), "startup must not wait for a peer's long index flock"
        assert not errors, "SQLite startup contention must defer, not fail initialization"
        assert p._recall_token() is None
        before = time.monotonic()
        for _ in range(20):
            assert p.prefetch("What are my preferences?") == ""
            p.queue_prefetch("What are my preferences?")
        assert time.monotonic() - before < 0.2
        release.set()
        _join(child)
        assert p._index_worker.drain(3)
        # The SQLite worker may have exhausted its short attempt while locked.
        time.sleep(1.1)
        p.queue_prefetch("What are my preferences?")
        assert p._index_worker.drain(3)
        assert p._recall_token() is not None
        assert "healed" in p.prefetch("What are my preferences?")
    finally:
        release.set()
        _join(child)
        thread.join(5)
        p.shutdown()



def test_notification_reopens_deferred_inbox_without_prefetch(tmp_path, monkeypatch):
    seed = make_provider(tmp_path)
    seed.shutdown()
    seed._mirror_inbox.append(["add", "user", "existing preference", {}])
    ctx = multiprocessing.get_context("spawn")
    entered, release = ctx.Event(), ctx.Event()
    child = ctx.Process(target=_hold_sqlite, args=(str(seed._mirror_inbox.path), entered, release))
    child.start()
    p = ZvecMemoryProvider(config={"vault": str(seed._vault)})
    peer = None
    callback = None
    unlock = ctx.Event()
    try:
        assert entered.wait(5)
        before = time.monotonic()
        p.initialize("contended", hermes_home=str(tmp_path))
        assert time.monotonic() - before < 0.8
        assert p._mirror_inbox is None
        release.set()
        _join(child)

        # Leave delivery pending to prove restart durability. Neither a
        # prefetch nor a peer's long index lock may be needed to persist it.
        assert p._disk_worker.close(timeout=3)
        monkeypatch.setattr(p, "_maybe_reindex", lambda **kwargs: None)
        locked = ctx.Event()
        peer = ctx.Process(target=_hold_engine_lock, args=(str(seed._vault / ".mirror.lock"), locked, unlock))
        peer.start()
        assert locked.wait(5)
        done, errors = threading.Event(), []
        def notify():
            try:
                p.on_memory_write("add", "user", "new preference", {})
            except Exception as exc:
                errors.append(exc)
            finally:
                done.set()
        callback = threading.Thread(target=notify)
        callback.start()
        assert done.wait(0.8), "notification persistence must not wait for the vault lock"
        assert not errors, f"notification failed after deferred initialization: {errors}"
        assert not (seed._vault / ".mirror-delivery-failed.json").exists()
        with seed._mirror_inbox.connect() as db:
            assert db.execute("SELECT COUNT(*) FROM notifications").fetchone()[0] == 2
        assert p._recall_token() is None
    finally:
        release.set()
        unlock.set()
        _join(child)
        if peer is not None:
            _join(peer)
        if callback is not None:
            callback.join(5)
        p.shutdown()

    recovered = make_provider(tmp_path)
    try:
        assert recovered._index_worker.drain(3)
        assert recovered._mirror_inbox.first() is None
        state = recovered._mirror_state()
        assert sorted(record["content"] for record in state["records"].values()) == [
            "existing preference", "new preference"]
        assert len(list((seed._vault / "facts").glob("*.md"))) == 2
        recovered._drain_mirror_inbox()
        assert recovered._mirror_state() == state
        assert recovered._recall_token() is not None
    finally:
        recovered.shutdown()


def _hold_sqlite(path, entered, release):
    with sqlite3.connect(path) as db:
        db.execute("BEGIN EXCLUSIVE")
        entered.set()
        assert release.wait(10)


def _remove_then_exit(root):
    ZvecMemoryProvider._maybe_reindex = lambda *a, **k: None
    p = make_provider(Path(root))
    p.on_memory_write("remove", "user", "", {"old_text": "obsolete"})
    assert p._disk_worker.drain(3)
    assert p._mirror_inbox.first() is None
    assert p._mirror_state()["refresh_required"]
    os._exit(0)


def test_clean_handle_discovers_peer_refresh_after_exit(tmp_path, monkeypatch):
    p = make_provider(tmp_path)
    calls = []
    monkeypatch.setattr(p, "_run_zg", lambda cmd, timeout: (calls.append(cmd) or (0, "healed", "")))
    monkeypatch.setattr(p, "is_available", lambda: True)
    try:
        p._apply_mirror("add", "user", "obsolete", {})
        assert p._index_worker.drain(3)
        calls.clear()
        assert not p._index_requested
        child = multiprocessing.get_context("spawn").Process(target=_remove_then_exit, args=(str(tmp_path),))
        child.start()
        _join(child)
        assert p._mirror_inbox.first() is None
        assert p._mirror_state()["refresh_required"]
        assert p._recall_token() is None
        p.prefetch("What are my preferences?")
        assert p._index_worker.drain(3)
        assert not p._mirror_state()["refresh_required"]
        assert p._recall_token() is not None
        assert len([c for c in calls if c[0] == "index"]) == 1
    finally:
        p.shutdown()


def _join(child):
    child.join(5)
    if child.is_alive():
        child.kill()
        child.join()
    assert child.exitcode == 0


@pytest.mark.parametrize("demand_while_locked", [False, True])
def test_demand_recovers_inbox_after_worker_sqlite_timeout(tmp_path, monkeypatch, demand_while_locked):
    p = make_provider(tmp_path)
    calls = []
    monkeypatch.setattr(p, "_run_zg", lambda cmd, timeout: (calls.append(cmd) or (0, "healed", "")))
    monkeypatch.setattr(p, "is_available", lambda: True)
    ctx = multiprocessing.get_context("spawn")
    entered, release = ctx.Event(), ctx.Event()
    paused, resume = threading.Event(), threading.Event()
    child = None
    try:
        p._apply_mirror("add", "user", "obsolete", {})
        assert p._index_worker.drain(3)
        calls.clear()
        def pause():
            paused.set()
            assert resume.wait(5)
        p._disk_worker.submit(pause)
        assert paused.wait(3)
        p.on_memory_write("remove", "user", "", {"old_text": "obsolete"})
        child = ctx.Process(target=_hold_sqlite, args=(str(p._mirror_inbox.path), entered, release))
        child.start()
        assert entered.wait(5)
        resume.set()
        assert p._disk_worker.drain(3)  # actual .2s SELECT timeout
        assert not calls
        if demand_while_locked:
            before = time.monotonic()
            for _ in range(20):
                assert p.prefetch("What are my preferences?") == ""
                p.queue_prefetch("What are my preferences?")
            assert time.monotonic() - before < 0.2, "discovery/recall must not wait on SQLite"
            assert p._index_worker.drain(3)
        else:
            assert not p._index_requested, "must discover durable work without a local retry flag"
        release.set()
        _join(child)
        assert p._mirror_inbox.first() is not None
        if demand_while_locked:
            time.sleep(1.1)
        p.queue_prefetch("What are my preferences?")
        assert p._index_worker.drain(3)
        assert p._mirror_inbox.first() is None
        assert p._mirror_state()["records"] == {}
        assert p._recall_token() is not None
        assert 1 <= len([c for c in calls if c[0] == "index"]) <= 2
        count = len(calls)
        for _ in range(20):
            p.queue_prefetch("What are my preferences?")
        assert p._index_worker.drain(3)
        assert len([c for c in calls[count:] if c[0] == "index"]) == 0
    finally:
        resume.set()
        release.set()
        if child is not None:
            _join(child)
        p.shutdown()
