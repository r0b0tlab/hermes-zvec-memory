"""Safety for owned legacy whole-file mirrors, through the Hermes bridge."""
from concurrent.futures import ThreadPoolExecutor
import threading
import pytest

from agent.memory_manager import MemoryManager
from test_provider import make_provider


@pytest.mark.parametrize("action", ["patch", "remove"])
def test_whole_file_entry_mutation_stays_pending(tmp_path, monkeypatch, action):
    p = make_provider(tmp_path)
    monkeypatch.setattr(p, "_maybe_reindex", lambda *a, **k: None)
    monkeypatch.setattr(p, "_run_in_background", lambda *a, **k: True)
    try:
        p._apply_mirror("add", "user", "First entry.\n§\nSecond entry.", {})
        before = (p._vault / ".mirror-map.json").read_bytes()
        facts = {f.name: f.read_bytes() for f in (p._vault / "facts").iterdir()}
        manager = MemoryManager()
        manager.add_provider(p)
        if action == "patch":
            args = {"target": "user", "action": "patch", "old_text": "First", "new_text": "Changed"}
            result = {"success": True, "patched_entry_before": "First entry.",
                      "patched_entry_after": "Changed entry."}
        else:
            args = {"target": "user", "action": "remove", "old_text": "First"}
            result = {"success": True, "removed_entry": "First entry."}
        manager.notify_memory_tool_write(result, args)
        pending = p._mirror_inbox.first()
        assert pending is not None
        with pytest.raises(ValueError, match="whole-file"):
            p._drain_mirror_inbox()
        assert p._mirror_inbox.first() == pending
        assert (p._vault / ".mirror-map.json").read_bytes() == before
        assert {f.name: f.read_bytes() for f in (p._vault / "facts").iterdir()} == facts
        assert p._recall_token() is None
    finally:
        p.shutdown()


def test_exact_entry_plus_whole_file_is_ambiguous(tmp_path, monkeypatch):
    p = make_provider(tmp_path)
    monkeypatch.setattr(p, "_maybe_reindex", lambda *a, **k: None)
    try:
        p._apply_mirror("add", "user", "First entry.\n§\nSecond entry.", {})
        p._apply_mirror("add", "user", "First entry.", {})
        before = (p._vault / ".mirror-map.json").read_bytes()
        with pytest.raises(ValueError, match="whole-file"):
            p._apply_mirror("remove", "user", "", {"previous_content": "First entry."})
        assert (p._vault / ".mirror-map.json").read_bytes() == before
    finally:
        p.shutdown()


def test_real_store_entry_lifecycle_and_notification_replay(tmp_path, monkeypatch, isolated_home):
    from tools.memory_tool_store import MemoryStore
    store = MemoryStore()
    store.load_from_disk()
    p = make_provider(tmp_path)
    monkeypatch.setattr(p, "_maybe_reindex", lambda *a, **k: None)
    monkeypatch.setattr(p, "_run_in_background", lambda *a, **k: True)
    try:
        manager = MemoryManager()
        manager.add_provider(p)
        actions = [
            ({"target": "user", "action": "add", "content": "Synthetic first preference."},
             lambda: store.add("user", "Synthetic first preference.")),
            ({"target": "user", "action": "add", "content": "Synthetic second preference."},
             lambda: store.add("user", "Synthetic second preference.")),
            ({"target": "user", "action": "patch", "old_text": "first", "new_text": "revised"},
             lambda: store.patch("user", "first", "revised")),
            ({"target": "user", "action": "remove", "old_text": "second"},
             lambda: store.remove("user", "second")),
        ]
        for args, operation in actions:
            result = operation()
            assert result["success"]
            manager.notify_memory_tool_write(result, args)
            item = p._mirror_inbox.first()
            assert item is not None
            p._drain_mirror_inbox()
            state = p._mirror_state()
            source = (isolated_home / "memories" / "USER.md").read_text()
            assert sorted(r["content"] for r in state["records"].values()) == sorted(store._parse_entries(source))
            assert not p._mirror_inbox.pending()
            # A crash after journal commit but before acknowledgement replays the same id.
            p._apply_mirror(*item[1], notification_id=item[0])
            assert p._mirror_state() == state
    finally:
        p.shutdown()


def test_concurrent_drains_never_acknowledge_ambiguous_legacy(tmp_path, monkeypatch):
    p = make_provider(tmp_path)
    monkeypatch.setattr(p, "_maybe_reindex", lambda *a, **k: None)
    try:
        p._apply_mirror("add", "user", "First entry.\n§\nSecond entry.", {})
        p._mirror_inbox.append(["remove", "user", "", {"previous_content": "First entry."}])
        before = (p._vault / ".mirror-map.json").read_bytes()
        barrier = threading.Barrier(2)
        def drain():
            barrier.wait(timeout=3)
            with pytest.raises(ValueError, match="whole-file"):
                p._drain_mirror_inbox()
        with ThreadPoolExecutor(max_workers=2) as executor:
            jobs = [executor.submit(drain) for _ in range(2)]
            for job in jobs:
                job.result(timeout=5)
        assert p._mirror_inbox.pending()
        assert (p._vault / ".mirror-map.json").read_bytes() == before
    finally:
        p.shutdown()


@pytest.mark.parametrize("action", ["patch", "remove"])
def test_real_store_commit_then_manager_leaves_legacy_pending(tmp_path, monkeypatch, isolated_home, action):
    from tools.memory_tool_store import MemoryStore
    store = MemoryStore()
    store.load_from_disk()
    assert store.add("user", "Synthetic first preference.")["success"]
    assert store.add("user", "Synthetic second preference.")["success"]
    source = isolated_home / "memories" / "USER.md"
    initial = source.read_text()
    p = make_provider(tmp_path)
    monkeypatch.setattr(p, "_maybe_reindex", lambda *a, **k: None)
    monkeypatch.setattr(p, "_run_in_background", lambda *a, **k: True)
    try:
        p._apply_mirror("add", "user", initial, {})
        manager = MemoryManager()
        manager.add_provider(p)
        if action == "patch":
            result = store.patch("user", "first", "revised")
            args = {"target": "user", "action": "patch", "old_text": "first", "new_text": "revised"}
        else:
            result = store.remove("user", "first")
            args = {"target": "user", "action": "remove", "old_text": "first"}
        assert result["success"]
        committed = source.read_bytes()
        manager.notify_memory_tool_write(result, args)
        before = (p._vault / ".mirror-map.json").read_bytes()
        for _ in range(2):
            with pytest.raises(ValueError, match="whole-file"):
                p._drain_mirror_inbox()
            assert (p._vault / ".mirror-map.json").read_bytes() == before
            assert source.read_bytes() == committed
            assert p._mirror_inbox.pending()
    finally:
        p.shutdown()
