# SOURCE-ONLY DRAFT: NOT_EXECUTED. Parent materializes/runs under original pt.sh shared lock/resource caps.
"""Additional fault boundaries of the shared mutation protocol (offline)."""
import json
import threading
from pathlib import Path

import pytest

from test_provider import _mod
from test_core_successor_runtime_review import peers
from zvec_memory_provider import maintenance


@pytest.mark.parametrize("after_write", [False, True])
def test_gate_publication_fault_never_dispatches_native(peers, monkeypatch, after_write):
    p, q, engine = peers
    before = q._recall_token()
    real = maintenance.atomic_json_write
    def fault(path, value, **kwargs):
        if path.name == maintenance.REFRESH_FILE and "mutation_id" in value:
            if after_write:
                real(path, value, **kwargs)
            raise OSError("gate publication failure")
        return real(path, value, **kwargs)
    with monkeypatch.context() as m:
        m.setattr(maintenance, "atomic_json_write", fault)
        p._maybe_reindex(force=True)
        assert p._index_worker.drain(5)
    assert not engine.calls, "native mutation began without a confirmed shared gate"
    assert p._index_requested and not p._index_running
    assert q._recall_token() is None if after_write else q._recall_token() == before
    p._maybe_reindex(force=True)
    assert p._index_worker.drain(5)
    assert q._recall_token() is not None
    assert all("--rebuild" not in c for c in engine.calls)


@pytest.mark.parametrize("stage", ["journal", "request_ack", "refresh_ack"])
@pytest.mark.parametrize("after_write", [False, True])
def test_post_generation_fault_retains_gate_or_proves_new_generation(peers, monkeypatch, stage, after_write):
    p, q, engine = peers
    before = q._recall_token()
    real_save = p._save_mirror_state
    state = p._mirror_state()
    state["refresh_required"] = True
    real_save(state)
    if stage == "journal":
        target, name = p, "_save_mirror_state"
    else:
        target, name = _mod, "acknowledge_request" if stage == "request_ack" else "acknowledge_refresh"
    real = getattr(target, name)
    reached = []
    def fault(*args, **kwargs):
        reached.append(True)
        if after_write:
            real(*args, **kwargs)
        raise OSError("post-generation publication fault")
    with monkeypatch.context() as m:
        m.setattr(target, name, fault)
        p._maybe_reindex(force=True)
        assert p._index_worker.drain(5)
    assert reached and any(c[0] == "index" for c in engine.calls)
    assert p._index_requested and not p._index_running
    # Only an exception after successful final unlink can expose recall: both
    # generation publication and all durable bookkeeping already succeeded.
    if stage == "refresh_ack" and after_write:
        assert q._recall_token() is not None and q._recall_token() != before
    else:
        assert q._recall_token() is None
        assert (p._vault / maintenance.REFRESH_FILE).exists()
    p._maybe_reindex(force=True)
    assert p._index_worker.drain(5)
    assert q._recall_token() is not None and q._recall_token() != before


def test_continuation_arriving_at_refresh_unlink_survives(peers, monkeypatch):
    p, q, engine = peers
    path = p._vault / maintenance.REFRESH_FILE
    real_unlink = Path.unlink
    entered, done = threading.Event(), threading.Event()
    errors, blocked = [], []
    thread = None
    def admission():
        entered.set()
        try:
            p._maybe_reindex(force=True)
        except BaseException as exc:
            errors.append(repr(exc))
        finally:
            done.set()
    def unlink(self, *args, **kwargs):
        nonlocal thread
        if self == path and thread is None:
            thread = threading.Thread(target=admission)
            thread.start()
            assert entered.wait(5)
            blocked.append(not done.wait(0.2))
        return real_unlink(self, *args, **kwargs)
    submit = p._index_worker.submit
    try:
        # Accept only the first job; the distinct producer below is competing
        # with acknowledgment under the real short flock, not a reentrant hook.
        def once(*args):
            monkeypatch.setattr(p._index_worker, "submit", lambda *args: False)
            return submit(*args)
        monkeypatch.setattr(p._index_worker, "submit", once)
        with monkeypatch.context() as m:
            m.setattr(Path, "unlink", unlink)
            p._maybe_reindex(force=True)
            assert p._index_worker.drain(5)
        assert thread is not None
        thread.join(5)
        assert not thread.is_alive() and not errors, errors
        assert blocked == [True]
        assert path.exists() and p._index_requested and not p._index_running
        p.shutdown()
        assert q._recall_token() is None
        q.prefetch("which fact was stored?")
        assert q._index_worker.drain(5)
        assert q._recall_token() is not None
    finally:
        if thread is not None:
            thread.join(5)


@pytest.mark.parametrize("payload", [b'{', b'[]', b'{}', b'{"request_id":"r","identity":null}',
                                      b'{"request_id":"r","operation":"first"}'])
def test_malformed_refresh_obligation_is_retained(peers, payload):
    p, q, engine = peers
    path = p._vault / maintenance.REFRESH_FILE
    path.write_bytes(payload)
    p._maybe_reindex(force=True)
    assert p._index_worker.drain(5)
    assert not engine.calls and path.read_bytes() == payload
    assert q._recall_token() is None


def test_refresh_alias_refusal_keeps_unrelated_bytes(peers):
    p, q, engine = peers
    sentinel = p._vault / "unrelated-sentinel"
    sentinel.write_text("keep")
    (p._vault / maintenance.REFRESH_FILE).symlink_to(sentinel)
    p._maybe_reindex(force=True)
    assert p._index_worker.drain(5)
    assert not engine.calls and sentinel.read_text() == "keep"
    assert q._recall_token() is None
