"""Real Hermes loader + provider + zg lifecycle, no real user memory."""
import json
import os
from pathlib import Path
from source_support import PROVIDER_ROOT
import shutil
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, os.environ.get("HERMES_AGENT_DIR", str(Path.home() / ".hermes/hermes-agent")))
sys.path.insert(0, str(ROOT / "scripts"))
from retrieval_evidence import capture_sources, cited_lines, cited_sections
pytestmark = [pytest.mark.integration, pytest.mark.skipif(
    os.environ.get("ZVEC_RUN_NATIVE") != "1", reason="requires opt-in local model integration")]


def _cited_observation(home, vault, query, response, *, surface="memory_search", raw_native=False):
    sources = capture_sources(response, vault)
    lines = cited_lines(response, sources)
    observation = {"query": query, "response": response, "sources": sources,
                   "cited_paths": [section[0] for section in cited_sections(response["results"])],
                   "returned_lines": lines, "public_surface": surface, "raw_native": raw_native}
    with (home / "retrieval-observations.jsonl").open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(observation) + "\n")
    return observation


def _native_mirror_process(home, content, start, errors):
    os.environ["HOME"] = home
    os.environ["HERMES_HOME"] = home
    from plugins.memory import load_memory_provider
    provider = None
    try:
        provider = load_memory_provider("zvec-memory", register_skills=False)
        provider.initialize("process-writer", hermes_home=home)
        assert start.wait(15)
        provider.on_memory_write("add", "user", content)
    except Exception as exc:
        errors.put(repr(exc))
        raise
    finally:
        if provider:
            provider.shutdown()


def test_native_two_processes_preserve_mirror_ownership(tmp_path, monkeypatch):
    import multiprocessing
    import time
    home = tmp_path / "multi-home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("ZVEC_GREP_MODE", "direct")
    # 0.2.2 revalidates completion-marker ctimes even for offline cached models.
    # Keep those writes under the controlled home, never in read-only inputs.
    model_cache = home / "native-model-cache"
    shutil.copytree(os.environ["ZVEC_TEST_MODEL_CACHE"], model_cache)
    monkeypatch.setenv("ZVEC_GREP_MODEL_CACHE", str(model_cache))
    shutil.copytree(PROVIDER_ROOT / "zvec-memory", home / "plugins/zvec-memory")
    vault = home / "zvec-memory"
    vault.mkdir()
    (vault / "config.json").write_text(json.dumps({
        "zg_bin": os.environ["ZVEC_TEST_BIN"], "reindex_min_seconds": 0}))
    context = multiprocessing.get_context("spawn")
    start, errors = context.Event(), context.Queue()
    contents = ["Synthetic user likes apricot kites.", "Synthetic user likes indigo kayaks."]
    children = [context.Process(target=_native_mirror_process,
                                args=(str(home), text, start, errors)) for text in contents]
    for child in children:
        child.start()
    start.set()
    try:
        for child in children:
            child.join(30)
            assert child.exitcode == 0, f"native writer exit: {child.exitcode}"
    finally:
        for child in children:
            if child.is_alive():
                child.terminate()
                child.join(5)
        errors.close()
    from plugins.memory import load_memory_provider
    provider = load_memory_provider("zvec-memory", register_skills=False)
    provider.initialize("reader", hermes_home=str(home))
    try:
        deadline = time.monotonic() + 45
        recalled = ""
        while time.monotonic() < deadline:
            # Automatic demand must discover/recover peer work, no force refresh.
            recalled = provider.prefetch("What synthetic user preferences are stored?")
            if all(text in recalled for text in contents):
                break
            time.sleep(0.3)
        assert all(text in recalled for text in contents), recalled
        state = json.loads((vault / ".mirror-map.json").read_text())
        assert {record["content"] for record in state["records"].values()} == set(contents)
    finally:
        provider.shutdown()


