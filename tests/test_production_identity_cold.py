"""F5 cold-init integrity controls; native work is a command-aware double.

Read errors are injected at the actual Path.read_text seam with a reach witness,
not claimed kernel EACCES under namespace root. No corrupt-state repair granted.
"""
import json
from pathlib import Path

import pytest

from test_provider import ZvecMemoryProvider

MODEL = "local/potion-retrieval-32m"


def cold_fixture(tmp_path, monkeypatch):
    vault = tmp_path / "vault"
    index = vault / ".zvec-grep"
    index.mkdir(parents=True)
    (index / "manifest.json").write_text("{}")
    sidecar = index / ".zvec-memory-state.json"
    p = ZvecMemoryProvider({"vault": str(vault), "embedding": MODEL, "zg_bin": "/fixture-zg"})
    calls = []

    def engine(args, timeout):
        calls.append(list(args))
        if args == ["--version"]:
            return 0, "0.2.2\n", ""
        if args[0] == "query":
            return 0, "facts/unproven.md:1: unproven index", ""
        return 0, "", ""

    monkeypatch.setattr(p, "_run_zg", engine)
    return p, sidecar, calls


def assert_closed(p, sidecar, calls, before):
    assert p._disk_worker.drain(5) and p._index_worker.drain(5)
    assert sidecar.read_bytes() == before, "cold adoption overwrote present-invalid identity evidence"
    assert p._recall_token() is None, "unproven index was blessed by cold adoption"
    assert "error" in json.loads(p._handle_search({"query": "unproven index"}))
    assert p.prefetch("unproven index") == ""
    p.queue_prefetch("unproven index")
    assert p._disk_worker.drain(5) and p._index_worker.drain(5)
    assert sidecar.read_bytes() == before
    assert not [args for args in calls if args[0] in ("index", "query")]


@pytest.mark.parametrize("text", ["{broken", "[]", "null", "{}", "\"state\"",
    '{"plugin_schema":0,"embedding":"local/potion-retrieval-32m","zg_bin":"/fixture-zg"}',
    '{"plugin_schema":true,"embedding":"local/potion-retrieval-32m","zg_bin":"/fixture-zg"}',
    '{"plugin_schema":1,"embedding":"local/potion-retrieval-32m","zg_bin":"/fixture-zg","index_generation":""}'],
    ids=["malformed", "array", "null", "empty-object", "string", "zero-schema", "bool-schema", "empty-generation"])
def test_F5_present_invalid_cold_init_retains_bytes_and_refuses_recall(tmp_path, monkeypatch, text):
    p, sidecar, calls = cold_fixture(tmp_path, monkeypatch)
    sidecar.write_text(text)
    before = sidecar.read_bytes()
    try:
        p.initialize("cold-invalid", hermes_home=str(tmp_path))
        assert_closed(p, sidecar, calls, before)
        assert not (p._vault / ".reindex-request.json").exists(), "corruption is not automatic rebuild authority"
    finally:
        p.shutdown()


@pytest.mark.parametrize("error", [PermissionError, OSError, IsADirectoryError, FileNotFoundError],
                         ids=["permission", "io", "directory-read", "present-then-read-missing"])
