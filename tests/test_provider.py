"""Tests for the zvec-memory provider.

Run with the Hermes gateway venv so ``agent.*`` imports resolve::

    ~/.hermes/hermes-agent/venv/bin/python -m pytest tests/ -q

Tests that need the ``zg`` binary skip when it is absent.
"""

import json
import os
import shutil
import sys
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
HERMES_AGENT_DIR = Path(os.environ.get(
    "HERMES_AGENT_DIR", str(Path.home() / ".hermes" / "hermes-agent")))
PROVIDER_SRC = REPO_ROOT / "zvec-memory" / "__init__.py"

sys.path.insert(0, str(HERMES_AGENT_DIR))

from agent.memory_provider import is_trivial_prompt  # noqa: E402

import importlib.util  # noqa: E402

_spec = importlib.util.spec_from_file_location("zvec_memory_provider", str(PROVIDER_SRC))
_mod = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = _mod
_spec.loader.exec_module(_mod)

ZvecMemoryProvider = _mod.ZvecMemoryProvider
needs_zg = pytest.mark.skipif(shutil.which("zg") is None, reason="zg not installed")


def make_provider(tmp_path, start_index=False, **overrides):
    cfg = {"vault": str(tmp_path / "vault"), "reindex_min_seconds": 3600}
    cfg.update(overrides)
    p = ZvecMemoryProvider(config=cfg)
    if not start_index:
        index = Path(cfg["vault"]) / ".zvec-grep"
        index.mkdir(parents=True, exist_ok=True)
        (index / "manifest.json").write_text("{}")
    p.initialize("test-session", hermes_home=str(tmp_path))
    return p


def test_native_config_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    p = ZvecMemoryProvider(config={})
    p.save_config({"recall_limit": 11, "preview": "full"}, str(tmp_path))
    p.save_config({"context_chars": 321}, str(tmp_path))
    q = ZvecMemoryProvider()
    assert q._recall_limit() == 11
    assert q._config["preview"] == "full"
    assert q._config["context_chars"] == 321
    assert not (tmp_path / "config.yaml").exists()


