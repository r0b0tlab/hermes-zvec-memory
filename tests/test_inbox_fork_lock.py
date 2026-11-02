"""Fork, crash, and deadline controls for the inbox admission gate."""
import fcntl
import multiprocessing
import os
import select
import signal
import sqlite3
import threading
import time

import pytest

from test_inbox_contention import MirrorInbox


def _recv(fd, timeout=5):
    assert select.select([fd], [], [], timeout)[0], "timed out waiting for process signal"
    data = os.read(fd, 1024)
    assert data
    return data


def _forking_holder(path, ready, control):
    inbox = MirrorInbox(path)
    with inbox._writer_turnstile(time.monotonic() + 2):
        child = os.fork()
        if child == 0:
            os.write(ready, b'child-ready\n')
            os.read(control, 1)
            os.write(ready, b'child-alive\n')
            os.read(control, 1)
            os._exit(0)
        os.write(ready, f'parent-ready {child}\n'.encode())
        while True:
            signal.pause()


def test_parent_crash_releases_gate_with_live_fork_child(tmp_path):
    inbox = MirrorInbox(tmp_path)
    ready_r, ready_w = os.pipe()
    ctrl_r, ctrl_w = os.pipe()
    # fork makes the controlled pipe descriptors available to the process.
    proc = multiprocessing.get_context('fork').Process(target=_forking_holder,
                                                        args=(tmp_path, ready_w, ctrl_r))
    proc.start()
    os.close(ready_w)
    os.close(ctrl_r)
    child = None
    try:
        data = b''
        while b'parent-ready ' not in data or b'child-ready\n' not in data:
            data += _recv(ready_r)
        child = int(data.split(b'parent-ready ')[1].splitlines()[0])
        # Negative control: the parent is definitely holding the gate.
        with pytest.raises(sqlite3.OperationalError) as caught:
            inbox.append(['while-parent-alive'])
        assert caught.value.sqlite_errorcode == sqlite3.SQLITE_BUSY
        assert proc.is_alive()
        os.kill(child, 0)
        proc.kill()
        proc.join(5)
        assert proc.exitcode == -signal.SIGKILL
        os.kill(child, 0)
        inbox.append(['after-parent-death'])
        os.write(ctrl_w, b'x')
        assert b'child-alive' in _recv(ready_r)
        assert inbox.first()[1] == ['after-parent-death']
    finally:
        if proc.is_alive():
            proc.kill()
        proc.join(5)
        if child is not None:
            try:
                os.write(ctrl_w, b'x')
            except BrokenPipeError:
                pass
        os.close(ready_r)
        os.close(ctrl_w)
        inbox.close()


def test_fork_context_exit_does_not_close_reused_unrelated_fd(tmp_path):
    inbox = MirrorInbox(tmp_path)
    read_fd, write_fd = os.pipe()
    with inbox._writer_turnstile(time.monotonic() + 2):
        target = str(tmp_path / '.mirror-inbox-admission.lock')
        gate_fd = next(int(name) for name in os.listdir('/proc/self/fd')
                       if os.path.exists(f'/proc/self/fd/{name}')
                       and os.path.realpath(f'/proc/self/fd/{name}') == target)
        pid = os.fork()
        if pid == 0:
            try:
                os.close(read_fd)
                # The inherited gate descriptor has been closed in the child.
                # Open and retain many unrelated descriptors to force reuse.
                fds = [os.open('/dev/null', os.O_RDONLY) for _ in range(32)]
                assert gate_fd in fds, 'child retained the inherited gate fd'
            except BaseException:
                os.write(write_fd, b'gate-not-reused')
                os._exit(2)
        else:
            os.close(write_fd)
    if pid == 0:
        try:
            for fd in fds:
                assert os.fstat(fd).st_rdev == os.stat('/dev/null').st_rdev
            os.write(write_fd, b'ok')
            os._exit(0)
        except BaseException:
            os.write(write_fd, b'foreign-closed')
            os._exit(3)
    try:
        result = _recv(read_fd)
        _, status = os.waitpid(pid, 0)
        assert result == b'ok'
        assert os.waitstatus_to_exitcode(status) == 0
    finally:
        os.close(read_fd)
        inbox.close()


