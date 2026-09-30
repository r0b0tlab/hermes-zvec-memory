"""Opt-in direct 0.2.2 effects; not daemon/native-owner/full lifecycle acceptance.

Real Hermes loader/manager and native CLI, only synthetic isolated profile data.
The pre-B2 control executes the same supported API and ownership/search oracle;
no native boundary is doubled, and no model is installed or downloaded.
"""
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
from types import SimpleNamespace

import pytest
from source_support import PROVIDER_ROOT

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, os.environ["HERMES_AGENT_DIR"])
sys.path.insert(0, str(ROOT / "scripts"))
from retrieval_evidence import capture_sources, cited_lines, cited_sections

pytestmark = [pytest.mark.integration, pytest.mark.skipif(
    os.environ.get("ZVEC_RUN_NATIVE") != "1", reason="requires opt-in cached local native engine")]
PRE_B2 = "a22eb3501b28484ef4f455600f80cc539b957cd0"
SELECTED = "Synthetic fixture chooses the cobalt compass."
LONGER = "Independent quotation: " + SELECTED + " Preserve this longer record."
REPLACEMENT = "Synthetic fixture chooses the saffron sextant."


def _prepare(home, monkeypatch, product="candidate"):
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("ZVEC_GREP_MODE", "direct")
    # Offline cache validation rewrites ctime-based completion metadata in 0.2.2.
    # Copy cached bytes (not install/download) so all model writes stay in HOME.
    model_cache = home / "native-model-cache"
    shutil.copytree(os.environ["ZVEC_TEST_MODEL_CACHE"], model_cache)
    monkeypatch.setenv("ZVEC_GREP_MODEL_CACHE", str(model_cache))
    for key in ("ZVEC_GREP_API_KEY", "ZVEC_GREP_ENDPOINT", "ZVEC_GREP_HOME",
                "HERMES_ENABLE_PROJECT_PLUGINS"):
        monkeypatch.delenv(key, raising=False)
    destination = home / "plugins/zvec-memory"
    if product == "candidate":
        shutil.copytree(PROVIDER_ROOT / "zvec-memory", destination,
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    else:
        assert product == "pre-b2"
        archive = subprocess.check_output(
            ["/usr/bin/git", "-C", str(ROOT), "archive", PRE_B2, "zvec-memory"], timeout=10)
        (home / "plugins").mkdir()
        with tarfile.open(fileobj=io.BytesIO(archive)) as files:
            files.extractall(home / "plugins", filter="data")
    vault = home / "zvec-memory"
    vault.mkdir()
    (vault / "config.json").write_text(json.dumps({
        "zg_bin": os.environ["ZVEC_TEST_BIN"], "embedding": "local/potion-retrieval-32m",
        "reindex_min_seconds": 0, "preview": "full", "context_chars": 2000}), encoding="utf-8")
    return vault


def _managed(home):
    from plugins.memory import load_memory_provider
    from agent.memory_manager import MemoryManager
    from agent.memory_provider import MemoryProvider
    provider = load_memory_provider("zvec-memory", register_skills=False)
    assert isinstance(provider, MemoryProvider) and provider.is_available()
    manager = MemoryManager()
    manager.add_provider(provider)
    manager.initialize_all("native-identity-fixture", hermes_home=str(home))
    assert provider._vault == (home / "zvec-memory").resolve()
    return SimpleNamespace(home=home, vault=provider._vault, provider=provider, manager=manager)


def _settle(runtime):
    # Drain automatic work only; do not force indexing to rescue an admission gap.
    assert runtime.manager.flush_pending(timeout=10)
    assert runtime.provider._disk_worker.drain(30)
    assert runtime.provider._index_worker.drain(60)
    assert not runtime.provider._index_running
    assert not runtime.provider._index_requested, "automatic native indexing did not finish"
    assert runtime.provider._zg_version() == "0.2.2"


def _invoke(runtime, store, arguments):
    from tools.memory_tool import memory_tool
    result = json.loads(memory_tool(store=store, **arguments))
    assert result.get("success") is True, result
    runtime.manager.notify_memory_tool_write(result, arguments)
    _settle(runtime)
    return result


def _records(runtime):
    return runtime.provider._mirror_state()["records"]


def _record_path(runtime, target, content):
    matches = [r["path"] for r in _records(runtime).values()
               if (r["target"], r["content"]) == (target, content)]
    assert len(matches) == 1, matches
    return matches[0]


def _observe(runtime, query, glob="facts/**", *, raw_native=False):
    if raw_native:
        # Audit real index content while the public recall gate deliberately refuses.
        rc, out, err = runtime.provider._run_zg([
            "query", "--mode", "direct", "--refresh", "off", "--preview", "full",
            "--limit", "5", "--fts=" + query, "-g", glob], timeout=30)
        assert rc == 0, err
        response = {"results": out.strip(), "mode": "fts"}
    else:
        response = json.loads(runtime.manager.handle_tool_call("memory_search", {
            "query": query, "mode": "fts", "globs": [glob], "limit": 5}))
    assert "error" not in response, response
    sources = capture_sources(response, runtime.vault)
    lines = cited_lines(response, sources)  # excludes echoed query, validates complete citations
    sections = list(cited_sections(response["results"]))
    observation = {"query": query, "glob": glob, "response": response, "sources": sources,
                   "cited_paths": [item[0] for item in sections], "returned_lines": lines}
    # Retain actual response and query-time source bytes for independent replay.
    with (runtime.home / "retrieval-observations.jsonl").open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(observation) + "\n")
    return observation