def test_real_host_provider_native_lifecycle(tmp_path, monkeypatch):
    home = tmp_path / "isolated-home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("ZVEC_GREP_MODE", "direct")
    # 0.2.2 revalidates completion-marker ctimes even for offline cached models.
    # Keep those writes under the controlled home, never in read-only inputs.
    model_cache = home / "native-model-cache"
    shutil.copytree(os.environ["ZVEC_TEST_MODEL_CACHE"], model_cache)
    monkeypatch.setenv("ZVEC_GREP_MODEL_CACHE", str(model_cache))
    for key in ("ZVEC_GREP_API_KEY", "ZVEC_GREP_ENDPOINT", "ZVEC_GREP_HOME"):
        monkeypatch.delenv(key, raising=False)
    shutil.copytree(PROVIDER_ROOT / "zvec-memory", home / "plugins/zvec-memory")
    vault = home / "zvec-memory"
    vault.mkdir()
    config = {"zg_bin": os.environ["ZVEC_TEST_BIN"], "reindex_min_seconds": 0,
              "embedding": "local/potion-retrieval-32m", "context_chars": 2000}
    (vault / "config.json").write_text(json.dumps(config))
    from plugins.memory import load_memory_provider
    from agent.memory_manager import MemoryManager
    from tools.memory_tool import MemoryStore, memory_tool
    p = load_memory_provider("zvec-memory", register_skills=False)
    assert p is not None and p.is_available()
    store = MemoryStore()
    assert store._path_for("user").resolve().is_relative_to(home)
    manager = MemoryManager()
    manager.add_provider(p)
    native = p._run_zg

    def record_native(args, timeout):
        result = native(args, timeout)
        with (home / "native-calls.jsonl").open("a", encoding="utf-8") as stream:
            stream.write(json.dumps({"args": args, "argv": [p._zg(), *args],
                "cwd": str(p._vault), "pid": os.getpid(), "timeout": timeout, "result": result}) + "\n")
        return result

    monkeypatch.setattr(p, "_run_zg", record_native)
    manager.initialize_all(session_id="origin-session", hermes_home=str(home))

    def refresh():
        assert p._disk_worker.drain(60)
        p._maybe_reindex(force=True)
        assert p._index_worker.drain(120)

    def search(query, glob="facts/**"):
        result = json.loads(manager.handle_tool_call("memory_search", {
            "query": query, "mode": "fts", "globs": [glob], "limit": 5}))
        assert "error" not in result, result
        return _cited_observation(home, vault, query, result)

    def positive(query, path, content):
        observation = search(query, path)
        assert observation["cited_paths"] == [path], observation
        assert content in observation["returned_lines"], observation

    def negative(query, path):
        observation = search(query, path)
        assert observation["cited_paths"] == [], observation
        assert observation["returned_lines"] == [], observation

    def owned_path(content):
        paths = [r["path"] for r in p._mirror_state()["records"].values()
                 if (r["target"], r["content"]) == ("user", content)]
        assert len(paths) == 1, paths
        return paths[0]

    try:
        result = json.loads(manager.handle_tool_call("memory_store", {
            "content": "The synthetic lunar deployment requires a copper canary gate.",
            "category": "project"}))
        assert result["status"] == "stored", result
        assert Path(result["path"]).is_file()
        explicit_path = Path(result["path"]).relative_to(vault).as_posix()
        explicit_bytes = Path(result["path"]).read_bytes()
        refresh()
        positive("copper canary", explicit_path,
                 "The synthetic lunar deployment requires a copper canary gate.")
        context = manager.prefetch_all("What gate does the lunar deployment require?", session_id="origin-session")
        assert "copper canary" in context and len(context) <= 2000
        observation = _cited_observation(home, vault, "What gate does the lunar deployment require?",
            {"results": context.removeprefix("## Zvec Memory\n")}, surface="prefetch_all")
        assert "The synthetic lunar deployment requires a copper canary gate." in observation["returned_lines"]
        manager.sync_all("synthetic old question", "synthetic old reply", session_id="origin-session")
        assert manager.flush_pending(timeout=60)
        p.on_session_switch("new-session")
        assert p._disk_worker.drain(60)
        session_text = "\n".join(f.read_text() for f in (vault / "sessions").glob("*.md"))
        assert "session origin-session" in session_text
        assert "session new-session" not in session_text

        def mirror(action, content="", old_text=""):
            operation = {"action": action, "target": "user"}
            if action != "remove":
                operation["content"] = content
            if old_text:
                operation["old_text"] = old_text
            committed = json.loads(memory_tool(store=store, **operation))
            assert committed.get("success") is True, committed
            manager.notify_memory_tool_write(committed, operation)
            assert manager.flush_pending(timeout=60)
            refresh()
            with (home / "committed-writes.jsonl").open("a", encoding="utf-8") as stream:
                stream.write(json.dumps({"operation": operation, "committed": committed}) + "\n")
            return committed

        selected = "User prefers synthetic scarlet marmalade."
        replacement = "User prefers synthetic violet marzipan."
        longer = "Independent quotation: " + selected + " Preserve this longer record."
        mirror("add", selected)
        selected_path = owned_path(selected)
        positive("scarlet marmalade", selected_path, selected)
        mirror("add", longer)
        longer_path = owned_path(longer)
        longer_bytes = (vault / longer_path).read_bytes()
        replaced = mirror("replace", replacement, selected)
        assert replaced["replaced_entry"] == selected
        negative("scarlet marmalade", selected_path)
        replacement_path = owned_path(replacement)
        positive("violet marzipan", replacement_path, replacement)
        positive("scarlet marmalade", longer_path, longer)
        removed = mirror("remove", old_text=replacement)
        assert removed["removed_entry"] == replacement
        negative("violet marzipan", replacement_path)
        positive("scarlet marmalade", longer_path, longer)
        assert (vault / longer_path).read_bytes() == longer_bytes
        assert set(store.user_entries) == {longer}
        assert (vault / explicit_path).read_bytes() == explicit_bytes
        positive("copper canary", explicit_path,
                 "The synthetic lunar deployment requires a copper canary gate.")
    finally:
        manager.shutdown_all()
    assert not p._disk_worker._thread.is_alive()
    assert not p._index_worker._thread.is_alive()
    fresh = load_memory_provider("zvec-memory", register_skills=False)
    assert fresh.backup_paths() == [str(vault.resolve())]
