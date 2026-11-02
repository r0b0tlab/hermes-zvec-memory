"""Durable FIFO notifications, independent of the long mirror/index lock."""
import fcntl
import json
import os
import random
import sqlite3
import threading
import time
from contextlib import contextmanager, nullcontext


def _admission_busy():
    error = sqlite3.OperationalError("database is locked (inbox admission)")
    error.sqlite_errorcode = sqlite3.SQLITE_BUSY
    error.sqlite_errorname = "SQLITE_BUSY"
    return error


# Share one registry across provider module aliases and reloads, not one callback
# per writer. The mutex makes descriptor
# open/registration and removal/close atomic with respect to fork, including
# forks from a different thread while os.open/os.close have released the GIL.
# Each mutable handle is invalidated in the child so a later context exit
# cannot close an unrelated descriptor which reused the same number.
if "_zvec_memory_inbox_turnstile_registry_v1" not in os.__dict__:
    class _TurnstileRegistry:
        def __init__(self):
            self.lock = threading.Lock()
            self.handles = []
            os.register_at_fork(before=self.lock.acquire,
                                after_in_parent=self.lock.release,
                                after_in_child=self._after_fork_child)

        def _after_fork_child(self):
            try:
                for handle in self.handles:
                    fd, handle[0] = handle[0], None
                    if fd is not None:
                        os.close(fd)  # never LOCK_UN: that would unlock the parent
                self.handles.clear()
            finally:
                self.lock.release()

        def open(self, path, deadline):
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not self.lock.acquire(timeout=remaining):
                raise _admission_busy()
            try:
                if time.monotonic() >= deadline:
                    raise _admission_busy()
                fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
                handle = [fd]
                try:
                    self.handles.append(handle)
                except BaseException:
                    os.close(fd)
                    raise
                return handle
            finally:
                self.lock.release()

        def close(self, handle):
            with self.lock:
                fd, handle[0] = handle[0], None
                if fd is not None:
                    self.handles.remove(handle)
                    os.close(fd)

    # Distinct module aliases can race their first imports. Each candidate is
    # fully registered before publication, so even a fork during bootstrap is
    # safe; at most those concurrent bootstrap candidates add empty callbacks.
    os.__dict__.setdefault("_zvec_memory_inbox_turnstile_registry_v1", _TurnstileRegistry())

_turnstile_registry = os.__dict__["_zvec_memory_inbox_turnstile_registry_v1"]


