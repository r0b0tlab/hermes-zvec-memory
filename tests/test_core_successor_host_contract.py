# SOURCE-ONLY DRAFT: NOT_EXECUTED. Parent materializes/runs under original pt.sh shared lock/resource caps.
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


def test_discovery_and_schema_are_cold_and_load_real_abc(host):
    threads = set(threading.enumerate())
    modules = set(sys.modules)
    assert host.discovery.find_provider_dir("zvec-memory") == host.plugin
    assert "zvec-memory" in host.discovery.list_memory_provider_names()
    from plugins.memory.config_schema import get_provider_config_schema
    schema = get_provider_config_schema("zvec-memory")
    assert schema is None
    # Schema loading must not import the provider runtime package.
    assert not any(
        getattr(sys.modules[name], "__file__", None) == str(host.plugin / "__init__.py")
        for name in set(sys.modules) - modules
    )
    assert not (host.home / "zvec-memory").exists()
    provider = load(host)
    assert provider.name == "zvec-memory"
    assert isinstance(provider.is_available(), bool)
    assert provider.pre_compress_checkpoint_api_version == 1
    assert set(threading.enumerate()) == threads
    assert not (host.home / "zvec-memory").exists()
    # The host's provider loader provisions <home>/skills in this checkout (its
    # own profile bootstrap, even with register_skills=False); what must stay
    # cold is plugin state and anything written under those braced directories.
    assert not (host.home / "skills").exists() or not any((host.home / "skills").iterdir())














