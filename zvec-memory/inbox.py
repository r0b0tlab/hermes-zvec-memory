"""Durable FIFO notifications, independent of the long mirror/index lock."""
import json
import os
import random
import sqlite3
import time
from contextlib import contextmanager


class MirrorInbox:
    def __init__(self, vault):
        self.path = vault / ".mirror-inbox.sqlite3"
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        os.close(fd)
        with self.connect() as db:
            # Readers must not block notification commits. WAL still serializes
            # writers; keep the bounded busy timeout and FULL commit durability.
            if db.execute("PRAGMA journal_mode=WAL").fetchone()[0].lower() != "wal":
                raise sqlite3.OperationalError("mirror inbox requires WAL journal mode")
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

    def close(self):
        anchor = getattr(self, "_anchor", None)
        self._anchor = None
        if anchor is not None:
            anchor.close()

    def __del__(self):
        self.close()

    @contextmanager
    def connect(self, *, writer=False):
        deadline = time.monotonic() + 0.2
        db = sqlite3.connect(self.path, timeout=0 if writer else 0.2)
        try:
            db.execute("PRAGMA synchronous=FULL")
            with db:
                if writer:
                    # SQLite's increasing busy sleeps can miss short vacancies
                    # during append/ack churn. Retry only reservation, with short
                    # desynchronized waits and one shared 200ms admission budget.
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
                # Mutation and FULL commit are performed once. Never replay an
                # I/O or ambiguous commit failure as though admission had failed.
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

    def first(self):
        with self.connect() as db:
            row = db.execute("SELECT id, payload FROM notifications ORDER BY id LIMIT 1").fetchone()
            return (row[0], json.loads(row[1])) if row else None

    def acknowledge(self, number):
        with self.connect(writer=True) as db:
            db.execute("DELETE FROM notifications WHERE id = ?", (number,))