@pytest.mark.parametrize('phase', ['open', 'close'])
def test_fork_is_serialized_with_descriptor_lifecycle(tmp_path, monkeypatch, phase):
    inbox = MirrorInbox(tmp_path)
    import zvec_memory_provider.inbox as module
    original_open, original_close = os.open, os.close
    paused, resume, fork_started, fork_done = (threading.Event() for _ in range(4))
    target = str(tmp_path / '.mirror-inbox-admission.lock')
    errors = []
    acquired_fd = []
    read_fd, write_fd = os.pipe()

    def controlled_open(path, *args, **kwargs):
        fd = original_open(path, *args, **kwargs)
        if str(path) == target:
            acquired_fd.append(fd)
            if phase == 'open':
                paused.set()
                assert resume.wait(5)
        return fd

    def controlled_close(fd):
        if phase == 'close' and acquired_fd and fd == acquired_fd[0]:
            paused.set()
            assert resume.wait(5)
        return original_close(fd)

    monkeypatch.setattr(module.os, 'open', controlled_open)
    monkeypatch.setattr(module.os, 'close', controlled_close)

    def lifecycle():
        try:
            with inbox._writer_turnstile(time.monotonic() + 4):
                pass
        except BaseException as exc:
            errors.append(exc)

    def fork():
        try:
            fork_started.set()
            pid = os.fork()
            if pid == 0:
                os.write(write_fd, b'child')
                os._exit(0)
            _, status = os.waitpid(pid, 0)
            assert os.waitstatus_to_exitcode(status) == 0
            fork_done.set()
        except BaseException as exc:
            errors.append(exc)

    worker = threading.Thread(target=lifecycle)
    forker = threading.Thread(target=fork)
    try:
        worker.start()
        assert paused.wait(5)
        forker.start()
        assert fork_started.wait(5)
        assert not fork_done.wait(0.1), 'fork bypassed lifecycle synchronization'
        resume.set()
        worker.join(5)
        forker.join(5)
        assert not worker.is_alive() and not forker.is_alive()
        assert not errors
        assert _recv(read_fd) == b'child'
    finally:
        resume.set()
        worker.join(5)
        if forker.ident is not None:
            forker.join(5)
        original_close(read_fd)
        original_close(write_fd)
        inbox.close()


def test_successful_begin_after_deadline_is_rolled_back_before_yield(tmp_path, monkeypatch):
    inbox = MirrorInbox(tmp_path)
    original = sqlite3.connect
    statements = []
    class SlowBegin(sqlite3.Connection):
        def execute(self, sql, *args):
            statements.append(sql)
            result = super().execute(sql, *args)
            if sql == 'BEGIN IMMEDIATE':
                time.sleep(0.21)
            return result
        def rollback(self):
            statements.append('ROLLBACK method')
            return super().rollback()
        def __exit__(self, *args):
            statements.append('COMMIT context' if args[0] is None else 'ROLLBACK context')
            return super().__exit__(*args)
    monkeypatch.setattr(sqlite3, 'connect', lambda *a, **kw: original(*a, factory=SlowBegin, **kw))
    try:
        with pytest.raises(sqlite3.OperationalError) as caught:
            inbox.append(['must-not-be-written'])
        assert caught.value.sqlite_errorcode == sqlite3.SQLITE_BUSY
        assert statements.count('BEGIN IMMEDIATE') == 1
        assert 'ROLLBACK method' in statements
        assert 'COMMIT context' not in statements
        assert not any(s.startswith('INSERT') for s in statements)
        with original(inbox.path) as db:
            assert db.execute('SELECT count(*) FROM notifications').fetchone()[0] == 0
    finally:
        inbox.close()


def test_gate_close_exhausting_deadline_rolls_back_without_mutation(tmp_path, monkeypatch):
    inbox = MirrorInbox(tmp_path)
    import zvec_memory_provider.inbox as module
    real_close = os.close
    original_connect = sqlite3.connect
    statements = []

    def traced_connect(*args, **kwargs):
        db = original_connect(*args, **kwargs)
        db.set_trace_callback(statements.append)
        return db

    def delayed_close(fd):
        if os.path.realpath(f'/proc/self/fd/{fd}') == str(tmp_path / '.mirror-inbox-admission.lock'):
            real_close(fd)
            time.sleep(0.21)
            return
        return real_close(fd)

    monkeypatch.setattr(module.os, 'close', delayed_close)
    monkeypatch.setattr(sqlite3, 'connect', traced_connect)
    try:
        with pytest.raises(sqlite3.OperationalError) as caught:
            inbox.append(['rejected'])
        assert caught.value.sqlite_errorcode == sqlite3.SQLITE_BUSY
        assert 'BEGIN IMMEDIATE' in statements
        assert 'ROLLBACK' in statements
        assert 'COMMIT' not in statements
        assert not any(s.startswith('INSERT') for s in statements)
        with original_connect(inbox.path) as db:
            assert db.execute('SELECT count(*) FROM notifications').fetchone()[0] == 0
    finally:
        inbox.close()


