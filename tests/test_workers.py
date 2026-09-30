from contextvars import ContextVar
from threading import Event, Thread
import time

from test_provider import _mod


def test_worker_fifo_context_and_shutdown():
    assert hasattr(_mod, "Worker"), "bounded FIFO worker is missing"
    worker = _mod.Worker("test", capacity=4)
    entered, release = Event(), Event()
    output = []
    context = ContextVar("test", default="missing")
    context.set("captured")

    def first():
        entered.set()
        assert release.wait(2)
        output.append((1, context.get()))

    try:
        assert worker.submit(first)
        assert entered.wait(2)
        context.set("later")
        assert worker.submit(lambda: output.append((2, context.get())))
        assert worker.submit(lambda: output.append((3, context.get())))
        assert worker.close(timeout=0) is False
        assert worker.submit(lambda: None) is False
        release.set()
        assert worker.close(timeout=2)
        assert output == [(1, "captured"), (2, "later"), (3, "later")]
    finally:
        release.set()
        worker.close(2)


def test_worker_saturation_rejects_and_closed_queue_drains(caplog):
    worker = _mod.Worker("saturated", capacity=1)
    entered, release = Event(), Event()
    output = []
    def block():
        entered.set()
        assert release.wait(2)
    try:
        assert worker.submit(block)
        assert entered.wait(2)
        assert worker.submit(output.append, 1)
        assert not worker.submit(output.append, 2)
        assert not worker.close(0)
        assert not worker.submit(output.append, 3)
        release.set()
        assert worker.close(2)
        assert output == [1]
    finally:
        release.set()
        worker.close(2)


def test_worker_continues_after_task_failure(caplog):
    worker = _mod.Worker("failure")
    output = []
    def fail():
        raise ValueError("expected fault")
    try:
        assert worker.submit(fail)
        assert worker.submit(output.append, 1)
        assert worker.drain(2)
        assert output == [1]
        assert "background task failed" in caplog.text
    finally:
        worker.close(2)


def test_drain_waits_for_continuation_of_owned_work():
    worker = _mod.Worker("continuation")
    draining, entered, release, done = Event(), Event(), Event(), Event()
    result = []
    def continuation():
        entered.set()
        assert release.wait(2)
    def first():
        assert draining.wait(2)
        time.sleep(0.05)
        assert worker.submit(continuation)
    def drain():
        draining.set()
        result.append(worker.drain(2))
        done.set()
    thread = Thread(target=drain)
    try:
        assert worker.submit(first)
        thread.start()
        assert entered.wait(2)
        assert not done.wait(0.1), "sentinel cannot overtake a task's continuation"
        release.set()
        thread.join(2)
        assert result == [True]
    finally:
        release.set()
        thread.join(2)
        worker.close(2)
