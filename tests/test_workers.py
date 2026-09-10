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