def _positive(runtime, path, content, query):
    observation = _observe(runtime, query, path)
    assert observation["cited_paths"] == [path], observation
    assert content in observation["returned_lines"], observation


class NativeOwnershipMismatch(AssertionError):
    """Only the genuine old-product stale-owned-source oracle may be xfailed."""


def _negative(runtime, path, query):
    observation = _observe(runtime, query, path)
    if observation["cited_paths"]:
        raise NativeOwnershipMismatch("selected owned source is still retrieved", observation)
    assert observation["returned_lines"] == [], observation


@pytest.mark.parametrize("product", ["candidate", pytest.param(
    "pre-b2", marks=pytest.mark.xfail(strict=True, raises=NativeOwnershipMismatch,
        reason="pre-B2 substring ambiguity leaves the selected owned native chunk current"))])
@pytest.mark.parametrize("action", ["replace", "remove"])
def test_native_committed_exact_identity_preserves_overlap_and_target(
        tmp_path, monkeypatch, product, action):
    from tools.memory_tool import MemoryStore
    home = tmp_path / "identity-home"
    _prepare(home, monkeypatch, product)
    runtime = _managed(home)
    store = MemoryStore()
    assert store._path_for("memory").resolve().is_relative_to(home)
    try:
        _settle(runtime)
        (home / "native-run-info.json").write_text(json.dumps({
            "native_version": runtime.provider._zg_version(),
            "embedding": "local/potion-retrieval-32m",
            "product": product if product == "candidate" else PRE_B2}))
        forwarded = []
        original = runtime.provider.on_memory_write

        def observe_forwarding(action, target, content, metadata=None):
            forwarded.append({"action": action, "target": target, "content": content,
                              "metadata": dict(metadata or {})})
            return original(action, target, content, metadata)

        # Observe and delegate the actual host callback; never replace native work.
        monkeypatch.setattr(runtime.provider, "on_memory_write", observe_forwarding)
        for target, content in (("memory", SELECTED), ("memory", LONGER), ("user", SELECTED)):
            _invoke(runtime, store, {"action": "add", "target": target, "content": content})
        selected_path = _record_path(runtime, "memory", SELECTED)
        longer_path = _record_path(runtime, "memory", LONGER)
        user_path = _record_path(runtime, "user", SELECTED)
        unaffected = {p: (runtime.vault / p).read_bytes() for p in (longer_path, user_path)}
        _positive(runtime, selected_path, SELECTED, "cobalt compass")
        _positive(runtime, longer_path, LONGER, "cobalt compass")
        _positive(runtime, user_path, SELECTED, "cobalt compass")
        explicit = json.loads(runtime.manager.handle_tool_call("memory_store", {
            "content": "Synthetic explicit fact preserves the obsidian lantern."}))
        assert explicit["status"] == "stored", explicit
        explicit_path = Path(explicit["path"]).relative_to(runtime.vault).as_posix()
        _settle(runtime)
        explicit_bytes = (runtime.vault / explicit_path).read_bytes()
        arguments = {"action": action, "target": "memory", "old_text": SELECTED}
        if action == "replace":
            arguments["content"] = REPLACEMENT
        result = _invoke(runtime, store, arguments)
        assert result["replaced_entry" if action == "replace" else "removed_entry"] == SELECTED
        assert forwarded[-1]["metadata"]["previous_content"] == SELECTED
        assert forwarded[-1]["metadata"]["old_text"] == SELECTED
        (home / "committed-forwarding.json").write_text(json.dumps({
            "arguments": arguments, "result": result, "forwarded": forwarded[-1]}))
        assert set(store.memory_entries) == ({LONGER, REPLACEMENT} if action == "replace" else {LONGER})
        assert set(store.user_entries) == {SELECTED}
        # First failing old-product oracle is actual indexed search, not a new API argument.
        _negative(runtime, selected_path, "cobalt compass")
        assert not (runtime.vault / selected_path).exists()
        assert {(r["target"], r["content"]) for r in _records(runtime).values()} == (
            {("memory", LONGER), ("user", SELECTED)} |
            ({("memory", REPLACEMENT)} if action == "replace" else set()))
        for path, data in unaffected.items():
            assert (runtime.vault / path).read_bytes() == data
        assert (runtime.vault / explicit_path).read_bytes() == explicit_bytes
        _positive(runtime, longer_path, LONGER, "cobalt compass")
        _positive(runtime, user_path, SELECTED, "cobalt compass")
        _positive(runtime, explicit_path, "Synthetic explicit fact preserves the obsidian lantern.", "obsidian lantern")
        if action == "replace":
            _positive(runtime, _record_path(runtime, "memory", REPLACEMENT), REPLACEMENT, "saffron sextant")
    finally:
        runtime.manager.shutdown_all()