def test_explicit_empty_config_ignores_ambient(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    p = ZvecMemoryProvider(config={})
    p.save_config({"recall_limit": 11}, str(tmp_path))
    assert ZvecMemoryProvider(config={})._config == {}


@pytest.mark.parametrize("raw", ["external", "$HERMES_HOME/external", "${HERMES_HOME}/external"])
def test_cold_backup_resolves_profile_paths(tmp_path, raw):
    p = ZvecMemoryProvider(config={"vault": raw})
    home = Path(os.environ["HERMES_HOME"])
    assert p.backup_paths() == [str(home / "external")]
    assert not (home / "external").exists()


def test_legacy_config_migrates_only_on_save(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    legacy = tmp_path / "config.yaml"
    text = "unrelated: keep\nplugins:\n  zvec-memory:\n    preview: full\n"
    legacy.write_text(text)
    assert ZvecMemoryProvider()._config == {"preview": "full"}
    assert not (tmp_path / "zvec-memory/config.json").exists()
    ZvecMemoryProvider().save_config({"recall_limit": 9}, str(tmp_path))
    assert ZvecMemoryProvider()._config == {"preview": "full", "recall_limit": 9}
    assert legacy.read_text() == text


def test_corrupt_native_config_is_not_silently_replaced(tmp_path):
    path = tmp_path / "zvec-memory/config.json"
    path.parent.mkdir()
    path.write_text("{broken")
    with pytest.raises(ValueError):
        ZvecMemoryProvider(config={}).save_config({"preview": "full"}, str(tmp_path))
    assert path.read_text() == "{broken"


def test_fact_prefix_collision_preserves_both(tmp_path, monkeypatch):
    p = make_provider(tmp_path)
    try:
        monkeypatch.setattr(_mod, "_utc_stamp", lambda: "20000101-000000")
        a = p._write_fact("shared prefix " * 8 + "first", "general", "")
        b = p._write_fact("shared prefix " * 8 + "second", "general", "")
        assert a != b
        assert "first" in a.read_text()
        assert "second" in b.read_text()
        assert "shared" not in a.name
        assert a.stat().st_mode & 0o777 == 0o600
    finally:
        p.shutdown()


def test_prompt_is_static_after_write(tmp_path):
    p = make_provider(tmp_path)
    try:
        before = p.system_prompt_block()
        p._write_fact("A new fact", "general", "")
        assert p.system_prompt_block() == before
    finally:
        p.shutdown()


@pytest.mark.parametrize("directory", ["facts", "sessions"])
def test_reject_symlinked_source_directories(tmp_path, directory):
    outside = tmp_path / "outside"
    outside.mkdir()
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / directory).symlink_to(outside, target_is_directory=True)
    p = ZvecMemoryProvider(config={"vault": str(vault)})
    try:
        with pytest.raises(ValueError, match="symlink"):
            p.initialize("test", hermes_home=str(tmp_path))
    finally:
        p.shutdown()
    assert list(outside.iterdir()) == []


def test_queued_turn_keeps_call_session(tmp_path, monkeypatch):
    p = make_provider(tmp_path)
    work = []
    monkeypatch.setattr(p, "_run_in_background", lambda fn, *args: work.append((fn, args)))
    try:
        p.sync_turn("old user", "old reply", session_id="old-id")
        p.on_session_switch("new-id")
        fn, args = work.pop(0)
        fn(*args)
        text = next((tmp_path / "vault/sessions").glob("*.md")).read_text()
        assert "session old-id" in text
        assert "session new-id" not in text
    finally:
        p.shutdown()


@pytest.mark.parametrize("context", ["primary", "subagent", "flush"])
def test_automatic_writes_only_for_primary(tmp_path, monkeypatch, context):
    monkeypatch.setattr(ZvecMemoryProvider, "_build_index", lambda self: None)
    p = ZvecMemoryProvider(config={"vault": str(tmp_path / "vault"), "auto_extract": True})
    p.initialize("test", hermes_home=str(tmp_path), agent_context=context)
    work = []
    monkeypatch.setattr(p, "_run_in_background", lambda fn, *args: work.append(fn.__name__))
    try:
        p.sync_turn("hello", "reply")
        p.on_memory_write("add", "user", "a preference")
        p.on_session_end([{"role": "user", "content": "I prefer tea"}])
        assert len(work) == (3 if context == "primary" else 0)
        assert json.loads(p.handle_tool_call("memory_store", {"content": "explicit fact"}))["status"] == "stored"
    finally:
        p.shutdown()


def test_disk_worker_preserves_fifo(tmp_path):
    from threading import Event
    p = make_provider(tmp_path)
    entered, release = Event(), Event()
    output = []
    try:
        assert getattr(p, "_disk_worker", None) is not None
        def first():
            entered.set()
            assert release.wait(2)
            output.append(1)
        assert p._run_in_background(first)
        assert entered.wait(2)
        assert p._run_in_background(output.append, 2)
        release.set()
        assert p._disk_worker.drain(2)
        assert output == [1, 2]
        p.shutdown()
        assert not p._run_in_background(output.append, 3)
    finally:
        release.set()
        p.shutdown()


def test_index_coalesces_requests_without_blocking_disk(tmp_path, monkeypatch):
    from threading import Event
    p = make_provider(tmp_path)
    entered, release, second, disk = Event(), Event(), Event(), Event()
    calls = []
    def run(cmd, timeout):
        calls.append(cmd)
        if len(calls) == 1:
            entered.set()
            assert release.wait(2)
        else:
            second.set()
        return 0, "", ""
    monkeypatch.setattr(p, "_run_zg", run)
    try:
        p._maybe_reindex(force=True)
        assert entered.wait(2)
        p._maybe_reindex(force=True)
        p._run_in_background(disk.set)
        assert disk.wait(0.5), "index must not starve disk persistence"
        release.set()
        assert second.wait(2)
        assert p._index_worker.drain(2)
        assert len(calls) == 2
    finally:
        release.set()
        p.shutdown()


@pytest.mark.parametrize("raises", [False, True])
def test_index_failure_retains_dirty_state_and_embedding(tmp_path, monkeypatch, raises):
    p = make_provider(tmp_path)
    calls = []
    def run(cmd, timeout):
        calls.append(cmd)
        if len(calls) == 1:
            if raises:
                raise OSError("fault")
            return 1, "", "fault"
        return 0, "", ""
    monkeypatch.setattr(p, "_run_zg", run)
    try:
        p._build_index()
        assert p._index_worker.drain(2)
        assert p._last_reindex == 0
        assert p._index_requested and not p._index_running
        p._maybe_reindex(force=True)
        assert p._index_worker.drain(2)
        assert len(calls) == 2
        assert "--embedding" in calls[1]
        assert p._last_reindex > 0
    finally:
        p.shutdown()


def test_index_serialized_between_vault_handles(tmp_path, monkeypatch):
    from threading import Event
    p, q = make_provider(tmp_path), make_provider(tmp_path)
    entered, release, collision = Event(), Event(), Event()
    active = []
    def run(cmd, timeout):
        active.append(1)
        if len(active) > 1:
            collision.set()
        entered.set()
        assert release.wait(2)
        active.pop()
        return 0, "", ""
    monkeypatch.setattr(p, "_run_zg", run)
    monkeypatch.setattr(q, "_run_zg", run)
    try:
        p._maybe_reindex(force=True)
        assert entered.wait(2)
        q._maybe_reindex(force=True)
        assert not collision.wait(0.2)
        release.set()
        assert p._index_worker.drain(2)
        assert q._index_worker.drain(2)
    finally:
        release.set()
        p.shutdown()
        q.shutdown()


@pytest.mark.parametrize("code", ["ZVEC_GREP.ENGINE.LOCK.BUSY", "ZVEC_GREP.ENGINE.DAEMON_LEASE_ACTIVE"])
def test_index_retries_native_lock_contention(tmp_path, monkeypatch, code):
    p = make_provider(tmp_path)
    calls, delays = [], []
    def run(cmd, timeout):
        calls.append(cmd)
        return (1, "", code) if len(calls) == 1 else (0, "", "")
    monkeypatch.setattr(p, "_run_zg", run)
    monkeypatch.setattr(_mod.time, "sleep", delays.append)
    try:
        p._maybe_reindex(force=True)
        assert p._index_worker.drain(2)
        assert len(calls) == 2
        assert delays and 0 < delays[0] <= 1
        assert p._last_reindex > 0
    finally:
        p.shutdown()


def test_index_scopes_markdown_only(tmp_path, monkeypatch):
    p = make_provider(tmp_path)
    calls = []
    monkeypatch.setattr(p, "_run_zg", lambda cmd, timeout: (calls.append(cmd) or (0, "", "")))
    try:
        p._maybe_reindex(force=True)
        assert p._index_worker.drain(2)
        assert "--reset-paths" in calls[0]
        assert calls[0].count("-g") == 2
        assert "facts/**/*.md" in calls[0] and "sessions/**/*.md" in calls[0]
    finally:
        p.shutdown()


@pytest.mark.parametrize("budget", [-1, 0, 1, 4, 12, 80])
def test_context_budget_is_hard_limit(tmp_path, budget):
    p = make_provider(tmp_path, context_chars=budget)
    try:
        assert len(p._cap("word " * 100)) <= max(0, budget)
        p._run_zg = lambda *a, **k: (0, "word " * 100, "")
        assert len(p._run_prefetch_query("deploy details")) <= max(0, budget)
    finally:
        p.shutdown()


def test_empty_prefetch_success_is_cached_but_errors_retry(tmp_path, monkeypatch):
    p = make_provider(tmp_path)
    calls = []
    responses = [(1, "", "error"), (0, "", "")]
    monkeypatch.setattr(p, "is_available", lambda: True)
    def run(cmd, timeout):
        calls.append(timeout)
        return responses[min(len(calls) - 1, 1)]
    monkeypatch.setattr(p, "_run_zg", run)
    try:
        for _ in range(3):
            assert p.prefetch("deployment procedure details") == ""
        assert len(calls) == 2
        assert calls == [2, 2]
    finally:
        p.shutdown()


def test_duplicate_queued_prefetch_is_coalesced(tmp_path, monkeypatch):
    from threading import Event
    p = make_provider(tmp_path)
    entered, release = Event(), Event()
    calls = []
    monkeypatch.setattr(p, "is_available", lambda: True)
    def run(cmd, timeout):
        calls.append(cmd)
        entered.set()
        assert release.wait(2)
        return 0, "", ""
    monkeypatch.setattr(p, "_run_zg", run)
    try:
        p.queue_prefetch("deployment procedure details")
        assert entered.wait(2)
        p.queue_prefetch("deployment procedure details")
        release.set()
        assert p._disk_worker.drain(2)
        assert len(calls) == 1
    finally:
        release.set()
        p.shutdown()


@pytest.mark.parametrize("write", ["fact", "turn", "index"])
def test_writes_invalidate_inflight_prefetch(tmp_path, monkeypatch, write):
    from threading import Event, Thread
    p = make_provider(tmp_path)
    entered, release = Event(), Event()
    monkeypatch.setattr(p, "is_available", lambda: True)
    def run(cmd, timeout):
        if cmd[0] == "query":
            entered.set()
            assert release.wait(2)
            return 0, "stale recalled fact", ""
        return 0, "", ""
    monkeypatch.setattr(p, "_run_zg", run)
    query = "deployment procedure details"
    thread = Thread(target=p.prefetch, args=(query,))
    try:
        thread.start()
        assert entered.wait(2)
        if write == "fact":
            p._write_fact("new fact", "general", "")
        elif write == "turn":
            p._append_turn("user", "reply", "session")
        else:
            p._maybe_reindex(force=True)
            assert p._index_worker.drain(2)
        release.set()
        thread.join(2)
        assert p._cached_prefetch(query) is None
    finally:
        release.set()
        thread.join(2)
        p.shutdown()


def test_mirror_replace_and_remove_preserves_explicit_facts(tmp_path):
    p = make_provider(tmp_path)
    try:
        explicit = p._write_fact("old preference explicit", "general", "")
        p.on_memory_write("add", "user", "old preference")
        assert p._disk_worker.drain(2)
        old = set((p._vault / "facts").glob("*.md")) - {explicit}
        assert len(old) == 1
        assert "metadata" in __import__("inspect").signature(p.on_memory_write).parameters
        p.on_memory_write("replace", "user", "new preference", metadata={"old_text": "old preference"})
        assert p._disk_worker.drain(2)
        assert not next(iter(old)).exists()
        files = set((p._vault / "facts").glob("*.md")) - {explicit}
        assert len(files) == 1 and "new preference" in next(iter(files)).read_text()
        p.on_memory_write("remove", "user", "", metadata={"old_text": "new preference"})
        assert p._disk_worker.drain(2)
        assert list((p._vault / "facts").glob("*.md")) == [explicit]
    finally:
        p.shutdown()


def test_mirror_unlink_crash_recovers_and_gates_until_index(tmp_path, monkeypatch):
    p = make_provider(tmp_path)
    p._apply_mirror("add", "user", "old preference", {})
    assert p._index_worker.drain(2)
    old = next((p._vault / "facts").glob("*.md"))
    real_unlink = Path.unlink
    def fail_unlink(path, *a, **kw):
        if path == old:
            raise OSError("unlink fault")
        return real_unlink(path, *a, **kw)
    try:
        with monkeypatch.context() as m:
            m.setattr(Path, "unlink", fail_unlink)
            with pytest.raises(OSError):
                p._apply_mirror("remove", "user", "", {"old_text": "old preference"})
        state = json.loads((p._vault / ".mirror-map.json").read_text())
        assert state["pending_deletes"], "deletion intent must survive interrupted unlink"
        assert not p._mirror_ready
        p.shutdown()
        monkeypatch.setattr(ZvecMemoryProvider, "_run_zg", lambda *a, **k: (1, "", "index fault"))
        q = make_provider(tmp_path)
        try:
            assert q._index_worker.drain(2)
            assert not old.exists()
            assert not q._mirror_ready
            assert json.loads(q.handle_tool_call("memory_search", {"query": "old preference"})).get("error")
            assert q.prefetch("old preference details") == ""
            assert json.loads((q._vault / ".mirror-map.json").read_text())["refresh_required"]
            monkeypatch.setattr(q, "_run_zg", lambda *a, **k: (0, "", ""))
            q._maybe_reindex(force=True)
            assert q._index_worker.drain(2)
            assert q._mirror_ready
            assert not json.loads((q._vault / ".mirror-map.json").read_text())["refresh_required"]
        finally:
            q.shutdown()
    finally:
        p.shutdown()


@pytest.mark.parametrize("landed", [False, True])
def test_mirror_map_write_fault_never_leaves_unowned_searchable_create(tmp_path, monkeypatch, landed):
    p = make_provider(tmp_path)
    p._apply_mirror("add", "user", "old preference", {})
    assert p._index_worker.drain(2)
    old = next((p._vault / "facts").glob("*.md"))
    save = p._save_mirror_state
    def fail(state):
        if landed:
            save(state)
        raise OSError("atomic map fault")
    try:
        with monkeypatch.context() as m:
            m.setattr(p, "_save_mirror_state", fail)
            with pytest.raises(OSError):
                p._apply_mirror("replace", "user", "new preference", {"old_text": "old preference"})
        assert list((p._vault / "facts").glob("*.md")) == [old]
        assert not p._mirror_ready
        p.shutdown()
        q = make_provider(tmp_path)
        try:
            assert q._index_worker.drain(2)
            files = list((q._vault / "facts").glob("*.md"))
            assert len(files) == 1
            assert ("new preference" if landed else "old preference") in files[0].read_text()
            assert q._mirror_ready
        finally:
            q.shutdown()
    finally:
        p.shutdown()


def test_mirror_recovery_validates_entire_journal_before_deleting(tmp_path):
    p = make_provider(tmp_path)
    protected = p._write_fact("preserve me", "general", "")
    p._save_mirror_state({"records": {}, "pending_deletes": [str(protected.relative_to(p._vault)), "../outside.md"], "refresh_required": True})
    try:
        with pytest.raises(ValueError):
            p._recover_mirrors()
        assert protected.exists()
        assert not p._mirror_ready
    finally:
        p.shutdown()


def test_other_handle_cannot_recall_pending_mirror_deletion(tmp_path, monkeypatch):
    p, q = make_provider(tmp_path), make_provider(tmp_path)
    p._apply_mirror("add", "user", "old preference", {})
    assert p._index_worker.drain(2)
    monkeypatch.setattr(p, "_run_zg", lambda *a, **k: (1, "", "index fault"))
    monkeypatch.setattr(q, "is_available", lambda: True)
    monkeypatch.setattr(q, "_run_zg", lambda *a, **k: (0, "old preference", ""))
    try:
        assert "old preference" in q.prefetch("old preference details")
        p._apply_mirror("remove", "user", "", {"old_text": "old preference"})
        assert p._index_worker.drain(2)
        assert q.prefetch("old preference details") == ""
        assert json.loads(q.handle_tool_call("memory_search", {"query": "old preference"})).get("error")
    finally:
        p.shutdown()
        q.shutdown()


@pytest.mark.parametrize("route", ["prefetch", "search", "queued"])
def test_inflight_recall_drops_results_after_mirror_change(tmp_path, monkeypatch, route):
    from threading import Event, Thread
    p = make_provider(tmp_path)
    p._apply_mirror("add", "user", "old preference", {})
    assert p._index_worker.drain(2)
    entered, release = Event(), Event()
    monkeypatch.setattr(p, "is_available", lambda: True)
    def run(cmd, timeout):
        if cmd[0] == "index":
            return 1, "", "index fault"
        entered.set()
        assert release.wait(2)
        return 0, "old preference", ""
    monkeypatch.setattr(p, "_run_zg", run)
    result = []
    query = "old preference details"
    def recall():
        result.append(p.prefetch(query) if route == "prefetch" else p.handle_tool_call("memory_search", {"query": query}))
    thread = Thread(target=recall)
    try:
        if route == "queued":
            p.queue_prefetch(query)
        else:
            thread.start()
        assert entered.wait(2)
        p._apply_mirror("remove", "user", "", {"old_text": "old preference"})
        release.set()
        if route == "queued":
            assert p._disk_worker.drain(2)
            assert p._cached_prefetch(query) is None
        else:
            thread.join(2)
            assert result and "old preference" not in result[0]
    finally:
        release.set()
        if thread.ident:
            thread.join(2)
        p.shutdown()


def test_auto_extract_fails_closed_when_filter_unavailable(tmp_path, monkeypatch):
    p = make_provider(tmp_path)
    monkeypatch.setitem(sys.modules, "agent.context_compressor", None)
    try:
        p._auto_extract([{"role": "user", "content": "I prefer generated summaries", "_compressed_summary": True}])
        assert list((p._vault / "facts").glob("*.md")) == []
    finally:
        p.shutdown()


@pytest.mark.parametrize("tool,key", [("memory_search", "query"), ("memory_store", "content")])
@pytest.mark.parametrize("value", [None, [], {}, "   "])
def test_invalid_required_tool_values(tmp_path, tool, key, value):
    p = make_provider(tmp_path)
    try:
        assert json.loads(p.handle_tool_call(tool, {key: value})).get("error")
        assert list((p._vault / "facts").glob("*.md")) == []
    finally:
        p.shutdown()


@pytest.mark.parametrize("extra", [{"globs": "facts/**"}, {"globs": [None]}, {"globs": {}}, {"limit": True}, {"limit": "5"}, {"limit": 1.2}])
def test_invalid_search_options(tmp_path, extra):
    p = make_provider(tmp_path)
    try:
        assert json.loads(p.handle_tool_call("memory_search", {"query": "deployment details", **extra})).get("error")
    finally:
        p.shutdown()


@pytest.mark.parametrize("mode", ["hybrid", "fts", "vector"])
def test_dash_prefixed_queries_are_bound_as_values(tmp_path, mode):
    p = make_provider(tmp_path)
    try:
        cmd = p._search_cmd("--allow-remote", mode, 5)
        assert f"--{mode}=--allow-remote" in cmd
        assert "--allow-remote" not in cmd
    finally:
        p.shutdown()


def test_search_schedules_debounced_dirty_index_without_daemon(tmp_path, monkeypatch):
    p = make_provider(tmp_path)
    calls = []
    monkeypatch.setattr(p, "_run_zg", lambda cmd, timeout: (calls.append(cmd) or (0, "", "")))
    try:
        p._last_reindex = time.monotonic()
        p.handle_tool_call("memory_store", {"content": "new fact"})
        assert p._index_requested and not p._index_running
        p.handle_tool_call("memory_search", {"query": "new fact details"})
        assert p._index_worker.drain(2)
        assert any(cmd[0] == "index" for cmd in calls)
    finally:
        p.shutdown()


def test_name_and_schemas(tmp_path):
    p = make_provider(tmp_path)
    try:
        assert p.name == "zvec-memory"
        names = {s["name"] for s in p.get_tool_schemas()}
        assert names == {"memory_search", "memory_store"}
        block = p.system_prompt_block()
        assert "Zvec Memory" in block and "memory_store" in block
    finally:
        p.shutdown()


def test_trivial_prefetch_never_shells_out(tmp_path, monkeypatch):
    p = make_provider(tmp_path)
    try:
        assert is_trivial_prompt("thanks")
        monkeypatch.setattr(_mod.subprocess, "run", lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("must not shell out")))
        assert p.prefetch("thanks") == ""
        assert p.prefetch("") == ""
    finally:
        p.shutdown()


