"""Real Hermes loader + provider + zg lifecycle, no real user memory."""
import json
import os
from pathlib import Path
import shutil
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, os.environ.get("HERMES_AGENT_DIR", str(Path.home() / ".hermes/hermes-agent")))
pytestmark = [pytest.mark.integration, pytest.mark.skipif(
    os.environ.get("ZVEC_RUN_NATIVE") != "1", reason="requires opt-in local model integration")]


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
    monkeypatch.setenv("ZVEC_GREP_MODEL_CACHE", os.environ["ZVEC_TEST_MODEL_CACHE"])
    shutil.copytree(ROOT / "zvec-memory", home / "plugins/zvec-memory")
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
    monkeypatch.setenv("ZVEC_GREP_MODEL_CACHE", os.environ["ZVEC_TEST_MODEL_CACHE"])
    for key in ("ZVEC_GREP_API_KEY", "ZVEC_GREP_ENDPOINT", "ZVEC_GREP_HOME"):
        monkeypatch.delenv(key, raising=False)
    shutil.copytree(ROOT / "zvec-memory", home / "plugins/zvec-memory")
    vault = home / "zvec-memory"
    vault.mkdir()
    config = {"zg_bin": os.environ["ZVEC_TEST_BIN"], "reindex_min_seconds": 0,
              "embedding": "local/potion-retrieval-32m", "context_chars": 2000}
    (vault / "config.json").write_text(json.dumps(config))
    from plugins.memory import load_memory_provider
    from agent.memory_manager import MemoryManager
    p = load_memory_provider("zvec-memory", register_skills=False)
    assert p is not None and p.is_available()
    manager = MemoryManager()
    manager.add_provider(p)
    manager.initialize_all(session_id="origin-session", hermes_home=str(home))

    def refresh():
        assert p._disk_worker.drain(60)
        p._maybe_reindex(force=True)
        assert p._index_worker.drain(120)

    def search(query):
        result = json.loads(manager.handle_tool_call("memory_search", {
            "query": query, "mode": "fts", "globs": ["facts/**"]}))
        assert "error" not in result, result
        # zg echoes the query before hits; never count that echo as retrieval.
        return result["results"].partition("\n#1 ")[2]

    try:
        result = json.loads(manager.handle_tool_call("memory_store", {
            "content": "The synthetic lunar deployment requires a copper canary gate.",
            "category": "project"}))
        assert result["status"] == "stored", result
        assert Path(result["path"]).is_file()
        refresh()
        assert "copper canary" in search("copper canary")
        context = manager.prefetch_all("What gate does the lunar deployment require?", session_id="origin-session")
        assert "copper canary" in context and len(context) <= 2000
        manager.sync_all("synthetic old question", "synthetic old reply", session_id="origin-session")
        assert manager.flush_pending(timeout=60)
        p.on_session_switch("new-session")
        assert p._disk_worker.drain(60)
        session_text = "\n".join(f.read_text() for f in (vault / "sessions").glob("*.md"))
        assert "session origin-session" in session_text
        assert "session new-session" not in session_text

        def mirror(action, content="", old_text=""):
            operation = {"action": action, "target": "user", "content": content}
            if old_text:
                operation["old_text"] = old_text
            manager.notify_memory_tool_write({"success": True}, operation)
            refresh()

        mirror("add", "User prefers synthetic scarlet marmalade.")
        assert "scarlet marmalade" in search("scarlet marmalade")
        mirror("replace", "User prefers synthetic violet marzipan.", "scarlet marmalade")
        assert "scarlet marmalade" not in search("scarlet marmalade")
        assert "violet marzipan" in search("violet marzipan")
        mirror("remove", old_text="violet marzipan")
        assert "violet marzipan" not in search("violet marzipan")
        assert "copper canary" in search("copper canary")
    finally:
        manager.shutdown_all()
    assert not p._disk_worker._thread.is_alive()
    assert not p._index_worker._thread.is_alive()
    fresh = load_memory_provider("zvec-memory", register_skills=False)
    assert fresh.backup_paths() == [str(vault.resolve())]