@pytest.mark.parametrize("action", ["replace", "remove"])
def test_native_malformed_identity_refused_without_fallback_or_mutation(tmp_path, monkeypatch, action):
    from tools.memory_tool import MemoryStore
    home = tmp_path / "refusal-home"
    _prepare(home, monkeypatch)
    runtime = _managed(home)
    store = MemoryStore()
    try:
        for content in (SELECTED, LONGER):
            _invoke(runtime, store, {"action": "add", "target": "memory", "content": content})
        path = _record_path(runtime, "memory", SELECTED)
        _positive(runtime, path, SELECTED, "cobalt compass")
        before = runtime.provider._mirror_state()
        files = {p.name: p.read_bytes() for p in (runtime.vault / "facts").glob("*.md")}
        malformed = [{}, {"old_text": "cobalt compass"}]
        malformed += [{"previous_content": value, "old_text": SELECTED}
                      for value in (None, "", 42, [], {})]
        for metadata in malformed:
            with pytest.raises(ValueError, match="Exact previous|not an exact"):
                runtime.provider._apply_mirror(action, "memory", REPLACEMENT, metadata)
            assert runtime.provider._mirror_state() == before
            assert {p.name: p.read_bytes() for p in (runtime.vault / "facts").glob("*.md")} == files
        # These are no-op selectors, not malformed authoritative replacement adds.
        runtime.provider._apply_mirror("remove", "user", "", {"previous_content": SELECTED})
        runtime.provider._apply_mirror("remove", "memory", "", {"previous_content": "Synthetic absent entry."})
        assert runtime.provider._mirror_state() == before
        _positive(runtime, path, SELECTED, "cobalt compass")
        _positive(runtime, _record_path(runtime, "memory", LONGER), LONGER, "cobalt compass")
        # 0.2.2 FTS is ranked retrieval and may return unrelated candidates.
        # Absence is about complete cited source bodies, not hit count.
        observation = _observe(runtime, "saffron sextant")
        assert REPLACEMENT not in observation["returned_lines"]
        assert all("saffron sextant" not in line for line in observation["returned_lines"])
    finally:
        runtime.manager.shutdown_all()


@pytest.mark.parametrize("metadata", [{}, {"previous_content": None, "old_text": SELECTED}],
                         ids=["missing", "malformed-authoritative"])
def test_native_unresolved_delivery_stays_pending_and_recall_closed(tmp_path, monkeypatch, metadata):
    from tools.memory_tool import MemoryStore
    home = tmp_path / "pending-home"
    _prepare(home, monkeypatch)
    runtime = _managed(home)
    store = MemoryStore()
    try:
        _invoke(runtime, store, {"action": "add", "target": "memory", "content": SELECTED})
        path = _record_path(runtime, "memory", SELECTED)
        _positive(runtime, path, SELECTED, "cobalt compass")
        before = runtime.provider._mirror_state()
        data = (runtime.vault / path).read_bytes()
        runtime.provider.on_memory_write("remove", "memory", "", metadata)
        assert runtime.provider._disk_worker.drain(30)
        assert runtime.provider._index_worker.drain(30)
        assert runtime.provider._mirror_inbox.pending(), "unresolved destructive intent must not be acknowledged"
        response = json.loads(runtime.manager.handle_tool_call("memory_search", {
            "query": "cobalt compass", "mode": "fts", "globs": [path]}))
        assert "Mirror cleanup incomplete" in response.get("error", ""), response
        assert runtime.provider._index_worker.drain(30)
        assert runtime.provider._mirror_state() == before
        assert (runtime.vault / path).read_bytes() == data
        observation = _observe(runtime, "cobalt compass", path, raw_native=True)
        assert observation["cited_paths"] == [path]
        assert SELECTED in observation["returned_lines"]
        assert runtime.provider._mirror_inbox.pending()
    finally:
        runtime.manager.shutdown_all()


