"""F5 real direct engine 0.2.2: cold gates and legacy positives, not daemon acceptance.

Corrupt metadata is retained/refused, never repaired or erased. Model completion
metadata writes use charged private copies of immutable cached test inputs.
"""
import json
import os
from pathlib import Path
import shutil

import pytest

from test_provider import ZvecMemoryProvider
from retrieval_evidence import capture_sources, cited_lines

pytestmark = [pytest.mark.integration, pytest.mark.skipif(
    os.environ.get("ZVEC_RUN_NATIVE") != "1", reason="requires opt-in pinned cached native engine")]
MODEL = "local/potion-retrieval-32m"
FACT = "Synthetic cold identity requires the cobalt compass acceptance gate."


@pytest.mark.parametrize("kind", ["malformed", "array", "empty-object", "schema-zero", "read-denied", "missing-legacy", "valid-v021"])
def test_F5_real_native_cold_identity_gate_and_legacy_controls(tmp_path, monkeypatch, kind):
    cache = tmp_path / "models-copy"
    shutil.copytree(os.environ["ZVEC_TEST_MODEL_CACHE"], cache)
    monkeypatch.setenv("ZVEC_GREP_MODEL_CACHE", str(cache))
    monkeypatch.setenv("ZVEC_GREP_MODE", "direct")
    vault = tmp_path / "native-vault"
    facts = vault / "facts"
    facts.mkdir(parents=True)
    source = facts / "acceptance.md"
    source.write_text("# Synthetic acceptance\n\n" + FACT + "\n")
    source_before = source.read_bytes()
    config = {"vault": str(vault), "zg_bin": os.environ["ZVEC_TEST_BIN"],
              "embedding": MODEL, "reindex_min_seconds": 0, "preview": "full"}
    seed = ZvecMemoryProvider(config)
    cold = None
    calls = []
    observations = []
    try:
        assert seed.is_available()
        seed.initialize("native-seed", hermes_home=os.environ["HERMES_HOME"])
        assert seed._disk_worker.drain(30) and seed._index_worker.drain(90)
        assert seed._zg_version() == "0.2.2"
        assert seed._recall_token() is not None and not seed._index_requested
        result = json.loads(seed._handle_search({"query": "cobalt compass", "mode": "fts"}))
        assert FACT in cited_lines(result, capture_sources(result, vault)), result
        manifest = vault / ".zvec-grep/manifest.json"
        manifest_before = manifest.read_bytes()
        sidecar = seed._engine_state_path()
        valid = json.loads(sidecar.read_text())
        seed.shutdown()
        assert not seed._disk_worker._thread.is_alive() and not seed._index_worker._thread.is_alive()
        if kind in ("malformed", "array", "empty-object"):
            sidecar.write_text({"malformed": "{private-native-state", "array": "[]", "empty-object": "{}"}[kind])
        elif kind == "schema-zero":
            sidecar.write_text(json.dumps({**valid, "plugin_schema": 0}))
        elif kind == "missing-legacy":
            sidecar.unlink()
        elif kind == "valid-v021":
            valid.pop("index_generation")
            sidecar.write_text(json.dumps(valid))
        before = sidecar.read_bytes() if sidecar.exists() else None
        reached = []
        if kind == "read-denied":
            original = Path.read_text
            def deny(path, *args, **kwargs):
                if path == sidecar:
                    reached.append(True)
                    raise PermissionError("private-native-read-detail")
                return original(path, *args, **kwargs)
            monkeypatch.setattr(Path, "read_text", deny)
        cold = ZvecMemoryProvider(config)
        native = cold._run_zg
        def recorded(args, timeout):
            value = native(args, timeout)
            calls.append({"args": list(args), "result": value})
            return value
        monkeypatch.setattr(cold, "_run_zg", recorded)
        cold.initialize("native-cold", hermes_home=os.environ["HERMES_HOME"])
        assert cold._disk_worker.drain(30) and cold._index_worker.drain(90)
        invalid = kind not in ("missing-legacy", "valid-v021")
        if kind == "read-denied":
            assert reached, "actual cold identity reader was not denied"
        if invalid:
            assert sidecar.read_bytes() == before, "real index was relabeled without a rebuild"
            assert cold._recall_token() is None
            result = json.loads(cold._handle_search({"query": "cobalt compass", "mode": "fts"}))
            assert "error" in result, result
            assert cold.prefetch("What gate does cold identity require?") == ""
            assert cold._index_worker.drain(30)
            assert not [c for c in calls if c["args"][0] in ("index", "query")]
            # Independent native read proves stored chunks survive the plugin gate.
            rc, out, err = cold._run_zg(["query", "--mode", "direct", "--refresh", "off",
                "--preview", "full", "--fts=cobalt compass", "-g", "facts/acceptance.md"], timeout=30)
            assert rc == 0, err
            raw = {"results": out}
            assert FACT in cited_lines(raw, capture_sources(raw, vault)), raw
            observations.append(raw)
        else:
            assert cold._recall_token() is not None
            result = json.loads(cold._handle_search({"query": "cobalt compass", "mode": "fts"}))
            assert FACT in cited_lines(result, capture_sources(result, vault)), result
            observations.append(result)
            if kind == "valid-v021":
                assert sidecar.read_bytes() == before
            assert not [c for c in calls if c["args"][0] == "index"]
        assert manifest.read_bytes() == manifest_before
        assert source.read_bytes() == source_before
        # Real version/status subprocesses, without pretending the capped
        # caller's 256-task ceiling qualifies a managed production service.
        from test_cli import load_cli
        cli = load_cli()
        checks = {c["name"]: c for c in cli.collect_checks(vault, config)}
        assert checks["engine"]["ok"] is True and checks["index"]["ok"] is True
        assert checks["identity"]["ok"] is (not invalid)
        assert checks["tasks"]["ok"] is False
        assert manifest.read_bytes() == manifest_before and source.read_bytes() == source_before
        assert not (vault / ".reindex-request.json").exists()
        (tmp_path / "native-identity-observations.json").write_text(json.dumps({
            "kind": kind, "engine": "0.2.2", "model": MODEL, "calls": calls,
            "observations": observations, "invalid_state_retained": invalid,
            "native_rebuild_or_repair_authorized": False, "daemon_acceptance": False}))
    finally:
        seed.shutdown()
        if cold is not None:
            cold.shutdown()