def test_unknown_tool_errors(tmp_path):
    p = make_provider(tmp_path)
    try:
        assert "Unknown tool" in p.handle_tool_call("nope", {})
        assert "query" in p.handle_tool_call("memory_search", {})
        assert "content" in p.handle_tool_call("memory_store", {})
        assert "Unknown mode" in p.handle_tool_call("memory_search",
                                                     {"query": "x", "mode": "nope"})
    finally:
        p.shutdown()


def test_memory_write_mirror(tmp_path):
    p = make_provider(tmp_path)
    try:
        p.on_memory_write("add", "user", "User prefers concise replies")
        p.shutdown()  # join background writer
        files = list((tmp_path / "vault" / "facts").glob("*.md"))
        assert len(files) == 1
        assert "concise replies" in files[0].read_text()
    finally:
        p.shutdown()


def test_backup_paths(tmp_path):
    p = make_provider(tmp_path)
    try:
        assert p.backup_paths() == [str(tmp_path / "vault")]
    finally:
        p.shutdown()


def test_prefetch_cache_avoids_subprocess(tmp_path, monkeypatch):
    vault = tmp_path / "vault"
    (vault / ".zvec-grep").mkdir(parents=True)
    (vault / ".zvec-grep" / "manifest.json").write_text("{}")
    p = make_provider(tmp_path)
    try:
        calls = []

        def fake_run(cmd, timeout):
            calls.append(cmd)
            return 0, "facts/x.md:1-2\nsome recalled fact", ""
        monkeypatch.setattr(p, "_run_zg", fake_run)
        monkeypatch.setattr(p, "is_available", lambda: True)
        p.queue_prefetch("what is the deploy process")
        assert p._disk_worker.drain(5)
        assert p._cached_prefetch("what is the deploy process").startswith("## Zvec Memory")

        def boom(cmd, timeout):
            raise AssertionError("cache hit must not shell out")
        monkeypatch.setattr(p, "_run_zg", boom)
        assert "some recalled fact" in p.prefetch("what is the deploy process")
    finally:
        p.shutdown()


