"""Actual cached 0.2.2 native publication/restart and same-model recovery.

Only plugin publication/identity artifacts are faulted. Every native call is
observed and delegated unchanged; no native status, manifest or success double.
Not an alternative-model, warm-daemon, native-lease, soak or release gate.
"""
import importlib
import json
import multiprocessing
import os
from pathlib import Path
import threading
from types import SimpleNamespace

import pytest

from test_native_identity_finish import _prepare, _settle, _record_path
from test_provider_native import _cited_observation

pytestmark = [pytest.mark.integration, pytest.mark.skipif(
    os.environ.get("ZVEC_RUN_NATIVE") != "1",
    reason="requires opt-in cached local native engine")]
MODEL = "local/potion-retrieval-32m"
OLD = "Synthetic rebuild fixture chooses the cobalt compass."
NEW = "Synthetic rebuild fixture chooses the saffron sextant."
KEEP = "Synthetic rebuild fixture preserves the obsidian lantern."


def _append(home, name, value):
    with (home / name).open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(value) + "\n")


def _snapshot(home, vault, stage):
    files = {}
    for name in (".zvec-grep/manifest.json", ".zvec-grep/.zvec-memory-state.json",
                 ".reindex-request.json", ".index-refresh.json", ".mirror-map.json"):
        path = vault / name
        raw = path.read_bytes() if path.exists() else None
        files[name] = {"raw_utf8": raw.decode("utf-8") if raw is not None else None,
                       "value": json.loads(raw) if raw is not None else None}
    observation = {"stage": stage, "files": files}
    _append(home, "artifact-observations.jsonl", observation)
    return observation


def _value(snapshot, name):
    return snapshot["files"][name]["value"]


def _assert_native_identity(snapshot):
    manifest = _value(snapshot, ".zvec-grep/manifest.json")
    assert manifest["manifestVersion"] == 1
    assert manifest["embedding"] == {"provider": "local", "model": "potion-retrieval-32m",
                                     "dimension": 512, "metric": "cosine"}
    sidecar = _value(snapshot, ".zvec-grep/.zvec-memory-state.json")
    assert sidecar["embedding"] == MODEL
    assert sidecar["plugin_schema"] == 1 and sidecar["zg_version"] == "0.2.2"
    assert sidecar["index_generation"]
    return sidecar


def _managed(home, monkeypatch, label, *, supersede=False, publication_barrier=None):
    from plugins.memory import load_memory_provider
    from agent.memory_manager import MemoryManager
    from agent.memory_provider import MemoryProvider
    p = load_memory_provider("zvec-memory", register_skills=False)
    assert isinstance(p, MemoryProvider) and p.is_available()
    module = importlib.import_module(type(p).__module__)
    maintenance = importlib.import_module(type(p).__module__ + ".maintenance")
    cli = importlib.import_module(type(p).__module__ + ".cli")
    vault = home / "zvec-memory"
    calls, acknowledgments, replacements = [], [], []
    original_native = p._run_zg
    original_ack = module.acknowledge_request

    def native(args, timeout):
        request = maintenance.read_request(vault) if args[0] == "index" else None
        refresh = maintenance.read_refresh(vault) if args[0] == "index" else None
        result = original_native(args, timeout)
        call = {"owner": label, "pid": os.getpid(), "args": list(args),
                "argv": [p._zg(), *args], "cwd": str(vault), "timeout": timeout,
                "result": list(result), "captured_request": request.decode() if request else None,
                "captured_refresh": refresh.decode() if refresh else None}
        calls.append(call)
        _append(home, "native-calls.jsonl", call)
        if supersede and args[0] == "index" and result[0] == 0 and not replacements:
            # Real generic CLI producer supersedes only request generation,
            # after actual native completion and before plugin publication.
            cli._request_reindex(vault)
            newer = maintenance.read_request(vault)
            assert newer is not None and newer != request
            assert maintenance.request_identity(newer) == maintenance.request_identity(request)
            replacements.append({"old": request.decode(), "new": newer.decode()})
            _append(home, "request-replacements.jsonl", replacements[-1])
        if publication_barrier and args[0] == "index" and result[0] == 0:
            completed, release = publication_barrier
            if not completed.is_set():
                completed.set()
                assert release.wait(30), "test-only post-native publication barrier expired"
        return result

    def acknowledge(target, completed, *, identity=None):
        current = maintenance.read_request(target)
        result = original_ack(target, completed, identity=identity)
        after = maintenance.read_request(target)
        event = {"owner": label, "completed": completed.decode() if completed else None,
                 "current": current.decode() if current else None,
                 "after": after.decode() if after else None, "identity": identity, "result": result}
        acknowledgments.append(event)
        _append(home, "request-acknowledgments.jsonl", event)
        return result

    monkeypatch.setattr(p, "_run_zg", native)
    monkeypatch.setattr(module, "acknowledge_request", acknowledge)
    manager = MemoryManager()
    manager.add_provider(p)
    manager.initialize_all(label, hermes_home=str(home))
    assert p._vault == vault.resolve()
    return SimpleNamespace(home=home, vault=vault, provider=p, manager=manager,
                           module=module, maintenance=maintenance, cli=cli, calls=calls,
                           acknowledgments=acknowledgments, replacements=replacements)


