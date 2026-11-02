"""Policy successors for byte-bound engine tests; historical failures remain visible."""
import json
import os
import shlex
import subprocess
from pathlib import Path

import pytest

from test_engine import RESOLVE_EXECUTABLE, config_for, fake_runtime, fixture_node, load_engine, unit_dir
from test_fresh_install_refusal_successor import prohibit_external_work, prohibit_network, snapshot


def test_unit_template_declares_the_task_ceiling_and_the_listen_address_successor(tmp_path):
    engine = load_engine()
    config = config_for(tmp_path, server_url="http://127.0.0.1:18099/mcp")
    unit = engine.unit_template(tmp_path / "hermes-home", config)
    assert f"TasksMax={engine.TASKS_MAX}\n" in unit
    assert "MemoryMax=4294967296\n" in unit
    assert f'ExecStart="{tmp_path}/runtime_root/zg-default" server run --listen "127.0.0.1:18099"' in unit
    assert f'--token-file "{tmp_path}/engine-home/server.token"' in unit
    assert "WantedBy=default.target\n" in unit


def test_fresh_install_refuses_before_npm_and_registry_successor(tmp_path, isolated_home, monkeypatch):
    engine = load_engine()
    config = config_for(tmp_path, node_bin="/not/executed/node")
    prohibit_external_work(monkeypatch, engine)
    before = snapshot(tmp_path)
    result = engine.install_engine(isolated_home, config, npm="/not/executed/npm")
    assert result["status"] == "fresh-install-unavailable"
    assert "transitive" in result["detail"] and "separately trusted" in result["detail"]
    assert snapshot(tmp_path) == before


@pytest.mark.parametrize("selection", ["path", "explicit"])
def test_existing_node_path_selected_for_readiness_and_launcher_successor(tmp_path, unit_dir, isolated_home, monkeypatch, selection):
    engine = load_engine()
    fake_runtime(tmp_path)
    selected = fixture_node(tmp_path / "node path with spaces ' literal")
    manager = tmp_path / "manager/bin"
    manager.mkdir(parents=True)
    (manager / "node").symlink_to(selected)
    monkeypatch.setattr(engine.shutil, "which", RESOLVE_EXECUTABLE)
    monkeypatch.setenv("PATH", f"{manager}:/usr/bin:/bin")
    config = config_for(tmp_path)
    if selection == "explicit":
        config["node_bin"] = str(manager / "node")
    original_config = dict(config)
    real_run = subprocess.run
    seen = []

    def versions_only(argv, **kwargs):
        assert argv in ([str(selected), "--version"],
                        [str(selected), str(engine.entry_path(isolated_home, config)), "--version"])
        assert kwargs["stdin"] == subprocess.DEVNULL and kwargs["timeout"] == 10
        seen.append(argv)
        return real_run(argv, **kwargs)

    monkeypatch.setattr(subprocess, "run", versions_only)
    prohibit_network(monkeypatch)
    result = engine.install_engine(isolated_home, config)
    assert result["status"] == "updated" and config == original_config
    assert seen == [[str(selected), "--version"],
                    [str(selected), str(engine.entry_path(isolated_home, config)), "--version"]]
    manifest = json.loads((tmp_path / "runtime_root/manifest.json").read_text())
    assert manifest["node"] == str(selected) and manifest["version"] == "0.2.2"
    launcher = Path(result["zg_bin"])
    assert f'exec {shlex.quote(str(selected))} ' in launcher.read_text()
    decoy = tmp_path / "decoy"
    decoy.mkdir()
    (decoy / "node").write_text("#!/bin/sh\nexit 98\n")
    (decoy / "node").chmod(0o700)
    proc = real_run([str(launcher), "--version"], capture_output=True, text=True,
                    env={**os.environ, "PATH": f"{decoy}:/usr/bin:/bin"}, timeout=10)
    assert (proc.returncode, proc.stdout.strip()) == (0, "0.2.2")


@pytest.mark.parametrize("version,rc", [
    ("v21.9.0", 0), ("v20.0.0", 0), ("22.0.0", 0), ("v22.0", 0),
    ("v22.0.0 extra", 0), ("", 0), ("v22.0.0", 1),
])
def test_existing_runtime_rejects_bad_node_version_successor(tmp_path, isolated_home, monkeypatch, version, rc):
    engine = load_engine()
    fake_runtime(tmp_path)
    node = fixture_node(tmp_path, version=version, rc=rc)
    config = config_for(tmp_path, node_bin=str(node))
    real_run = subprocess.run
    seen = []

    def version_only(argv, **kwargs):
        assert argv == [str(node), "--version"]
        assert kwargs["stdin"] == subprocess.DEVNULL and kwargs["timeout"] == 10
        seen.append(argv)
        return real_run(argv, **kwargs)

    monkeypatch.setattr(subprocess, "run", version_only)
    before = snapshot(tmp_path)
    with pytest.raises(ValueError, match="Node.js >=22"):
        engine.install_engine(isolated_home, config)
    assert seen == [[str(node), "--version"]]
    assert snapshot(tmp_path) == before


@pytest.mark.parametrize("selection", ["missing", "relative", "absent", "directory", "non-executable"])
def test_existing_runtime_rejects_bad_node_selection_successor(tmp_path, isolated_home, monkeypatch, selection):
    engine = load_engine()
    fake_runtime(tmp_path)
    node = tmp_path / "selected-node"
    config = config_for(tmp_path)
    if selection == "relative":
        config["node_bin"] = "relative/node"
    elif selection != "missing":
        config["node_bin"] = str(node)
        if selection == "directory":
            node.mkdir()
        elif selection == "non-executable":
            node.write_text("fixture")
            node.chmod(0o600)
    monkeypatch.setattr(engine.shutil, "which", lambda name: None)
    monkeypatch.setattr(engine.subprocess, "run", lambda *a, **k: pytest.fail("no process"))
    before = snapshot(tmp_path)
    with pytest.raises(ValueError, match="Node|node_bin"):
        engine.install_engine(isolated_home, config)
    assert snapshot(tmp_path) == before


def test_existing_runtime_rejects_invalid_node_format_successor(tmp_path, isolated_home):
    engine = load_engine()
    fake_runtime(tmp_path)
    node = tmp_path / "broken-node"
    node.write_text("#!/no/such/fixture-interpreter\n")
    node.chmod(0o700)
    before = snapshot(tmp_path)
    with pytest.raises(ValueError, match="Node.js >=22 verification failed"):
        engine.install_engine(isolated_home, config_for(tmp_path, node_bin=str(node)))
    assert snapshot(tmp_path) == before


@pytest.mark.parametrize("operation,status", [("ensure_engine", "missing-engine"),
                                               ("install_engine", "fresh-install-unavailable")])
def test_missing_engine_guidance_is_honest_successor(tmp_path, isolated_home, monkeypatch, operation, status):
    engine = load_engine()
    prohibit_external_work(monkeypatch, engine)
    before = snapshot(tmp_path)
    result = getattr(engine, operation)(isolated_home, config_for(tmp_path))
    assert result["status"] == status
    detail = result["detail"].lower()
    assert "fresh" in detail and "installation" in detail
    assert "unavailable" in detail or "disabled" in detail
    assert "separately trusted" in detail
    assert snapshot(tmp_path) == before
