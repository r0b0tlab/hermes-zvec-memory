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


@pytest.mark.parametrize("operation", ["append", "acknowledge"])
def test_writer_reserves_before_mutation_without_sqlite_busy_wait(tmp_path, monkeypatch, operation):
    inbox = MirrorInbox(tmp_path)
    inbox.append(["seed"])
    statements = []
    timeouts = []
    connect = sqlite3.connect

    def traced_connect(*args, **kwargs):
        db = connect(*args, **kwargs)
        timeouts.append(db.execute("PRAGMA busy_timeout").fetchone()[0])
        db.set_trace_callback(statements.append)
        return db

    monkeypatch.setattr(sqlite3, "connect", traced_connect)
    getattr(inbox, operation)(["new"] if operation == "append" else 1)
    assert "BEGIN IMMEDIATE" in statements
    mutation = next(i for i, sql in enumerate(statements)
                    if sql.startswith(("INSERT", "DELETE")))
    assert statements.index("BEGIN IMMEDIATE") < mutation
    assert timeouts == [0]
    assert "PRAGMA synchronous=FULL" in statements
    assert statements.count("COMMIT") == 1


@pytest.mark.parametrize("stage,code", [("BEGIN IMMEDIATE", sqlite3.SQLITE_IOERR),
                                        ("INSERT", sqlite3.SQLITE_BUSY),
                                        ("COMMIT", sqlite3.SQLITE_IOERR)])
def test_writer_does_not_replay_mutation_or_uncertain_commit(tmp_path, monkeypatch, stage, code):
    inbox = MirrorInbox(tmp_path)
    attempts = []
    connect = sqlite3.connect
    failure = sqlite3.OperationalError("injected failure")
    failure.sqlite_errorcode = code

    class FaultConnection(sqlite3.Connection):
        def execute(self, sql, *args):
            attempts.append(sql)
            if sql.startswith(stage):
                raise failure
            return super().execute(sql, *args)

        def __exit__(self, *args):
            if stage == "COMMIT" and args[0] is None:
                attempts.append("COMMIT")
                super().__exit__(*args)  # commit succeeded but receipt is lost
                raise failure
            return super().__exit__(*args)

    monkeypatch.setattr(sqlite3, "connect", lambda *a, **kw: connect(*a, factory=FaultConnection, **kw))
    with pytest.raises(sqlite3.OperationalError) as caught:
        inbox.append(["once"])
    assert caught.value is failure
    assert sum(sql.startswith(stage) for sql in attempts) == 1
    with connect(inbox.path) as db:
        assert db.execute("SELECT count(*) FROM notifications").fetchone()[0] == (stage == "COMMIT")


def _burst_writer(path, ready, start, results, identity):
    inbox = MirrorInbox(path)
    ready.put(identity)
    assert start.wait(10)
    durations, errors = [], []
    for number in range(100):
        began = time.monotonic()
        try:
            inbox.append([identity, number])
        except sqlite3.OperationalError as exc:
            errors.append(str(exc))
        durations.append(time.monotonic() - began)
        # Acknowledge churn competes for the same writer slot as callbacks.
        item = inbox.first()
        if item:
            inbox.acknowledge(item[0])
    inbox.close()
    results.put((identity, durations, errors))


def test_two_process_burst_admission(tmp_path):
    inbox = MirrorInbox(tmp_path)
    ctx = multiprocessing.get_context("spawn")
    ready, results, start = ctx.Queue(), ctx.Queue(), ctx.Event()
    peers = [ctx.Process(target=_burst_writer,
                         args=(tmp_path, ready, start, results, i)) for i in range(2)]
    for peer in peers:
        peer.start()
    try:
        for _ in peers:
            ready.get(timeout=10)
        start.set()
        samples = [results.get(timeout=20) for _ in peers]
        durations = sorted(t for _, times, _ in samples for t in times)
        errors = [e for _, _, failures in samples for e in failures]
        print(f"burst admitted={len(durations) - len(errors)}/200 "
              f"p50={durations[99]:.6f} p95={durations[189]:.6f} "
              f"max={max(durations):.6f} errors={errors}")
        assert not errors
        assert max(durations) < 0.25
    finally:
        start.set()
        for peer in peers:
            peer.join(5)
            if peer.is_alive():
                peer.kill()
                peer.join()
            assert peer.exitcode == 0
        inbox.close()


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


def test_wal_conversion_waits_for_a_concurrent_holder(tmp_path, monkeypatch):
    """A peer holding the journal-conversion lock must not kill a second opener.

    Reproduces the native two-process failure: process A converts a fresh inbox
    to WAL while process B runs ``PRAGMA journal_mode=WAL`` against the same
    file, which needs a short exclusive lock. B must wait for it, not fail.
    """
    import threading
    monkeypatch.setattr(MirrorInbox, "WAL_CONVERSION_SECONDS", 3.0)
    path = tmp_path / ".mirror-inbox.sqlite3"
    holder = sqlite3.connect(path, timeout=5, check_same_thread=False)
    holder.execute("CREATE TABLE IF NOT EXISTS notifications "
                   "(id INTEGER PRIMARY KEY AUTOINCREMENT, payload TEXT NOT NULL)")
    holder.commit()
    holder.execute("BEGIN IMMEDIATE")

    def release():
        time.sleep(0.4)
        holder.rollback()

    thread = threading.Thread(target=release)
    thread.start()
    try:
        inbox = MirrorInbox(tmp_path)
        assert inbox.first() is None
        assert inbox._anchor.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
        inbox.close()
    finally:
        thread.join()
        holder.close()


def test_wal_conversion_is_bounded_when_the_holder_never_releases(tmp_path, monkeypatch):
    path = tmp_path / ".mirror-inbox.sqlite3"
    holder = sqlite3.connect(path, timeout=5)
    holder.execute("CREATE TABLE IF NOT EXISTS notifications "
                   "(id INTEGER PRIMARY KEY AUTOINCREMENT, payload TEXT NOT NULL)")
    holder.commit()
    holder.execute("BEGIN IMMEDIATE")
    monkeypatch.setattr(MirrorInbox, "WAL_CONVERSION_SECONDS", 0.3)
    try:
        with pytest.raises(sqlite3.OperationalError, match="WAL"):
            MirrorInbox(tmp_path)
    finally:
        holder.rollback()
        holder.close()
