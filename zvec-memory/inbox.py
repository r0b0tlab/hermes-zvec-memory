"""Durable FIFO notifications, independent of the long mirror/index lock."""
import json
import os
import sqlite3
from contextlib import contextmanager


class MirrorInbox:
    def __init__(self, vault):
        self.path = vault / ".mirror-inbox.sqlite3"
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        os.close(fd)
        with self.connect() as db:
            db.execute("CREATE TABLE IF NOT EXISTS notifications (id INTEGER PRIMARY KEY AUTOINCREMENT, payload TEXT NOT NULL)")

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=0.2)
        try:
            with db:
                yield db
        finally:
            db.close()

    def append(self, payload):
        with self.connect() as db:
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
        with self.connect() as db:
            db.execute("DELETE FROM notifications WHERE id = ?", (number,))