def _commit(runtime, store, action, content="", old_text=""):
    from tools.memory_tool import memory_tool
    arguments = {"action": action, "target": "user"}
    if action != "remove":
        arguments["content"] = content
    if old_text:
        arguments["old_text"] = old_text
    result = json.loads(memory_tool(store=store, **arguments))
    assert result.get("success") is True, result
    runtime.manager.notify_memory_tool_write(result, arguments)
    assert runtime.manager.flush_pending(timeout=10)
    assert runtime.provider._disk_worker.drain(30)
    assert runtime.provider._index_worker.drain(60)
    _append(runtime.home, "committed-writes.jsonl", {"arguments": arguments, "result": result})
    return result


def _observe(runtime, query, path, *, raw_native=False):
    if raw_native:
        rc, out, err = runtime.provider._run_zg([
            "query", "--mode", "direct", "--refresh", "off", "--preview", "full",
            "--limit", "5", "--fts=" + query, "-g", path], timeout=30)
        assert rc == 0, err
        response = {"results": out.strip(), "mode": "fts"}
    else:
        response = json.loads(runtime.manager.handle_tool_call("memory_search", {
            "query": query, "mode": "fts", "globs": [path], "limit": 5}))
    assert "error" not in response, response
    return _cited_observation(runtime.home, runtime.vault, query, response,
        surface="direct_cli" if raw_native else "memory_search", raw_native=raw_native)


def _positive(runtime, path, text, query, *, raw_native=False):
    observation = _observe(runtime, query, path, raw_native=raw_native)
    assert observation["cited_paths"] == [path], observation
    assert text in observation["returned_lines"], observation


def _negative(runtime, path, query, *, raw_native=False):
    observation = _observe(runtime, query, path, raw_native=raw_native)
    assert observation["cited_paths"] == [] and observation["returned_lines"] == [], observation


def _ready(runtime):
    rc, out, err = runtime.provider._run_zg(
        ["status", str(runtime.vault), "--check-ready"], timeout=30)
    _append(runtime.home, "native-readiness.jsonl", {"rc": rc, "out": out, "err": err})
    assert rc == 0, (out, err)


def _closed(runtime, *, drain=True):
    assert runtime.provider._recall_token() is None
    response = json.loads(runtime.manager.handle_tool_call("memory_search", {
        "query": "cobalt compass", "mode": "fts", "globs": ["facts/**"]}))
    _append(runtime.home, "public-fences.jsonl", {"surface": "memory_search", "response": response})
    assert "Mirror cleanup incomplete" in response.get("error", ""), response
    assert runtime.manager.prefetch_all("What synthetic fixture is stored?") == ""
    if drain:
        assert runtime.provider._index_worker.drain(60)


