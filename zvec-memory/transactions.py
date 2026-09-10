"""Reentrant per-vault transactions across threads and POSIX processes."""
import os
import threading


class VaultLock:
    def __init__(self, path):
        if os.name != "posix":
            raise RuntimeError("zvec-memory requires POSIX flock (Linux/macOS); Windows is unsupported")
        self.path = path
        self._thread_lock = threading.RLock()
        self._depth = 0
        self._fd = None

    def __enter__(self):
        self.acquire()
        return self

    def acquire(self, blocking=True):
        import fcntl
        if not self._thread_lock.acquire(blocking=blocking):
            return False
        try:
            if self._depth == 0:
                fd = os.open(self.path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
                except BlockingIOError:
                    os.close(fd)
                    self._thread_lock.release()
                    return False
                except BaseException:
                    os.close(fd)
                    raise
                self._fd = fd
            self._depth += 1
            return self
        except BaseException:
            self._thread_lock.release()
            raise

    def __exit__(self, *exc):
        import fcntl
        try:
            self._depth -= 1
            if self._depth == 0:
                assert self._fd is not None
                try:
                    fcntl.flock(self._fd, fcntl.LOCK_UN)
                finally:
                    os.close(self._fd)
                    self._fd = None
        finally:
            self._thread_lock.release()
