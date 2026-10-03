import multiprocessing
import os
import threading
import time
from pathlib import Path

import pytest

from test_provider import ZvecMemoryProvider, make_provider


def _saturated_exit(root):
    ZvecMemoryProvider._run_zg = lambda *a, **k: (0, "", "")
    p = make_provider(Path(root))
    p._apply_mirror("add", "user", "obsolete preference", {})
    assert p._index_worker.drain(3)
    entered = threading.Event()
    def blocked():
        entered.set()
        threading.Event().wait(30)
    assert p._disk_worker.submit(blocked)
    assert entered.wait(3)
    # Prevent the independent fallback index worker from consuming the spool.
    entered.clear()
    assert p._index_worker.submit(blocked)
    assert entered.wait(3)
    for _ in range(256):
        assert p._disk_worker.submit(lambda: None)
    before = time.monotonic()
    p.on_memory_write("remove", "user", "", {"old_text": "obsolete preference"})
    assert time.monotonic() - before < 0.5
    assert p._recall_token() is None
    assert p._mirror_inbox.first() is not None
    # Real abrupt exit: no daemon threads, shutdown or atexit draining.
    os._exit(0)


@pytest.mark.parametrize("previous", [None, "", False, {}, []])
def test_invalid_authoritative_identity_does_not_fall_back_or_acknowledge(tmp_path, previous):
    p = make_provider(tmp_path)
    try:
        p._apply_mirror("add", "user", "Exact existing preference.", {})
        assert p._index_worker.drain(3)
        p._mirror_inbox.append(["remove", "user", "", {
            "old_text": "Exact existing preference.", "previous_content": previous}])
        pending = p._mirror_inbox.first()
        with pytest.raises(ValueError, match="previous memory content"):
            p._drain_mirror_inbox()
        assert p._mirror_inbox.first() == pending
        assert {record["content"] for record in p._mirror_state()["records"].values()} == {"Exact existing preference."}
        assert p._recall_token() is None
    finally:
        p.shutdown()


@pytest.mark.parametrize("action", ["replace", "remove"])
def test_legacy_substring_notification_stays_pending(tmp_path, action):
    p = make_provider(tmp_path)
    try:
        for content in ("Uses a blue interface.", "Another blue interface preference."):
            p._apply_mirror("add", "user", content, {})
        assert p._index_worker.drain(3)
        original = {path.name: path.read_bytes() for path in (p._vault / "facts").glob("*.md")}
        p._mirror_inbox.append([action, "user", "Uses a green interface.",
                                {"old_text": "blue interface"}])
        pending = p._mirror_inbox.first()
        with pytest.raises(ValueError, match="Legacy selector"):
            p._drain_mirror_inbox()
        assert p._mirror_inbox.first() == pending
        assert p._recall_token() is None
        assert {path.name: path.read_bytes() for path in (p._vault / "facts").glob("*.md")} == original
    finally:
        p.shutdown()


def test_saturated_notification_recovers_without_restart(tmp_path):
    p = make_provider(tmp_path)
    entered, release = threading.Event(), threading.Event()
    try:
        p._apply_mirror("add", "user", "obsolete preference", {})
        assert p._index_worker.drain(3)
        def blocked():
            entered.set()
            release.wait(5)
        assert p._disk_worker.submit(blocked)
        assert entered.wait(3)
        for _ in range(256):
            assert p._disk_worker.submit(lambda: None)
        p.on_memory_write("remove", "user", "", {"old_text": "obsolete preference"})
        assert p._recall_token() is None
        assert p._index_worker.drain(3)
        assert p._mirror_state()["records"] == {}
    finally:
        release.set()
        p.shutdown()


def test_saturated_notification_survives_process_exit(tmp_path):
    ctx = multiprocessing.get_context("spawn")
    child = ctx.Process(target=_saturated_exit, args=(str(tmp_path),))
    child.start()
    child.join(10)
    if child.is_alive():
        child.kill()
        child.join()
    assert child.exitcode == 0
    assert list((tmp_path / "vault/facts").glob("*.md")), "child must exit before delivery"
    p = make_provider(tmp_path)
    try:
        assert p._disk_worker.drain(3)
        assert p._index_worker.drain(3)
        assert p._mirror_state()["records"] == {}
        assert not list((p._vault / "facts").glob("*.md"))
    finally:
        p.shutdown()