def _rebuilt(runtime):
    indices = [c for c in runtime.calls if c["args"][0] == "index"]
    assert indices and all(c["result"][0] == 0 for c in indices), indices
    assert all("--rebuild" in c["args"] and
               c["args"][c["args"].index("--embedding") + 1] == MODEL for c in indices)
    for call in indices:
        assert call["captured_request"] and call["captured_refresh"]
        intent = json.loads(call["captured_refresh"])
        assert intent["operation"] == "rebuild" and intent["identity"]["embedding"] == MODEL
    for acknowledgment in runtime.acknowledgments:
        assert acknowledgment["completed"] in [c["captured_request"] for c in indices]
        if acknowledgment["result"]:
            assert acknowledgment["completed"] == acknowledgment["current"]
            assert acknowledgment["after"] is None
            bound = runtime.maintenance.request_identity(acknowledgment["completed"])
            assert bound is None or all(acknowledgment["identity"].get(k) == v for k, v in bound.items())
    assert any(a["result"] for a in runtime.acknowledgments)
    assert runtime.provider._recall_token() is not None
    assert runtime.maintenance.read_request(runtime.vault) is None
    assert runtime.maintenance.read_refresh(runtime.vault) is None
    state = runtime.provider._mirror_state()
    assert not state["refresh_required"] and not state["pending_deletes"] and not state["pending_creates"]
    assert not runtime.provider._mirror_inbox.pending()
    _ready(runtime)


def _cold_recovery_process(home, connection, specification):
    """New interpreter/host/provider, consuming only durable on-disk intent."""
    from tools.memory_tool import MemoryStore
    runtime = None
    completed, release = threading.Event(), threading.Event()
    try:
        with pytest.MonkeyPatch.context() as patch:
            runtime = _managed(Path(home), patch, "cold-recovery",
                               supersede=specification["supersede"],
                               publication_barrier=(completed, release))
            assert completed.wait(30), "cold recovery did not complete actual native work"
            try:
                # Pause only after the actual native success, before returning
                # unchanged to plugin publication; public readers remain shut
                # in this fresh interpreter even though native bytes are ready.
                _closed(runtime, drain=False)
                _snapshot(runtime.home, runtime.vault, "cold-process-publication-pending-fenced")
            finally:
                release.set()
            _settle(runtime)  # startup discovery alone; no forced refresh
            _rebuilt(runtime)
            after = _snapshot(runtime.home, runtime.vault, "cold-process-recovered")
            final = _assert_native_identity(after)
            assert final["index_generation"] != specification["previous_generation"]
            _negative(runtime, specification["old_path"], "cobalt compass")
            _positive(runtime, specification["new_path"], NEW, "saffron sextant")
            if specification["supersede"]:
                assert len(runtime.replacements) == 1
                replacement = runtime.replacements[0]
                first = next(a for a in runtime.acknowledgments if a["completed"] == replacement["old"])
                assert first["result"] is False and first["after"] == replacement["new"]
                assert any(a["completed"] == replacement["new"] and a["result"]
                           for a in runtime.acknowledgments)
            else:
                assert any(c["captured_request"] == specification["requested"]
                           for c in runtime.calls if c["args"][0] == "index")
                path = specification["keep_path"]
                _positive(runtime, path, KEEP, "obsidian lantern")
                assert (runtime.vault / path).read_text() == specification["keep_text"]
                disk_store = MemoryStore()
                disk_store.load_from_disk()
                assert set(disk_store.user_entries) == {NEW, KEEP}
            runtime.manager.shutdown_all()
            assert not runtime.provider._disk_worker._thread.is_alive()
            assert not runtime.provider._index_worker._thread.is_alive()
            connection.send({"pid": os.getpid(), "after": after,
                "indices": [c for c in runtime.calls if c["args"][0] == "index"],
                "acknowledgments": runtime.acknowledgments, "replacements": runtime.replacements})
    except BaseException:
        import traceback
        connection.send({"error": traceback.format_exc()})
        raise
    finally:
        release.set()
        if runtime is not None:
            runtime.manager.shutdown_all()
        connection.close()