class MirrorInbox:
    # A peer converting the same fresh database holds the journal-conversion
    # lock for milliseconds; waiting briefly for it is correct, failing closed
    # is not. The bound stays well inside the provider's startup budget: a
    # persistently contended inbox must still defer recovery quickly.
    WAL_CONVERSION_SECONDS = 0.4

    def __init__(self, vault):
        self.path = vault / ".mirror-inbox.sqlite3"
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        os.close(fd)
        self._enable_wal()
        # The table is created through the documented writer admission path so
        # a peer's brief write reservation cannot fail a second opener either.
        with self.connect(writer=True) as db:
            db.execute("CREATE TABLE IF NOT EXISTS notifications (id INTEGER PRIMARY KEY AUTOINCREMENT, payload TEXT NOT NULL)")
        # Keep WAL attached between short-lived operation connections. The
        # last connection's cleanup takes an exclusive lock, otherwise even
        # two readers can make nonwaiting discovery spuriously fail closed.
        anchor = sqlite3.connect(self.path, timeout=0.2, check_same_thread=False)
        try:
            anchor.execute("SELECT 1 FROM notifications LIMIT 1").fetchone()
        except BaseException:
            anchor.close()
            raise
        self._anchor = anchor

    def _enable_wal(self):
        """Convert the inbox to WAL once, tolerating a peer that holds the lock.

        ``PRAGMA journal_mode=WAL`` needs a short exclusive lock while a fresh
        database is converted, so two processes starting together would
        otherwise have one of them fail closed. The mode is persistent: an
        already-WAL database needs no lock at all, and a contended conversion
        is retried under a bounded deadline instead of an immediate failure.
        """
        deadline = time.monotonic() + self.WAL_CONVERSION_SECONDS
        db = sqlite3.connect(self.path, timeout=self.WAL_CONVERSION_SECONDS)
        try:
            while True:
                try:
                    if db.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal":
                        return
                    if db.execute("PRAGMA journal_mode=WAL").fetchone()[0].lower() == "wal":
                        return
                except sqlite3.OperationalError as exc:
                    if getattr(exc, "sqlite_errorcode", None) not in (sqlite3.SQLITE_BUSY,
                                                                    sqlite3.SQLITE_LOCKED):
                        raise
                if time.monotonic() >= deadline:
                    raise sqlite3.OperationalError("mirror inbox requires WAL journal mode")
                time.sleep(random.uniform(0.005, 0.02))
        finally:
            db.close()

    def close(self):
        anchor = getattr(self, "_anchor", None)
        self._anchor = None
        if anchor is not None:
            anchor.close()

    def __del__(self):
        self.close()

    @contextmanager
    def _writer_turnstile(self, deadline):
        # Persistent inode: never unlink this file while any inbox can be open.
        # Separate from SQLite's fcntl locks and from the long mirror lock.
        path = self.path.with_name(".mirror-inbox-admission.lock")
        handle = _turnstile_registry.open(path, deadline)
        try:
            while True:
                if handle[0] is None:
                    raise RuntimeError("inbox admission cannot continue in a fork child")
                if time.monotonic() >= deadline:
                    raise _admission_busy()
                try:
                    fcntl.flock(handle[0], fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise _admission_busy()
                    time.sleep(min(remaining, random.uniform(0.001, 0.003)))
            # A contender retains this gate while waiting for SQLite, so a
            # previous winner cannot immediately barge back in front of it.
            # flock polling itself is not FIFO among multiple gate waiters.
            if handle[0] is None:
                raise RuntimeError("inbox admission cannot continue in a fork child")
            yield
        finally:
            _turnstile_registry.close(handle)

    @contextmanager
    def connect(self, *, writer=False):
        deadline = time.monotonic() + 0.2
        db = sqlite3.connect(self.path, timeout=0 if writer else 0.2)
        try:
            # The same deadline covers turnstile, setup and SQLite reservation.
            # Release the gate immediately after BEGIN, not after the commit:
            # a waiting peer can then hold it while the current writer finishes.
            with self._writer_turnstile(deadline) if writer else nullcontext():
                while True:
                    try:
                        db.execute("PRAGMA synchronous=FULL")
                        break
                    except sqlite3.OperationalError as exc:
                        if getattr(exc, "sqlite_errorcode", None) not in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED):
                            raise
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            raise
                        time.sleep(min(remaining, random.uniform(0.001, 0.003)))
                        if time.monotonic() >= deadline:
                            raise
                if writer:
                    if time.monotonic() >= deadline:
                        raise _admission_busy()
                    while True:
                        try:
                            db.execute("BEGIN IMMEDIATE")
                            break
                        except sqlite3.OperationalError as exc:
                            if getattr(exc, "sqlite_errorcode", None) != sqlite3.SQLITE_BUSY:
                                raise
                            remaining = deadline - time.monotonic()
                            if remaining <= 0:
                                raise
                            time.sleep(min(remaining, random.uniform(0.001, 0.003)))
                            if time.monotonic() >= deadline:
                                raise
            # Even a successful BEGIN or gate close can finish after the
            # admission deadline. Roll back before exposing any mutation.
            if writer and time.monotonic() >= deadline:
                db.rollback()
                raise _admission_busy()
            # Mutation and FULL commit are performed once. Never replay an
            # I/O or ambiguous commit failure as though admission had failed.
            with db:
                yield db
        finally:
            db.close()

    def append(self, payload):
        with self.connect(writer=True) as db:
            db.execute("INSERT INTO notifications(payload) VALUES (?)", (json.dumps(payload),))

    def pending(self):
        """Nonwaiting discovery; a busy inbox is itself a recovery obligation."""
        db = sqlite3.connect(self.path, timeout=0)
        try:
            return db.execute("SELECT 1 FROM notifications LIMIT 1").fetchone() is not None
        except sqlite3.OperationalError:
            return True
        finally:
            db.close()

    def high_watermark(self, *, nonblocking=False):
        """Highest allocated identity, including already acknowledged rows."""
        if nonblocking:
            db = sqlite3.connect(self.path, timeout=0)
            try:
                row = db.execute("SELECT seq FROM sqlite_sequence WHERE name = 'notifications'").fetchone()
                return int(row[0]) if row else 0
            finally:
                db.close()
        with self.connect() as db:
            row = db.execute("SELECT seq FROM sqlite_sequence WHERE name = 'notifications'").fetchone()
            return int(row[0]) if row else 0

    def first(self):
        with self.connect() as db:
            row = db.execute("SELECT id, payload FROM notifications ORDER BY id LIMIT 1").fetchone()
            return (row[0], json.loads(row[1])) if row else None

    def acknowledge(self, number):
        with self.connect(writer=True) as db:
            db.execute("DELETE FROM notifications WHERE id = ?", (number,))
