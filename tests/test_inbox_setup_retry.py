"""Regression: transient SQLite contention before the writer transaction."""
import sqlite3

import pytest

from test_provider import _mod


@pytest.mark.parametrize("failure_code", [sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED])
def test_synchronous_setup_retries_before_any_mutation(tmp_path, monkeypatch, failure_code):
    inbox = _mod.MirrorInbox(tmp_path)
    original_connect = sqlite3.connect
    seen = []

    class Contended(sqlite3.Connection):
        def execute(self, sql, *args):
            seen.append(sql)
            if sql == "PRAGMA synchronous=FULL" and seen.count(sql) <= 2:
                failure = sqlite3.OperationalError("injected transient setup contention")
                failure.sqlite_errorcode = failure_code
                raise failure
            return super().execute(sql, *args)

    monkeypatch.setattr(sqlite3, "connect", lambda *a, **kw: original_connect(*a, factory=Contended, **kw))
    try:
        inbox.append(["single-record"])
        assert seen.count("PRAGMA synchronous=FULL") == 3
        assert sum(sql.startswith("INSERT") for sql in seen) == 1
        assert inbox.first()[1] == ["single-record"]
    finally:
        inbox.close()


def test_synchronous_setup_io_error_is_not_retried(tmp_path, monkeypatch):
    inbox = _mod.MirrorInbox(tmp_path)
    original_connect = sqlite3.connect
    seen = []
    failure = sqlite3.OperationalError("injected io error")
    failure.sqlite_errorcode = sqlite3.SQLITE_IOERR

    class Fault(sqlite3.Connection):
        def execute(self, sql, *args):
            seen.append(sql)
            if sql == "PRAGMA synchronous=FULL":
                raise failure
            return super().execute(sql, *args)

    monkeypatch.setattr(sqlite3, "connect", lambda *a, **kw: original_connect(*a, factory=Fault, **kw))
    try:
        with pytest.raises(sqlite3.OperationalError) as caught:
            inbox.append(["not-written"])
        assert caught.value is failure
        assert seen == ["PRAGMA synchronous=FULL"]
    finally:
        inbox.close()