def _cold_recover(home, specification):
    context = multiprocessing.get_context("spawn")
    reader, writer = context.Pipe(duplex=False)
    child = context.Process(target=_cold_recovery_process, args=(str(home), writer, specification))
    try:
        child.start()
        writer.close()
        assert reader.poll(90), "cold recovery process did not finish within the original rung"
        result = reader.recv()
        child.join(10)
        assert "error" not in result, result
        assert not child.is_alive() and child.exitcode == 0, child.exitcode
        assert result["pid"] != os.getpid()
        assert all(c["pid"] == result["pid"] for c in result["indices"])
        _append(home, "cold-process-result.jsonl", result)
        return result
    finally:
        if child.is_alive():
            child.terminate()
            child.join(5)
            if child.is_alive():
                child.kill()
                child.join(5)
        assert not child.is_alive(), "test-owned cold recovery child cleanup incomplete"
        child.close()
        reader.close()
        writer.close()


@pytest.mark.parametrize("after_publication", [False, True], ids=["before-sidecar", "after-sidecar"])
def test_native_publication_failure_retains_intent_and_cold_restart_rebuilds(
        tmp_path, monkeypatch, after_publication):
    from tools.memory_tool import MemoryStore
    home = tmp_path / "publication-home"
    _prepare(home, monkeypatch)
    runtime = _managed(home, monkeypatch, "publication-owner")
    try:
        _settle(runtime)
        store = MemoryStore()
        _commit(runtime, store, "add", OLD)
        _commit(runtime, store, "add", KEEP)
        _settle(runtime)
        old_path = _record_path(runtime, "user", OLD)
        keep_path = _record_path(runtime, "user", KEEP)
        keep_bytes = (runtime.vault / keep_path).read_bytes()
        _positive(runtime, old_path, OLD, "cobalt compass")
        _positive(runtime, keep_path, KEEP, "obsidian lantern")
        _ready(runtime)
        before = _snapshot(home, runtime.vault, "before-fault")
        previous = _assert_native_identity(before)
        runtime.cli._request_reindex(runtime.vault)
        requested = runtime.maintenance.read_request(runtime.vault)
        original = runtime.provider._record_engine_state
        faults = []

        def publication(identity=None):
            # This seam is reached only after the unchanged real native call
            # succeeded. Leave all captured request/refresh bytes untouched.
            indices = [c for c in runtime.calls if c["args"][0] == "index"]
            assert indices[-1]["result"][0] == 0 and "--rebuild" in indices[-1]["args"]
            if after_publication:
                original(identity)
            faults.append(_snapshot(home, runtime.vault, "injected-publication-fault"))
            raise OSError("test-only plugin generation publication failure")

        with monkeypatch.context() as fault_patch:
            fault_patch.setattr(runtime.provider, "_record_engine_state", publication)
            committed = _commit(runtime, store, "replace", NEW, OLD)
            assert committed["replaced_entry"] == OLD
            assert faults and runtime.provider._index_requested
            assert not runtime.provider._index_running
            assert runtime.maintenance.read_request(runtime.vault) == requested
            assert runtime.maintenance.read_refresh(runtime.vault) is not None
            assert runtime.provider._mirror_state()["refresh_required"]
            _closed(runtime)
            new_path = _record_path(runtime, "user", NEW)
            _negative(runtime, old_path, "cobalt compass", raw_native=True)
            _positive(runtime, new_path, NEW, "saffron sextant", raw_native=True)
            _positive(runtime, keep_path, KEEP, "obsidian lantern", raw_native=True)
            failed = _snapshot(home, runtime.vault, "failed-before-shutdown")
            failed_state = _assert_native_identity(failed)
            assert (failed_state["index_generation"] != previous["index_generation"]) == after_publication
            assert (runtime.vault / keep_path).read_bytes() == keep_bytes
            assert runtime.maintenance.read_request(runtime.vault) == requested
            # Keep injection active throughout shutdown: no forced rescue.
            runtime.manager.shutdown_all()
        assert not runtime.provider._disk_worker._thread.is_alive()
        assert not runtime.provider._index_worker._thread.is_alive()
        closed = _snapshot(home, runtime.vault, "closed-owner")
        assert closed["files"] == failed["files"]
        assert all(
            a["completed"] != requested.decode() for a in runtime.acknowledgments)
        _cold_recover(home, {"supersede": False, "requested": requested.decode(),
            "previous_generation": failed_state["index_generation"], "old_path": old_path,
            "new_path": new_path, "keep_path": keep_path, "keep_text": keep_bytes.decode()})
    finally:
        runtime.manager.shutdown_all()