def _spawn_committed_writer(home, target, action, connection):
    """Independent host producer; deliberately exits after durable mirror admission."""
    os.environ["HOME"] = home
    os.environ["HERMES_HOME"] = home
    from tools.memory_tool import MemoryStore, memory_tool
    runtime = _managed(Path(home))
    arguments = {"action": action, "target": target, "old_text": SELECTED}
    if action == "replace":
        arguments["content"] = REPLACEMENT
    result = json.loads(memory_tool(store=MemoryStore(), **arguments))
    assert result.get("success") is True, result
    assert result["replaced_entry" if action == "replace" else "removed_entry"] == SELECTED
    runtime.manager.notify_memory_tool_write(result, arguments)
    assert runtime.manager.flush_pending(timeout=10)
    connection.send({"pid": os.getpid(), "result": result, "target": target, "action": action})
    connection.close()
    # No graceful disk/index drain: the other process owns the real vault lock.
    os._exit(0)


def test_native_spawned_committed_mirror_writers_recover_automatically(tmp_path, monkeypatch):
    import multiprocessing
    import time
    from tools.memory_tool import MemoryStore
    home = tmp_path / "spawn-home"
    _prepare(home, monkeypatch)
    runtime = _managed(home)
    store = MemoryStore()
    context = multiprocessing.get_context("spawn")
    children, readers, acknowledgments = [], [], []
    try:
        for target, content in (("memory", SELECTED), ("memory", LONGER), ("user", SELECTED)):
            _invoke(runtime, store, {"action": "add", "target": target, "content": content})
        memory_path = _record_path(runtime, "memory", SELECTED)
        user_path = _record_path(runtime, "user", SELECTED)
        longer_path = _record_path(runtime, "memory", LONGER)
        longer_bytes = (runtime.vault / longer_path).read_bytes()
        _positive(runtime, memory_path, SELECTED, "cobalt compass")
        _positive(runtime, user_path, SELECTED, "cobalt compass")
        before = runtime.provider._mirror_state()
        # Real inter-process flock blocks publication/indexing, not inbox admission.
        with runtime.provider._vault_lock:
            for target, action in (("user", "replace"), ("memory", "remove")):
                reader, writer = context.Pipe(duplex=False)
                child = context.Process(target=_spawn_committed_writer,
                                        args=(str(home), target, action, writer))
                children.append(child)
                readers.append(reader)
                child.start()
                writer.close()
            deadline = time.monotonic() + 30
            for child, reader in zip(children, readers):
                assert reader.poll(max(0, deadline - time.monotonic())), "writer did not admit committed notification"
                acknowledgments.append(reader.recv())
                child.join(max(0, deadline - time.monotonic()))
                assert child.exitcode == 0, child.exitcode
            assert len({a["pid"] for a in acknowledgments}) == 2
            assert all(a["pid"] != os.getpid() for a in acknowledgments)
            assert runtime.provider._mirror_inbox.pending()
            assert runtime.provider._mirror_state() == before
            assert (runtime.vault / memory_path).is_file() and (runtime.vault / user_path).is_file()
        runtime.manager.shutdown_all()
        # New real host instance discovers peer inbox work at startup; no force refresh.
        recovered = _managed(home)
        try:
            _settle(recovered)
            assert not recovered.provider._mirror_inbox.pending()
            state = recovered.provider._mirror_state()
            assert not state["refresh_required"] and not state["pending_deletes"] and not state["pending_creates"]
            assert {(r["target"], r["content"]) for r in state["records"].values()} == {
                ("memory", LONGER), ("user", REPLACEMENT)}
            assert (recovered.vault / longer_path).read_bytes() == longer_bytes
            _negative(recovered, memory_path, "cobalt compass")
            _negative(recovered, user_path, "cobalt compass")
            _positive(recovered, longer_path, LONGER, "cobalt compass")
            _positive(recovered, _record_path(recovered, "user", REPLACEMENT), REPLACEMENT, "saffron sextant")
            native_store = MemoryStore()
            native_store.load_from_disk()
            assert set(native_store.memory_entries) == {LONGER}
            assert set(native_store.user_entries) == {REPLACEMENT}
            (home / "spawn-observations.json").write_text(json.dumps({
                "acknowledgments": acknowledgments, "native_version": recovered.provider._zg_version(),
                "recovered_state": state, "automatic_recovery": True}))
        finally:
            recovered.manager.shutdown_all()
    finally:
        runtime.manager.shutdown_all()
        for child in children:
            if child.is_alive():
                child.terminate()
                child.join(5)
                if child.is_alive():
                    child.kill()
                    child.join(5)
            assert not child.is_alive(), "test-owned writer cleanup incomplete"
            child.close()
        for reader in readers:
            reader.close()