@pytest.mark.parametrize('phase', ['before-flock', 'after-flock'])
def test_fork_during_flock_drops_child_fd_but_preserves_parent(tmp_path, monkeypatch, phase):
    inbox = MirrorInbox(tmp_path)
    import zvec_memory_provider.inbox as module
    real_flock = fcntl.flock
    paused, resume = threading.Event(), threading.Event()
    captured, errors = [], []
    read_fd, write_fd = os.pipe()

    def hooked(fd, flags):
        captured.append(fd)
        if phase == 'before-flock':
            paused.set()
            assert resume.wait(5)
        result = real_flock(fd, flags)
        if phase == 'after-flock':
            paused.set()
            assert resume.wait(5)
        return result

    monkeypatch.setattr(module.fcntl, 'flock', hooked)
    def acquire():
        try:
            with inbox._writer_turnstile(time.monotonic() + 4):
                pass
        except BaseException as exc:
            errors.append(exc)

    worker = threading.Thread(target=acquire)
    worker.start()
    try:
        assert paused.wait(5)
        pid = os.fork()
        if pid == 0:
            try:
                try:
                    os.fstat(captured[0])
                except OSError:
                    os.write(write_fd, b'closed')
                    os._exit(0)
                os.write(write_fd, b'gate-still-open')
                os._exit(1)
            except BaseException:
                os._exit(2)
        result = _recv(read_fd)
        _, status = os.waitpid(pid, 0)
        assert result == b'closed'
        assert os.waitstatus_to_exitcode(status) == 0
        contender = os.open(tmp_path / '.mirror-inbox-admission.lock', os.O_RDWR)
        try:
            if phase == 'after-flock':
                with pytest.raises(BlockingIOError):
                    real_flock(contender, fcntl.LOCK_EX | fcntl.LOCK_NB)
            resume.set()
            worker.join(5)
            assert not worker.is_alive() and not errors
            real_flock(contender, fcntl.LOCK_EX | fcntl.LOCK_NB)
        finally:
            os.close(contender)
    finally:
        resume.set()
        worker.join(5)
        os.close(read_fd)
        os.close(write_fd)
        inbox.close()


def test_registry_wait_shares_deadline_and_callbacks_do_not_accumulate(tmp_path, monkeypatch):
    import importlib.util
    import zvec_memory_provider.inbox as module
    inbox = MirrorInbox(tmp_path)
    registrations = []
    monkeypatch.setattr(module.os, 'register_at_fork', lambda **kw: registrations.append(kw))
    for _ in range(100):
        with inbox._writer_turnstile(time.monotonic() + 1):
            pass
    for index in range(30):
        spec = importlib.util.spec_from_file_location(f'inbox_alias_{index}', module.__file__)
        assert spec is not None and spec.loader is not None
        alias = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(alias)
        assert alias._turnstile_registry is module._turnstile_registry
    assert registrations == []
    assert not module._turnstile_registry.handles
    entered, release = threading.Event(), threading.Event()
    def hold():
        with module._turnstile_registry.lock:
            entered.set()
            assert release.wait(5)
    worker = threading.Thread(target=hold)
    worker.start()
    try:
        assert entered.wait(5)
        started = time.monotonic()
        with pytest.raises(sqlite3.OperationalError) as caught:
            inbox.append(['rejected'])
        assert caught.value.sqlite_errorcode == sqlite3.SQLITE_BUSY
        assert 0.15 <= time.monotonic() - started < 0.5
        assert inbox.first() is None
    finally:
        release.set()
        worker.join(5)
        inbox.close()


def test_fork_after_close_preserves_descriptor_reused_by_unrelated_file(tmp_path):
    inbox = MirrorInbox(tmp_path)
    import zvec_memory_provider.inbox as module
    read_fd, write_fd = os.pipe()
    with inbox._writer_turnstile(time.monotonic() + 2):
        gate_fd = module._turnstile_registry.handles[-1][0]
    foreign = os.open('/dev/null', os.O_RDONLY)
    if foreign != gate_fd:
        os.dup2(foreign, gate_fd)
        os.close(foreign)
    try:
        pid = os.fork()
        if pid == 0:
            try:
                assert os.fstat(gate_fd).st_rdev == os.stat('/dev/null').st_rdev
                os.write(write_fd, b'foreign-ok')
                os._exit(0)
            except BaseException:
                os._exit(1)
        assert _recv(read_fd) == b'foreign-ok'
        _, status = os.waitpid(pid, 0)
        assert os.waitstatus_to_exitcode(status) == 0
        assert os.fstat(gate_fd).st_rdev == os.stat('/dev/null').st_rdev
    finally:
        os.close(gate_fd)
        os.close(read_fd)
        os.close(write_fd)
        inbox.close()


def test_fork_drops_all_simultaneously_registered_handles(tmp_path):
    import zvec_memory_provider.inbox as module
    first = tmp_path / 'one'
    second = tmp_path / 'two'
    first.mkdir()
    second.mkdir()
    a, b = MirrorInbox(first), MirrorInbox(second)
    read_fd, write_fd = os.pipe()
    try:
        with a._writer_turnstile(time.monotonic() + 2):
            with b._writer_turnstile(time.monotonic() + 2):
                handles = [h[0] for h in module._turnstile_registry.handles]
                assert len(handles) == 2 and len(set(handles)) == 2
                pid = os.fork()
                if pid == 0:
                    for fd in handles:
                        try:
                            os.fstat(fd)
                        except OSError:
                            continue
                        os.write(write_fd, b'still-open')
                        os._exit(1)
                    os.write(write_fd, b'both-closed')
                    os._exit(0)
                result = _recv(read_fd)
                _, status = os.waitpid(pid, 0)
                assert result == b'both-closed'
                assert os.waitstatus_to_exitcode(status) == 0
                for fd in handles:
                    os.fstat(fd)
        assert not module._turnstile_registry.handles
    finally:
        os.close(read_fd)
        os.close(write_fd)
        a.close()
        b.close()
