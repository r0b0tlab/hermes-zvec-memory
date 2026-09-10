"""Actual independent-process regressions; no native engine or profile writes."""
import multiprocessing
import time
import fcntl
from pathlib import Path

from test_provider import ZvecMemoryProvider, make_provider


def _add_process(root, content, entered, release, started):
    ZvecMemoryProvider._run_zg = lambda *a, **k: (0, "", "")
    ZvecMemoryProvider._maybe_reindex = lambda *a, **k: None
    started.set()
    p = make_provider(Path(root))
    if content == "first preference":
        original = p._write_fact
        def paused(*args, **kwargs):
            entered.set()
            assert release.wait(5)
            return original(*args, **kwargs)
        p._write_fact = paused
    p._apply_mirror("add", "user", content, {})
    p.shutdown()


def test_process_transactions_preserve_ownership_and_removal(tmp_path):
    ctx = multiprocessing.get_context("spawn")
    entered, release, started = ctx.Event(), ctx.Event(), ctx.Event()
    first = ctx.Process(target=_add_process, args=(str(tmp_path), "first preference", entered, release, started))
    second = ctx.Process(target=_add_process, args=(str(tmp_path), "second preference", entered, release, started))
    first.start()
    assert entered.wait(5)
    started.clear()
    second.start()
    assert started.wait(5)
    time.sleep(0.3)
    release.set()
    for child in (first, second):
        child.join(10)
        if child.is_alive():
            child.kill()
            child.join()
        assert child.exitcode == 0
    p = make_provider(tmp_path)
    try:
        assert {r["content"] for r in p._mirror_state()["records"].values()} == {"first preference", "second preference"}
        p._apply_mirror("remove", "user", "", {"old_text": "first preference"})
        assert not any("first preference" in f.read_text() for f in (p._vault / "facts").glob("*.md"))
    finally:
        p.shutdown()


def _hold_engine_lock(path, entered, release):
    with open(path, "w") as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        entered.set()
        assert release.wait(10)


def test_automatic_prefetch_retries_exhausted_process_lock(tmp_path, monkeypatch):
    ctx = multiprocessing.get_context("spawn")
    entered, release = ctx.Event(), ctx.Event()
    lock_path = tmp_path / "engine.lock"
    child = ctx.Process(target=_hold_engine_lock, args=(str(lock_path), entered, release))
    child.start()
    assert entered.wait(5)
    p = make_provider(tmp_path)
    calls = []
    def engine(cmd, timeout):
        if cmd[0] != "index":
            return 0, "recovered preference", ""
        calls.append(cmd)
        with open(lock_path) as stream:
            try:
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return 1, "", "ZVEC_GREP.ENGINE.LOCK.BUSY"
        return 0, "", ""
    monkeypatch.setattr(p, "_run_zg", engine)
    monkeypatch.setattr(p, "is_available", lambda: True)
    try:
        p._apply_mirror("add", "user", "new preference", {})
        assert p._index_worker.drain(3)
        assert len(calls) == 4
        before = time.monotonic()
        for _ in range(20):
            assert p.prefetch("What are my preferences?") == ""
            p.queue_prefetch("What are my preferences?")
        assert time.monotonic() - before < 0.2
        assert len(calls) == 4, "cooldown must coalesce automatic retries"
        release.set()
        child.join(5)
        assert child.exitcode == 0
        time.sleep(1.1)
        p.prefetch("What are my preferences?")
        assert p._index_worker.drain(3)
        assert len(calls) == 5
        assert "recovered preference" in p.prefetch("What are my preferences?")
    finally:
        release.set()
        child.join(5)
        p.shutdown()
