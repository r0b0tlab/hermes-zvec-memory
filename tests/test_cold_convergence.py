"""Offline provider/worker regressions; command-aware double, not native evidence."""
import json
import threading
import time

import pytest
from test_provider import ZvecMemoryProvider, _mod


class NativeDouble:
    """Only native boundary doubled; manifest appears only on successful index."""
    def __init__(self):
        self.calls = []
        self.completed_indices = []
        self.entered = threading.Event()
        self.release = threading.Event()
        self.block_next = True
        self.snapshot = {}

    def run(self, provider, args, timeout):
        self.calls.append((list(args), timeout))
        if args == ["--version"]:
            return 0, "0.2.2\n", ""
        if args[0] == "index":
            snapshot = {p.relative_to(provider._vault).as_posix(): p.read_text()
                        for directory in ("facts", "sessions")
                        for p in (provider._vault / directory).glob("*.md")}
            if self.block_next:
                self.block_next = False
                self.entered.set()
                assert self.release.wait(5), "test index barrier expired"
            time.sleep(1.25)
            self.snapshot = snapshot
            path = provider._vault / ".zvec-grep/manifest.json"
            path.parent.mkdir(exist_ok=True)
            previous = json.loads(path.read_text()) if path.exists() else {}
            embedding = (args[args.index("--embedding") + 1] if "--embedding" in args
                         else previous.get("embedding", _mod.DEFAULT_EMBEDDING))
            path.write_text(json.dumps({"manifestVersion": 1, "embedding": embedding}))
            self.completed_indices.append(list(args))
            return 0, "indexed\n", ""
        if args[0] == "query":
            assert "--refresh" in args and args[args.index("--refresh") + 1] == "off"
            return 0, "", ""
        if args[0] == "status":
            return (0 if provider._index_ready() else 1), "", ""
        raise AssertionError(f"unsupported native command: {args}")

    @property
    def indices(self):
        return [args for args, _ in self.calls if args[0] == "index"]


@pytest.fixture
def cold_pair(tmp_path, monkeypatch):
    native = NativeDouble()
    monkeypatch.setattr(ZvecMemoryProvider, "_run_zg", lambda self, args, timeout: native.run(self, args, timeout))
    monkeypatch.setattr(ZvecMemoryProvider, "is_available", lambda self: True)
    vault = tmp_path / "vault"
    (vault / "facts").mkdir(parents=True)
    for i in range(200):
        (vault / "facts" / f"background-{i}.md").write_text(f"Synthetic background {i}\n")
    handles = []
    try:
        for i in range(2):
            p = ZvecMemoryProvider(config={"vault": str(vault), "zg_bin": "/offline/zg",
                                           "reindex_min_seconds": 0})
            handles.append(p)
            p.initialize(f"cold-{i}", hermes_home=str(tmp_path))
            if i == 0:
                assert native.entered.wait(3)
        yield handles, native
    finally:
        native.release.set()
        for p in handles:
            p.shutdown()


def converge(handles, timeout=4):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for p in handles:
            p.queue_prefetch("soakcontrolanchor")
        state = handles[-1]._mirror_state()
        if (not any(state.get(k) for k in ("pending_creates", "pending_deletes", "refresh_required"))
                and all(p._recall_token() is not None and p._index_ready()
                        and p._mirror_inbox is not None and not p._mirror_inbox.pending()
                        and not p._index_running for p in handles)):
            return True
        time.sleep(.05)
    return False


def test_two_cold_handles_automatic_demand_quiesces_unchanged_corpus(cold_pair):
    handles, native = cold_pair
    native.release.set()
    settled = converge(handles)
    assert settled, (f"automatic convergence failed; successful index calls="
                     f"{len(native.completed_indices)}; dispatched={len(native.indices)}")
    assert len(native.indices) == 1, "idle peer validation must not mutate an already completed index"
    generation = handles[0]._index_generation()
    for _ in range(4):
        for p in handles:
            p.queue_prefetch("soakcontrolanchor")
        time.sleep(.05)
    assert all(p._index_generation() == generation for p in handles)
    assert len(native.indices) == 1


@pytest.mark.parametrize("writer", ["store", "turn", "mirror"])
def test_real_write_during_peer_native_work_survives_adoption(cold_pair, writer):
    handles, native = cold_pair
    owner, peer = handles
    content = "Synthetic new source must survive peer completion"
    captured = _mod.read_refresh(peer._vault)
    assert captured is not None
    if writer == "store":
        result = json.loads(peer.handle_tool_call("memory_store", {"content": content}))
        assert result["status"] == "stored"
        assert _mod.read_refresh(peer._vault) != captured
    elif writer == "turn":
        peer.sync_turn(content, "Synthetic reply")
        assert peer._disk_worker.drain(2)
        assert _mod.read_refresh(peer._vault) != captured
    else:
        peer.on_memory_write("add", "user", content)
        assert peer._mirror_inbox.pending()
    assert all(p._recall_token() is None for p in handles)
    native.release.set()
    assert converge(handles, timeout=8)
    assert len(native.indices) >= 2, "new source work must not be treated as idle validation"
    assert any(content in text for text in native.snapshot.values())
    assert _mod.read_refresh(peer._vault) is None
    assert not peer._mirror_inbox.pending()
    if writer == "mirror":
        state = peer._mirror_state()
        assert state["last_notification"] == peer._mirror_inbox.high_watermark()
        assert any(row["content"] == content for row in state["records"].values())


def test_explicit_rebuild_is_not_idle_adoption(cold_pair):
    handles, native = cold_pair
    native.release.set()
    assert converge(handles)
    p = handles[-1]
    generation = p._index_generation()
    count = len(native.indices)
    _mod.request_rebuild(p._vault, identity={**p._engine_identity(), "zg_version": "0.2.2"})
    assert all(h._recall_token() is None for h in handles)
    assert converge(handles, timeout=6)
    assert len(native.indices) > count
    assert "--rebuild" in native.indices[-1]
    assert p._index_generation() != generation
    assert _mod.read_request(p._vault) is None


def test_identity_conflict_stays_closed_without_old_state_adoption(cold_pair):
    handles, native = cold_pair
    native.release.set()
    assert converge(handles)
    p = handles[-1]
    generation = p._index_generation()
    count = len(native.indices)
    _mod.request_rebuild(p._vault, identity={**p._engine_identity(), "embedding": "local/other"})
    request = _mod.read_request(p._vault)
    for h in handles:
        h.queue_prefetch("soakcontrolanchor")
    for h in handles:
        assert h._index_worker.drain(3)
        assert h._recall_token() is None
    assert len(native.indices) == count
    assert p._index_generation() == generation
    assert _mod.read_request(p._vault) == request


def test_unreadable_journal_cannot_adopt_peer_generation(cold_pair, monkeypatch):
    from pathlib import Path
    handles, native = cold_pair
    native.release.set()
    assert converge(handles)
    p = handles[-1]
    count = len(native.indices)
    journal = p._vault / ".mirror-map.json"
    journal.write_text(json.dumps(p._mirror_state()))
    original = Path.read_text
    def unreadable(path, *args, **kwargs):
        if path == journal:
            raise PermissionError("offline journal read fault")
        return original(path, *args, **kwargs)
    monkeypatch.setattr(Path, "read_text", unreadable)
    p.queue_prefetch("soakcontrolanchor")
    assert p._index_worker.drain(3)
    assert p._recall_token() is None
    assert not p._mirror_ready
    assert len(native.indices) == count
