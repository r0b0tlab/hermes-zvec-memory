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


def test_name_and_schemas(tmp_path):
    p = make_provider(tmp_path)
    try:
        assert p.name == "zvec-memory"
        names = {s["name"] for s in p.get_tool_schemas()}
        assert names == {"memory_search", "memory_store"}
        block = p.system_prompt_block()
        assert "Zvec Memory" in block and "empty" in block
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
        p.shutdown()  # join the warming thread
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