def test_F5_actual_cold_read_failure_is_not_legacy_absence(tmp_path, monkeypatch, error):
    p, sidecar, calls = cold_fixture(tmp_path, monkeypatch)
    sidecar.write_text(json.dumps({"plugin_schema": 1, "embedding": MODEL, "zg_bin": "/fixture-zg"}))
    before = sidecar.read_bytes()
    real_read = Path.read_text
    reached = []

    def fault(path, *args, **kwargs):
        if path == sidecar:
            reached.append(True)
            raise error("private-identity-read-detail")
        return real_read(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", fault)
    try:
        p.initialize("cold-read-failed", hermes_home=str(tmp_path))
        assert reached, "fault did not reach the real identity reader"
        assert_closed(p, sidecar, calls, before)
    finally:
        p.shutdown()


@pytest.mark.parametrize("kind", ["missing-legacy", "valid-v021"])
def test_F5_legacy_missing_and_valid_v021_cold_positives(tmp_path, monkeypatch, kind):
    p, sidecar, calls = cold_fixture(tmp_path, monkeypatch)
    if kind == "valid-v021":
        sidecar.write_text(json.dumps({"plugin_schema": 1, "embedding": MODEL,
            "zg_bin": "/fixture-zg", "zg_version": "0.2.2", "updated": "2026-01-01T00:00:00+00:00"}))
        before = sidecar.read_bytes()
    try:
        p.initialize("cold-legacy", hermes_home=str(tmp_path))
        assert p._disk_worker.drain(5) and p._index_worker.drain(5)
        assert p._recall_token() is not None
        if kind == "valid-v021":
            assert sidecar.read_bytes() == before, "valid legacy metadata must not be restamped"
        else:
            state = json.loads(sidecar.read_text())
            assert state["plugin_schema"] == 1 and state["embedding"] == MODEL
            assert state["index_generation"]
        assert not [args for args in calls if args[0] == "index"]
        assert "results" in json.loads(p._handle_search({"query": "legacy control"}))
    finally:
        p.shutdown()


def test_F5_dangling_sidecar_is_present_not_legacy_missing(tmp_path, monkeypatch):
    p, sidecar, calls = cold_fixture(tmp_path, monkeypatch)
    target = tmp_path / "not-created.json"
    sidecar.symlink_to(target)
    try:
        p.initialize("cold-dangling", hermes_home=str(tmp_path))
        assert p._index_worker.drain(5)
        assert sidecar.is_symlink() and sidecar.readlink() == target
        assert not target.exists()
        assert p._recall_token() is None
        assert not [args for args in calls if args[0] == "index"]
    finally:
        p.shutdown()


def test_F5_explicit_reindex_does_not_silently_repair_invalid_metadata(tmp_path, monkeypatch):
    from zvec_memory_provider.maintenance import request_rebuild
    p, sidecar, calls = cold_fixture(tmp_path, monkeypatch)
    sidecar.write_text("{broken-private-state")
    before = sidecar.read_bytes()
    request = request_rebuild(sidecar.parent.parent)
    request_before = request.read_bytes()
    try:
        p.initialize("cold-explicit-invalid", hermes_home=str(tmp_path))
        assert_closed(p, sidecar, calls, before)
        assert request.read_bytes() == request_before
    finally:
        p.shutdown()


def test_F5_valid_identity_engine_mismatch_keeps_rebuild_and_unknown_version_controls(tmp_path, monkeypatch):
    p, sidecar, native_calls = cold_fixture(tmp_path, monkeypatch)
    try:
        p.initialize("cold-valid-version", hermes_home=str(tmp_path))
        before = sidecar.read_bytes()
        recorded = json.loads(sidecar.read_text())
        calls = []
        monkeypatch.setattr(p, "_maybe_reindex", lambda **kwargs: calls.append(kwargs))
        monkeypatch.setattr(p, "_zg_version", lambda: "0.2.3")
        p._ensure_engine_identity()
        assert calls and calls[-1].get("force") is True
        assert sidecar.read_bytes() == before
        # Retain the actual durable request while exercising the unchanged
        # matched/unknown version scheduling oracles with synthetic metadata.
        marker = p._vault / ".reindex-request.json"
        request_before = marker.read_bytes()
        assert json.loads(marker.read_text())["identity"]["zg_version"] == "0.2.3"
        sidecar.write_text(json.dumps({**recorded, "zg_version": "0.2.3"}))
        calls.clear()
        p._ensure_engine_identity()
        assert calls == []
        sidecar.write_text(json.dumps({**recorded, "zg_version": None}))
        calls.clear()
        p._ensure_engine_identity()
        assert calls == [], "unknown recorded version must not thrash rebuilds"
        assert marker.read_bytes() == request_before and p._recall_token() is None
    finally:
        p.shutdown()


def test_F5_strict_reader_does_not_swallow_unrelated_logic_errors(tmp_path, monkeypatch):
    p, sidecar, calls = cold_fixture(tmp_path, monkeypatch)
    sidecar.write_text(json.dumps({"plugin_schema": 1, "embedding": MODEL, "zg_bin": "/fixture-zg"}))
    p._vault = sidecar.parent.parent
    original = Path.read_text
    def logic(path, *args, **kwargs):
        if path == sidecar:
            raise AssertionError("identity reader logic control")
        return original(path, *args, **kwargs)
    monkeypatch.setattr(Path, "read_text", logic)
    with pytest.raises(AssertionError, match="identity reader logic control"):
        p._engine_state()
