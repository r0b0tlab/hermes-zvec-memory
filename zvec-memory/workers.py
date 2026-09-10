"""Bounded FIFO execution with captured caller context and honest shutdown."""
from contextvars import copy_context
from queue import Queue, Empty, Full
from threading import Event, Lock, Thread
import logging

log = logging.getLogger(__name__)


class Worker:
    def __init__(self, name, capacity=256):
        self._queue = Queue(maxsize=capacity)
        self._lock = Lock()
        self._closed = False
        self._thread = Thread(target=self._run, name=name, daemon=True)
        self._thread.start()

    def submit(self, fn, *args):
        with self._lock:
            if self._closed:
                return False
            try:
                self._queue.put_nowait((copy_context(), fn, args))
            except Full:
                return False
            return True

    def _run(self):
        while True:
            # A closed, full queue still drains: shutdown needs no sentinel slot.
            try:
                item = self._queue.get(timeout=0.05)
            except Empty:
                with self._lock:
                    if self._closed and self._queue.empty():
                        return
                continue
            context, fn, args = item
            try:
                context.run(fn, *args)
            except Exception:
                log.exception("zvec-memory background task failed")
            finally:
                self._queue.task_done()

    def drain(self, timeout=5):
        done = Event()
        return self.submit(done.set) and done.wait(timeout)

    def close(self, timeout=5):
        with self._lock:
            self._closed = True
        self._thread.join(max(0, timeout))
        return not self._thread.is_alive()