def test_prefetch_skips_unready_index(tmp_path, monkeypatch):
    monkeypatch.setattr(ZvecMemoryProvider, "_build_index", lambda self: None)
    p = make_provider(tmp_path, start_index=True)
    try:
        monkeypatch.setattr(_mod.subprocess, "run", lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("must not shell out without an index")))
        assert p.prefetch("what is the deploy process") == ""
    finally:
        p.shutdown()


@needs_zg
@pytest.mark.integration
def test_store_then_search_roundtrip(tmp_path):
    p = make_provider(tmp_path, start_index=True, reindex_min_seconds=0)
    try:
        res = json.loads(p.handle_tool_call(
            "memory_store",
            {"content": "The staging deploy password rotates on blue moons",
             "category": "project", "tags": "deploy"}))
        assert res["status"] == "stored"
        # Indexing is background by design: force a refresh, then poll until
        # the fact is retrievable (concurrent index runs serialize; a query
        # racing one may briefly fail, so retry).
        p._maybe_reindex(force=True)
        deadline = time.time() + 120
        hit = ""
        while time.time() < deadline:
            out = json.loads(p.handle_tool_call(
                "memory_search", {"query": "staging deploy password rotation", "mode": "hybrid"}))
            if "blue moons" in out.get("results", ""):
                hit = out["results"]
                break
            time.sleep(5)
        assert "blue moons" in hit
    finally:
        p.shutdown()
