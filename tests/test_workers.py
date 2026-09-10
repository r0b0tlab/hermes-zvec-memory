from contextvars import ContextVar
from threading import Event

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
