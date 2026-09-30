"""Actual independent-process regressions; no native engine or profile writes."""
import multiprocessing
import time
import fcntl
import os
import sys
import subprocess
import pytest
from pathlib import Path

from test_provider import ZvecMemoryProvider, make_provider

_REAL_RUN_ZG = ZvecMemoryProvider._run_zg


@pytest.mark.parametrize("code,expected", [
    ("import os; os.write(1, b'\\xffout'); os.write(2, b'\\xfeerr')", (0, "�out", "�err")),
    ("import sys; print('normal'); print('failed', file=sys.stderr); sys.exit(3)", (3, "normal\n", "failed\n")),
    ("import time; time.sleep(2)", (124, "", "zg timed out")),
])
def test_real_runner_normalizes_streams_and_status(tmp_path, monkeypatch, code, expected):
    p = ZvecMemoryProvider({})
    p._vault = tmp_path
    monkeypatch.setattr(p, "_zg", lambda: sys.executable)
    assert _REAL_RUN_ZG(p, ["-I", "-c", code], timeout=0.2) == expected


def test_real_runner_reads_eof_not_inherited_input(tmp_path, monkeypatch):
    p = ZvecMemoryProvider({})
    p._vault = tmp_path
    monkeypatch.setattr(p, "_zg", lambda: sys.executable)
    read, write = os.pipe()
    os.write(write, b"must not reach child")
    os.close(write)
    real = subprocess.run
    def inherited(*args, **kwargs):
        kwargs.setdefault("stdin", read)
        return real(*args, **kwargs)
    monkeypatch.setattr(subprocess, "run", inherited)
    try:
        assert _REAL_RUN_ZG(p, ["-I", "-c", "import sys; print(len(sys.stdin.buffer.read()))"], 2) == (0, "0\n", "")
    finally:
        os.close(read)


@pytest.mark.parametrize("kind", ["missing", "not-executable", "directory"])
def test_real_runner_execution_errors_are_values(tmp_path, monkeypatch, kind):
    p = ZvecMemoryProvider({})
    p._vault = tmp_path
    binary = tmp_path / "fixture"
    if kind == "not-executable":
        binary.write_text("fixture")
        binary.chmod(0o600)
    elif kind == "directory":
        binary.mkdir()
    monkeypatch.setattr(p, "_zg", lambda: str(binary))
    rc, out, err = _REAL_RUN_ZG(p, [], 1)
    assert rc == 127 and out == "" and "zg" in err


@pytest.mark.parametrize("stream", [1, 2])
def test_real_runner_finite_multibyte_flood_without_newlines(tmp_path, monkeypatch, stream):
    p = ZvecMemoryProvider({})
    p._vault = tmp_path
    monkeypatch.setattr(p, "_zg", lambda: sys.executable)
    # Bounded fixture, not a claim that capture_output has a runtime byte cap.
    code = f"import os; os.write({stream}, (b'\\xff'+bytes([0xe2,0x98,0x83]))*32768)"
    rc, out, err = _REAL_RUN_ZG(p, ["-I", "-c", code], 2)
    assert rc == 0
    assert (out if stream == 1 else err) == "�☃" * 32768
    assert (err if stream == 1 else out) == ""


def test_real_runner_descendant_gap_is_measured_and_fixture_reaped(tmp_path, monkeypatch, request):
    """Evidence of a LIMITATION: timeout cleans the leader, not descendants.

    An owned subreaper in the test (not product code) adopts/reaps the bounded
    fixture; no arbitrary process-group signalling or system supervisor changes.
    """
    import ctypes
    import json
    import signal
    libc = ctypes.CDLL(None, use_errno=True)
    previous = ctypes.c_int()
    assert libc.prctl(37, ctypes.byref(previous), 0, 0, 0) == 0  # GET_CHILD_SUBREAPER
    assert libc.prctl(36, 1, 0, 0, 0) == 0  # SET_CHILD_SUBREAPER
    p = ZvecMemoryProvider({})
    p._vault = tmp_path
    monkeypatch.setattr(p, "_zg", lambda: sys.executable)
    identity = tmp_path / "descendant.pid"
    code = ("import os,time,pathlib; pid=os.fork(); "
            f"pathlib.Path({str(identity)!r}).write_text(str(pid)) if pid else None; "
            "os._exit(0) if pid else None; time.sleep(1); os._exit(0)")
    pidfd = None
    pid = None
    reaped = False
    started = time.monotonic()
    try:
        rc, out, err = _REAL_RUN_ZG(p, ["-I", "-c", code], 0.1)
        elapsed = time.monotonic() - started
        pid = int(identity.read_text())
        assert pid > 0
        pidfd = os.pidfd_open(pid)
        running = os.waitid(os.P_PID, pid, os.WEXITED | os.WNOWAIT | os.WNOHANG) is None
        evidence = {"rc": rc, "elapsed_seconds": elapsed, "descendant_alive_at_return": running,
                    "scope": "known descendant cleanup gap, not a containment pass"}
        request.node.user_properties.append(("process_boundary_observation", json.dumps(evidence)))
        (tmp_path / "process-boundary-observation.json").write_text(json.dumps(evidence))
        assert rc == 124 and running
    finally:
        if pid is not None:
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                if os.waitpid(pid, os.WNOHANG)[0] == pid:
                    reaped = True
                    break
                time.sleep(0.01)
            if not reaped and pidfd is not None:
                signal.pidfd_send_signal(pidfd, signal.SIGKILL)
                deadline = time.monotonic() + 2
                while time.monotonic() < deadline:
                    if os.waitpid(pid, os.WNOHANG)[0] == pid:
                        reaped = True
                        break
                    time.sleep(0.01)
        if pidfd is not None:
            os.close(pidfd)
        assert libc.prctl(36, previous.value, 0, 0, 0) == 0
        assert reaped, "test-owned bounded descendant must be reaped before returning"
        request.node.user_properties.append(("fixture_reaped", "true"))


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
