"""Offline compatibility tests against a real Hermes checkout, never host doubles.

Run each checkout in a fresh interpreter (host modules cache import state)::

    HERMES_AGENT_DIR=/path/to/hermes-agent .venv/bin/python -m pytest \
        tests/test_host_contract.py -q

Requires pytest, PyYAML, python-dotenv, rich and FastAPI in the test venv.
The only functional substitute is the native zg boundary. Socket/process guards
fail closed if a supposedly offline code path attempts external work.
"""

import importlib
import json
import os
from pathlib import Path
from source_support import PROVIDER_ROOT
import shutil
import socket
import subprocess
import sys
import threading
from types import SimpleNamespace

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
HOST_ROOT = Path(os.environ.get(
    "HERMES_AGENT_DIR", str(Path.home() / ".hermes" / "hermes-agent")
)).resolve()


@pytest.fixture
def host(tmp_path, monkeypatch):
    """Resolve the real host only after all filesystem roots are isolated."""
    assert (HOST_ROOT / "agent" / "memory_provider.py").is_file(), HOST_ROOT
    monkeypatch.syspath_prepend(str(HOST_ROOT))
    # The host creates $HERMES_HOME subdirs (skills/, logs/, ...) the first time
    # hermes_cli.config is imported. Warm that import before pointing HERMES_HOME at
    # this test's profile, so the assertions below measure the provider's own
    # behaviour instead of the host's one-time home bootstrap.
    importlib.import_module("hermes_cli.config")
    home = tmp_path / "profile"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.delenv("HERMES_ENABLE_PROJECT_PLUGINS", raising=False)
    monkeypatch.chdir(tmp_path)
    attempts = []

    def forbidden(*args, **kwargs):
        attempts.append((args, kwargs))
        raise AssertionError("Offline host contract attempted network or subprocess")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    destination = home / "plugins" / "zvec-memory"
    shutil.copytree(PROVIDER_ROOT / "zvec-memory", destination,
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    discovery = importlib.import_module("plugins.memory")
    base = importlib.import_module("agent.memory_provider")
    assert Path(base.__file__).resolve().is_relative_to(HOST_ROOT)
    assert Path(discovery.__file__).resolve().is_relative_to(HOST_ROOT)
    yield SimpleNamespace(home=home, plugin=destination, discovery=discovery,
                          base=base, tmp=tmp_path)
    assert not attempts, "An offline call was attempted, even if host swallowed it"


def load(host):
    provider = host.discovery.load_memory_provider("zvec-memory", register_skills=False)
    assert isinstance(provider, host.base.MemoryProvider)
    return provider


def write_native(host, values):
    path = host.home / "zvec-memory" / "config.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(values), encoding="utf-8")
    return path


@pytest.fixture
def managed(host, monkeypatch):
    from agent.memory_manager import MemoryManager

    write_native(host, {"zg_bin": sys.executable, "auto_extract": False})
    vault = host.home / "zvec-memory"
    # Offline sentinel only: this does NOT establish real native index readiness.
    manifest = vault / ".zvec-grep" / "manifest.json"
    manifest.parent.mkdir()
    manifest.write_text("{}", encoding="utf-8")
    provider = load(host)
    calls = []

    def native(args, timeout):
        calls.append((list(args), timeout))
        assert provider._vault.resolve() == vault.resolve()
        if args == ["--version"]:
            return 0, "0.0.0-contract-fixture", ""
        if args[0] == "query":
            return 0, "facts/offline.md:1: Contract-only recall fixture", ""
        assert args[0] == "index", args
        return 0, "", ""

    monkeypatch.setattr(provider, "_run_zg", native)
    manager = MemoryManager()
    manager.add_provider(provider)
    try:
        manager.initialize_all("initial-session", agent_context="primary")
        assert provider._engine_state()["zg_version"] == "0.0.0-contract-fixture"
        assert provider._vault.resolve() == vault.resolve()
        assert (vault / "facts").is_dir()
        yield SimpleNamespace(manager=manager, provider=provider, vault=vault, calls=calls)
    finally:
        assert manager.flush_pending(timeout=5)
        manager.shutdown_all()


def test_manager_routes_json_tools_and_keeps_prompt_static(managed):
    manager = managed.manager
    assert manager.get_all_tool_names() == {"memory_search", "memory_store"}
    assert {s["name"] for s in manager.get_all_tool_schemas()} == manager.get_all_tool_names()
    before = manager.build_system_prompt()
    assert before
    stored = json.loads(manager.handle_tool_call(
        "memory_store", {"content": "Contract fact stored through real manager"}))
    assert stored["status"] == "stored"
    path = Path(stored["path"])
    assert path.resolve().is_relative_to(managed.vault / "facts")
    assert "Contract fact stored through real manager" in path.read_text()
    # Store acknowledges durable source/admission, not completed indexing.
    assert managed.provider._index_worker.drain(5)
    assert managed.provider._recall_token() is not None
    recalled = json.loads(manager.handle_tool_call("memory_search", {"query": "contract fact"}))
    assert "results" in recalled, recalled
    assert "Contract-only recall fixture" in recalled["results"]
    assert any(args[0] == "query" for args, _ in managed.calls)
    assert manager.build_system_prompt() == before
    assert not manager.supports_pre_compress_checkpoint()


def test_manager_sync_keeps_explicit_turn_session(managed):
    manager = managed.manager
    manager.on_session_switch("different-active-session")
    manager.sync_all("Originating user text", "Originating assistant reply",
                     session_id="originating-turn-session")
    assert manager.flush_pending(timeout=5)
    # Host flush drains its executor, not the provider's nested disk queue.
    # Real shutdown is the public durability boundary; never resume afterwards.
    manager.shutdown_all()
    text = "\n".join(p.read_text() for p in (managed.vault / "sessions").glob("*.md"))
    assert "Originating user text" in text
    assert "Originating assistant reply" in text
    assert "session originating-turn-session" in text
    assert "session different-active-session" not in text


def test_manager_prefetch_returns_unwrapped_reference(managed):
    recalled = managed.manager.prefetch_all("What was the contract decision?",
                                             session_id="recall-session")
    assert "Contract-only recall fixture" in recalled
    assert "<memory-context>" not in recalled
    assert any(args[0] == "query" for args, _ in managed.calls)


@pytest.mark.parametrize("disabled", [False, True])
def test_host_toolset_gate_controls_advertisement(managed, disabled):
    from agent.memory_manager import inject_memory_provider_tools, memory_provider_tools_enabled

    denied = ["memory"] if disabled else []
    agent = SimpleNamespace(_memory_manager=managed.manager, tools=[],
                            enabled_toolsets=["memory"], disabled_toolsets=denied,
                            valid_tool_names=set())
    assert memory_provider_tools_enabled(["memory"], denied) is (not disabled)
    assert inject_memory_provider_tools(agent) == (0 if disabled else 2)
    names = {tool["function"]["name"] for tool in agent.tools}
    assert names == (set() if disabled else {"memory_search", "memory_store"})