def test_native_same_model_invalid_identity_recovers_and_only_acknowledges_captured_intent(
        tmp_path, monkeypatch):
    from tools.memory_tool import MemoryStore
    home = tmp_path / "identity-recovery-home"
    _prepare(home, monkeypatch)
    runtime = _managed(home, monkeypatch, "identity-owner")
    try:
        _settle(runtime)
        store = MemoryStore()
        _commit(runtime, store, "add", OLD)
        old_path = _record_path(runtime, "user", OLD)
        _commit(runtime, store, "replace", NEW, OLD)
        new_path = _record_path(runtime, "user", NEW)
        _settle(runtime)
        _negative(runtime, old_path, "cobalt compass")
        _positive(runtime, new_path, NEW, "saffron sextant")
        before = _snapshot(home, runtime.vault, "before-same-model-invalidation")
        valid = _assert_native_identity(before)
        runtime.manager.shutdown_all()
        # Explicit test-only isolated plugin metadata invalidation. Native
        # embedding bytes and engine version are not forged or changed.
        sidecar = runtime.provider._engine_state_path()
        invalid = {**valid, "plugin_schema": 0}
        sidecar.write_text(json.dumps(invalid), encoding="utf-8")
        invalidated = _snapshot(home, runtime.vault, "invalid-plugin-schema-same-native-model")
        assert invalidated["files"][".zvec-grep/manifest.json"] == before["files"][".zvec-grep/manifest.json"]
        assert _value(invalidated, ".reindex-request.json") is None
        assert _value(invalidated, ".index-refresh.json") is None
        assert invalid["embedding"] == valid["embedding"] == MODEL
        result = _cold_recover(home, {"supersede": True,
            "previous_generation": valid["index_generation"], "old_path": old_path, "new_path": new_path})
        final = _assert_native_identity(result["after"])
        assert final["embedding"] == valid["embedding"]
        _append(home, "scope.jsonl", {"same_model_metadata_recovery": True,
            "actual_alternative_model_switch": False,
            "alternative_model_dimension": "blocked: no preseeded explicitly approved second model"})
    finally:
        runtime.manager.shutdown_all()


@pytest.mark.parametrize("case", ["hybrid", "fts", "vector", "scope-unicode",
                                 "excludes-config", "deleted-fact", "concurrent-refresh"])
def test_existing_native_cli_neighbor_with_private_model_cache(
        tmp_path, tmp_path_factory, monkeypatch, case):
    """Execute unchanged legacy native oracles without mutating cached inputs.

    Its module fixture otherwise points the 0.2.2 ctime-marker writer straight
    at ZVEC_TEST_MODEL_CACHE. Only redirect that input to this case's byte copy;
    the original native fixture, CLI calls and assertion functions run intact.
    These legacy assertions are neighbors, not new source-attribution proof.
    """
    import test_zg_native as neighbor
    home = tmp_path / "legacy-native-private-cache"
    _prepare(home, monkeypatch)
    monkeypatch.setenv("ZVEC_TEST_MODEL_CACHE", str(home / "native-model-cache"))
    native = neighbor.native.__wrapped__(tmp_path_factory)
    if case in {"hybrid", "fts", "vector"}:
        neighbor.test_native_retrieval_modes(native, case)
    else:
        functions = {"scope-unicode": neighbor.test_native_scope_unicode_and_option_like_query,
                     "excludes-config": neighbor.test_native_markdown_selection_excludes_config,
                     "deleted-fact": neighbor.test_native_refresh_removes_deleted_fact,
                     "concurrent-refresh": neighbor.test_native_concurrent_refresh_reports_lock_or_success}
        functions[case](native)
