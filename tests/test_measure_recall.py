"""Offline methodology tests: never invoke zg or a model."""
import importlib.util
import sys
import json
from types import SimpleNamespace
from pathlib import Path

import pytest

SPEC = importlib.util.spec_from_file_location(
    "measure_recall", Path(__file__).resolve().parents[1] / "scripts/measure_recall.py")
assert SPEC is not None and SPEC.loader is not None
benchmark = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = benchmark
SPEC.loader.exec_module(benchmark)


@pytest.mark.parametrize("retrieved,visible,chars,errors,passed", [
    (8, 8, [2000], [], True),
    (7, 8, [2000], [], False),
    (8, 7, [2000], [], False),
    (10, 10, [2001], [], False),
    (10, 10, [100], [{"stderr": "native failure"}], False),
])
def test_quality_gates(retrieved, visible, chars, errors, passed):
    assert hasattr(benchmark, "evaluate_metrics"), "missing pure gate evaluator"
    result = benchmark.evaluate_metrics({"near": {
        "count": 10, "hits": {"hybrid": retrieved, "fts": 0},
        "visible_hits": visible, "chars": chars,
    }}, errors, cap=2000)
    assert result["passed"] is passed
    assert bool(result["failures"]) is not passed


@pytest.mark.parametrize("response", [
    '{"error":"native exploded"}', 'not json', '{"results":null}',
    '{"results":"expected", "success":false}',
])
def test_query_errors_are_not_misses(response):
    p = SimpleNamespace(handle_tool_call=lambda *args: response)
    result = benchmark.run_set(p, p, [("query", "expected")])
    assert isinstance(result, dict), "need structured errors, not miss-only tuple"
    assert len(result["errors"]) == 3
    assert result["hits"]["hybrid"] == 0
    assert result["visible_hits"] == 0
    assert not result["misses"]
    assert result["errors"][0]["raw"] == response


@pytest.mark.parametrize("native_rc", [0, 9])
def test_isolated_benchmark_lifecycle_and_json(tmp_path, monkeypatch, native_rc):
    import os
    import threading
    events = []
    homes = []
    old_home = os.environ["HOME"]
    release = threading.Event()
    worker_done = threading.Event()

    class Provider:
        def __init__(self, config):
            self.config = config
            self.cache = {}
        def initialize(self, session_id, **kwargs):
            home = Path(os.environ["HOME"])
            homes.append(home)
            assert home != Path(old_home)
            assert Path(os.environ["HERMES_HOME"]).is_relative_to(home)
            assert Path(os.environ["XDG_CACHE_HOME"]).is_relative_to(home)
            assert Path(os.environ["ZVEC_GREP_MODEL_CACHE"]).is_relative_to(home)
            assert Path(os.environ["ZVEC_GREP_HOME"]).is_relative_to(home)
            assert os.environ["ZVEC_GREP_MODE"] == "direct"
            assert events[0] == "index"
            self.vault = Path(self.config["vault"])
            assert len(list((self.vault / "facts").glob("*.md"))) == len(benchmark.FACTS)
            events.append("initialize")
            if len(homes) == 1:
                def worker():
                    release.wait(5)
                    assert self.vault.exists()
                    worker_done.set()
                threading.Thread(target=worker).start()
        def handle_tool_call(self, name, args):
            return json.dumps({"results": " ".join(f[1] for f in benchmark.FACTS)})
        def _run_zg(self, args, **kwargs):
            return native_rc, "", "native query failed" if native_rc else ""
        def _cached_prefetch(self, query):
            return self.cache.get(query)
        def prefetch(self, query):
            self._run_zg(["query", query])
            self.cache[query] = ""  # empty successful cache hits count as ready
            return ""
        def queue_prefetch(self, query):
            self.cache[query] = ""
        def shutdown(self):
            assert self.vault.exists()
            events.append("shutdown")
            release.set()

    def native(argv, **kwargs):
        if "index" in argv:
            events.append("index")
            vault = Path(argv[-1])
            assert len(list((vault / "facts").glob("*.md"))) == len(benchmark.FACTS)
        return SimpleNamespace(returncode=0, stdout="test metadata", stderr="")

    assert hasattr(benchmark, "load_provider"), "provider must load after environment isolation"
    monkeypatch.setattr(benchmark, "load_provider", lambda: Provider)
    monkeypatch.setattr(benchmark.subprocess, "run", native)
    output = tmp_path / "result.json"
    assert benchmark.main(["--output", str(output), "--zg-bin", "offline-zg"]) == int(bool(native_rc))
    record = json.loads(output.read_text())
    assert record["gates"]["passed"] is (native_rc == 0)
    if native_rc:
        assert any(e.get("stderr") == "native query failed" for e in record["errors"])
    assert record["provenance"]["plugin_sha"] == "test metadata"
    assert record["prefetch"]["repeated_identical_query"]["cache_ready"] is True
    samples = record["prefetch"]["distinct_next_turn_queries"]["samples"]
    assert [s["query"] for s in samples] == [q for q, _ in benchmark.NEAR_QUERIES]
    assert all(s["cache_hit_before"] is False for s in samples)
    assert events.count("shutdown") == 2
    assert worker_done.is_set()
    assert all(not home.exists() for home in homes)
    assert os.environ.get("HOME") == old_home


def test_repeated_prefetch_context_is_also_gated():
    query = benchmark.PARAPHRASE_QUERIES[0][0]
    p = SimpleNamespace(
        prefetch=lambda q: "x" * 2001 if q == query else "",
        queue_prefetch=lambda q: None,
        _cached_prefetch=lambda q: "",
    )
    errors = []
    benchmark.measure_prefetch(p, errors, timeout=0)
    assert any(e.get("error") == "context exceeds cap" for e in errors)


def test_metric_summary_separates_retrieval_and_visibility():
    full = SimpleNamespace(handle_tool_call=lambda *args: '{"results":"known fact"}')
    capped = SimpleNamespace(handle_tool_call=lambda *args: '{"results":"known"}')
    result = benchmark.run_set(capped, full, [("query", "fact")])
    assert result.get("retrieved_hit_at_5") == {"hybrid": 1.0, "fts": 1.0}
    assert result["visible_hit_at_5"] == 0.0
    assert result["context_chars"] == {"mean": 5.0, "max": 5}


def test_timeout_preserves_raw_command_evidence(tmp_path, monkeypatch):
    def timeout(argv, **kwargs):
        raise benchmark.subprocess.TimeoutExpired(argv, 60, output=b"partial output", stderr=b"actual error")
    monkeypatch.setattr(benchmark.subprocess, "run", timeout)
    monkeypatch.setattr("platform.platform", lambda: "offline-platform")
    output = tmp_path / "failure.json"
    assert benchmark.main(["--output", str(output)]) == 1
    record = json.loads(output.read_text())
    assert record["errors"][0].get("stdout") == "partial output"
    assert record["errors"][0]["stderr"] == "actual error"
    assert not record["gates"]["passed"]
