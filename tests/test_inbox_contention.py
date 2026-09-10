"""Real process contention: readers must not consume admission's lock budget."""
import multiprocessing
import sqlite3
import time

import pytest

from test_provider import _mod, make_provider

MirrorInbox = _mod.MirrorInbox


def test_shutdown_releases_idle_connection(tmp_path):
    provider = make_provider(tmp_path)
    anchor = provider._mirror_inbox._anchor
    provider.shutdown()
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        anchor.execute("SELECT 1")
    provider.shutdown()  # idempotent, including guard cleanup


def test_existing_rollback_inbox_migrates_without_losing_fifo(tmp_path):
    path = tmp_path / ".mirror-inbox.sqlite3"
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE notifications (id INTEGER PRIMARY KEY AUTOINCREMENT, payload TEXT NOT NULL)")
    db.execute("INSERT INTO notifications(payload) VALUES ('[\"legacy\"]')")
    db.commit()
    assert db.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
    db.close()
    inbox = MirrorInbox(tmp_path)
    assert inbox.first() == (1, ["legacy"])
    inbox.append(["new"])
    inbox.acknowledge(1)
    assert inbox.first() == (2, ["new"])
    inbox.close()
    assert MirrorInbox(tmp_path).first() == (2, ["new"])


def _hold_transaction(path, writer, entered, release):
    db = sqlite3.connect(path)
    try:
        db.execute("BEGIN IMMEDIATE" if writer else "BEGIN")
        db.execute("SELECT * FROM notifications").fetchall()
        entered.set()
        assert release.wait(10)
    finally:
        db.rollback()
        db.close()


def _read_empty(path, entered, release):
    inbox = MirrorInbox(path)
    entered.set()
    while not release.is_set():
        assert inbox.first() is None


def test_empty_discovery_does_not_contend_with_peer_readers(tmp_path):
    inbox = MirrorInbox(tmp_path)
    ctx = multiprocessing.get_context("spawn")
    entered, release = ctx.Event(), ctx.Event()
    peer = ctx.Process(target=_read_empty, args=(tmp_path, entered, release))
    peer.start()
    try:
        assert entered.wait(5)
        false_pending = sum(inbox.pending() for _ in range(1000))
        assert false_pending == 0
    finally:
        release.set()
        peer.join(5)
        if peer.is_alive():
            peer.kill()
            peer.join()
        assert peer.exitcode == 0


@pytest.mark.parametrize("writer", [False, True])
def test_peer_transaction_preserves_admission_and_busy_bound(tmp_path, writer):
    inbox = MirrorInbox(tmp_path)
    inbox.append(["seed"])
    ctx = multiprocessing.get_context("spawn")
    entered, release = ctx.Event(), ctx.Event()
    peer = ctx.Process(target=_hold_transaction,
                       args=(str(inbox.path), writer, entered, release))
    peer.start()
    try:
        assert entered.wait(5)
        started = time.monotonic()
        if writer:
            with pytest.raises(sqlite3.OperationalError, match="locked"):
                inbox.append(["blocked"])
            elapsed = time.monotonic() - started
            assert 0.15 <= elapsed < 0.5  # .2s busy timeout, scheduling allowance
        else:
            inbox.append(["accepted"])
            elapsed = time.monotonic() - started
            assert elapsed < 0.2
        print(f"peer_writer={writer} append_seconds={elapsed:.6f}")
    finally:
        release.set()
        peer.join(5)
        if peer.is_alive():
            peer.kill()
            peer.join()
        assert peer.exitcode == 0
    # Reopen after the competing process exits; successful admissions survive,
    # rejected admissions do not appear, and FIFO/acknowledgement stay intact.
    reopened = MirrorInbox(tmp_path)
    delivered = []
    while (item := reopened.first()) is not None:
        number, payload = item
        delivered.append(payload)
        reopened.acknowledge(number)
    assert delivered == ([["seed"]] if writer else [["seed"], ["accepted"]])
    with reopened.connect() as db:
        assert db.execute("PRAGMA synchronous").fetchone()[0] == 2
        assert db.execute("PRAGMA busy_timeout").fetchone()[0] == 200
    assert not reopened.pending()
