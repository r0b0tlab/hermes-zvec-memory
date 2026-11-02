"""Offline host PM and legacy-config contract; no engine or package install."""
import argparse
import builtins
import importlib.util
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tomllib

import pytest
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

from source_support import PROVIDER_ROOT

HOST = Path(os.environ["HERMES_AGENT_DIR"]).resolve()
sys.path.insert(0, str(HOST))


@pytest.fixture
def real_host(isolated_home, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("HERMES_ENABLE_PROJECT_PLUGINS", raising=False)
    plugin = isolated_home / "plugins" / "zvec-memory"
    shutil.copytree(PROVIDER_ROOT / "zvec-memory", plugin,
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    # Host import performs its own local version discovery (git rev-parse).
    # Install tripwires after host initialization, before the plugin paths.
    import plugins.memory as discovery
    from pm import plugin_declarations, workspace
    attempts = []

    def forbidden(*args, **kwargs):
        attempts.append((args, kwargs))
        raise AssertionError("unexpected subprocess or network")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    assert Path(discovery.__file__).resolve().is_relative_to(HOST)
    assert Path(plugin_declarations.__file__).resolve().is_relative_to(HOST)
    yield isolated_home, plugin, discovery, plugin_declarations, workspace
    assert not attempts


def test_host_pm_declares_and_materializes_index_requirement(real_host, tmp_path):
    home, plugin, discovery, pm, workspace = real_host
    declaration = pm.read_python_declaration(plugin)
    assert declaration.files == (plugin / "plugin.yaml",)
    assert declaration.pyproject is None and not declaration.external
    assert declaration.requirements == declaration.install_requirements == ("PyYAML>=6,<7",)
    assert declaration.installs_packages
    requirement = Requirement(declaration.install_requirements[0])
    assert canonicalize_name(requirement.name) == "pyyaml"
    assert not requirement.url and not requirement.marker
    assert "6.0.3" in requirement.specifier and "5.4" not in requirement.specifier
    assert "7.0" not in requirement.specifier
    (home / "config.yaml").write_text("memory:\n  provider: zvec-memory\n")
    assert plugin in workspace.enabled_member_dirs()
    member = workspace._workspace_member(plugin, tmp_path / "pm-snapshot", identity=plugin)
    generated = tomllib.loads((member / "pyproject.toml").read_text())
    assert generated["project"]["dependencies"] == ["PyYAML>=6,<7"]
    assert generated["tool"]["uv"]["package"] is False
    assert discovery.find_provider_dir("zvec-memory") == plugin
    assert "zvec-memory" in discovery.list_memory_provider_names()


def test_legacy_yaml_real_loader_and_lifecycle(real_host, monkeypatch):
    home, plugin, discovery, _, _ = real_host
    config = home / "config.yaml"
    config.write_text('plugins:\n  zvec-memory:\n    vault: legacy-vault\n    auto_extract: false\n    reindex_min_seconds: 3600\n', encoding="utf-8")
    before = config.read_bytes()
    provider = discovery.load_memory_provider("zvec-memory", register_skills=False)
    assert provider is not None and provider.name == "zvec-memory"
    assert provider._config["vault"] == "legacy-vault"
    assert provider._config["auto_extract"] is False
    index = home / "legacy-vault" / ".zvec-grep"
    index.mkdir(parents=True)
    (index / "manifest.json").write_text("{}")
    monkeypatch.setattr(provider, "_run_zg", lambda *a, **kw: (0, "", ""))
    from agent.memory_manager import MemoryManager
    manager = MemoryManager()
    manager.add_provider(provider)
    try:
        manager.initialize_all("synthetic-session", hermes_home=str(home))
        assert provider._vault == home / "legacy-vault"
        assert (provider._vault / "facts").is_dir()
        assert (provider._vault / "sessions").is_dir()
        assert manager.flush_pending(timeout=5)
    finally:
        manager.shutdown_all()
    assert not provider._disk_worker._thread.is_alive()
    assert not provider._index_worker._thread.is_alive()
    assert config.read_bytes() == before


def _cli(plugin):
    spec = importlib.util.spec_from_file_location("dependency_test_cli", plugin / "cli.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("command", ["doctor", "status", "reindex"])
@pytest.mark.parametrize("as_json", [False, True])
def test_malformed_legacy_yaml_is_redacted_and_reindex_refused(real_host, capsys, command, as_json):
    home, plugin, _, _, _ = real_host
    canary = "synthetic-yaml-secret-49a"
    config = home / "config.yaml"
    config.write_text(f"plugins: [ {canary}\n", encoding="utf-8")
    before = config.read_bytes()
    cli = _cli(plugin)
    parser = argparse.ArgumentParser()
    cli.register_cli(parser)
    args = parser.parse_args([command] + (["--json"] if as_json else []))
    rc = args.func(args)
    captured = capsys.readouterr()
    assert canary not in captured.out + captured.err
    assert rc == (0 if command == "status" else 1)
    if as_json:
        result = json.loads(captured.out)
        assert not result["ok"]
        if command == "reindex":
            assert result["action"]["status"] == "failed"
    assert not (home / "zvec-memory" / ".reindex-request.json").exists()
    assert config.read_bytes() == before


def test_missing_yaml_fails_closed_not_alternate_parser(real_host, monkeypatch, capsys):
    home, plugin, discovery, _, _ = real_host
    (home / "config.yaml").write_text("plugins:\n  zvec-memory:\n    vault: legacy-vault\n")
    cli = _cli(plugin)
    imported = builtins.__import__
    attempts = []

    def deny_yaml(name, *args, **kwargs):
        if name == "yaml":
            attempts.append(name)
            raise ModuleNotFoundError("synthetic missing yaml secret", name="yaml")
        if name == "hermes_yaml":
            attempts.append(name)
            raise AssertionError("alternate parser attempted")
        return imported(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", deny_yaml)
    assert discovery.load_memory_provider("zvec-memory", register_skills=False) is None
    parser = argparse.ArgumentParser()
    cli.register_cli(parser)
    args = parser.parse_args(["reindex", "--json"])
    assert args.func(args) == 1
    output = capsys.readouterr()
    assert "synthetic missing yaml secret" not in output.out + output.err
    assert json.loads(output.out)["action"]["status"] == "failed"
    assert attempts and set(attempts) == {"yaml"}
    assert not (home / "legacy-vault").exists()
    assert not (home / "zvec-memory").exists()


def test_native_json_does_not_import_yaml(real_host, monkeypatch):
    home, _, discovery, _, _ = real_host
    path = home / "zvec-memory" / "config.json"
    path.parent.mkdir()
    path.write_text(json.dumps({"vault": "native-vault"}))
    imported = builtins.__import__
    def deny_yaml(name, *args, **kwargs):
        if name == "yaml":
            raise AssertionError("native JSON must not import PyYAML")
        return imported(name, *args, **kwargs)
    monkeypatch.setattr(builtins, "__import__", deny_yaml)
    provider = discovery.load_memory_provider("zvec-memory", register_skills=False)
    assert provider is not None and provider._config["vault"] == "native-vault"
