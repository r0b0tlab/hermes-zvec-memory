"""Successor contract: refuse fresh fetches, preserve existing-runtime semantics."""
import os
from pathlib import Path
import socket
import subprocess

import pytest

from test_engine import config_for, fake_runtime, fixture_node, load_engine


def snapshot(root):
    """Record small fixture trees without following symlinks."""
    out = {}
    for base, dirs, files in os.walk(root, followlinks=False):
        for name in [".", *dirs, *files]:
            path = Path(base) / name
            st = path.lstat()
            rel = str(path.relative_to(root))
            if path.is_symlink():
                payload = ("link", os.readlink(path))
            elif path.is_file():
                payload = ("file", path.read_bytes())
            else:
                payload = ("directory",)
            out[rel] = (st.st_mode, st.st_mtime_ns, payload)
    return out


def prohibit_network(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("network access forbidden")
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(socket.socket, "connect", forbidden)


def prohibit_external_work(monkeypatch, engine):
    def forbidden(*args, **kwargs):
        pytest.fail("refusal must not resolve executables, launch processes or access network")
    monkeypatch.setattr(engine.shutil, "which", forbidden)
    monkeypatch.setattr(engine.subprocess, "run", forbidden)
    monkeypatch.setattr(engine.subprocess, "Popen", forbidden)
    prohibit_network(monkeypatch)


@pytest.mark.parametrize("shape", ["missing", "empty-prefix", "dangling-package", "file-prefix"])
def test_fresh_refusal_is_side_effect_free(tmp_path, isolated_home, monkeypatch, shape):
    engine = load_engine()
    config = config_for(tmp_path)
    root = Path(config["runtime_dir"])
    package = engine.package_json_path(isolated_home, config).parent
    if shape == "empty-prefix":
        package.parent.mkdir(parents=True)
    elif shape == "dangling-package":
        package.parent.mkdir(parents=True)
        package.symlink_to(tmp_path / "absent")
    elif shape == "file-prefix":
        root.mkdir()
        (root / "runtime").write_text("do not replace")
    prohibit_external_work(monkeypatch, engine)
    before = snapshot(tmp_path)
    result = engine.install_engine(isolated_home, config, npm="/not/executed/npm")
    assert result["status"] == ("invalid-engine-metadata" if shape == "dangling-package" else "fresh-install-unavailable")
    assert snapshot(tmp_path) == before


def test_fresh_refusal_preserves_existing_managed_artifacts(tmp_path, isolated_home, monkeypatch):
    engine = load_engine()
    config = config_for(tmp_path)
    sentinels = [engine.launcher_path(isolated_home, config), engine.unit_path(),
                 engine.runtime_root(isolated_home, config) / engine.MANIFEST_NAME,
                 engine.provider_config_path(isolated_home), isolated_home / "config.yaml"]
    for path in sentinels:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("operator-owned sentinel\n")
    prohibit_external_work(monkeypatch, engine)
    before = snapshot(tmp_path)
    result = engine.install_engine(isolated_home, config)
    assert result["status"] == "fresh-install-unavailable"
    assert snapshot(tmp_path) == before


@pytest.mark.parametrize("change, error", [
    ({"embedding": "remote/model"}, "embedding"),
    ({"runtime_dir": "bad%path"}, "systemd"),
    ({"engine_home": "bad'path"}, "systemd"),
    ({"zg_bin": "/not/the/managed/launcher"}, "Custom zg_bin"),
])
def test_invalid_policy_and_paths_refuse_before_side_effects(tmp_path, isolated_home, monkeypatch, change, error):
    engine = load_engine()
    config = config_for(tmp_path, **change)
    prohibit_external_work(monkeypatch, engine)
    before = snapshot(tmp_path)
    with pytest.raises((ValueError, RuntimeError), match=error):
        engine.install_engine(isolated_home, config)
    assert snapshot(tmp_path) == before


@pytest.mark.parametrize("version", ["0.2.3", "latest", " 0.2.2", None, True])
def test_version_policy_refuses_before_side_effects(tmp_path, isolated_home, monkeypatch, version):
    engine = load_engine()
    prohibit_external_work(monkeypatch, engine)
    before = snapshot(tmp_path)
    with pytest.raises(ValueError):
        engine.install_engine(isolated_home, config_for(tmp_path), version=version)
    assert snapshot(tmp_path) == before


def test_nondefault_profile_refuses_before_side_effects(tmp_path, monkeypatch):
    engine = load_engine()
    prohibit_external_work(monkeypatch, engine)
    before = snapshot(tmp_path)
    with pytest.raises(ValueError, match="default"):
        engine.install_engine(tmp_path / "other-profile", config_for(tmp_path))
    assert snapshot(tmp_path) == before


@pytest.mark.parametrize("condition, expected", [("invalid", "invalid-engine-metadata"),
                                                    ("mismatch", "migration-required"),
                                                    ("missing-entry", "missing-engine")])
def test_existing_incompatible_package_is_preserved(tmp_path, isolated_home, monkeypatch, condition, expected):
    engine = load_engine()
    fake_runtime(tmp_path, version="0.2.1" if condition == "mismatch" else "0.2.2",
                 entry=condition != "missing-entry")
    config = config_for(tmp_path)
    if condition == "invalid":
        engine.package_json_path(isolated_home, config).write_text("bad-json")
    prohibit_external_work(monkeypatch, engine)
    before = snapshot(tmp_path)
    assert engine.install_engine(isolated_home, config)["status"] == expected
    assert snapshot(tmp_path) == before


def test_compatible_existing_fixture_reuses_without_npm(tmp_path, isolated_home, monkeypatch):
    engine = load_engine()
    fake_runtime(tmp_path)
    node = fixture_node(tmp_path / "fixture-node")
    config = config_for(tmp_path, node_bin=str(node))
    monkeypatch.setattr(engine.shutil, "which", lambda *a, **k: pytest.fail("no npm lookup"))
    prohibit_network(monkeypatch)
    real_run = subprocess.run
    seen = []
    def versions_only(argv, **kwargs):
        assert argv in ([str(node), "--version"],
                        [str(node), str(engine.entry_path(isolated_home, config)), "--version"])
        seen.append(argv)
        return real_run(argv, **kwargs)
    monkeypatch.setattr(engine.subprocess, "run", versions_only)
    first = engine.install_engine(isolated_home, config)
    assert first["status"] == "updated"
    assert first["version"] == engine.PINNED_VERSION
    assert engine.launcher_path(isolated_home, config).is_file()
    assert engine.unit_path().is_file()
    before = snapshot(tmp_path)
    second = engine.install_engine(isolated_home, config)
    assert second["status"] == "present" and second["writes"] == []
    assert snapshot(tmp_path) == before
    assert seen == [[str(node), "--version"],
                    [str(node), str(engine.entry_path(isolated_home, config)), "--version"]] * 2


def test_existing_executable_version_mismatch_cannot_generate_artifacts(tmp_path, isolated_home, monkeypatch):
    engine = load_engine()
    fake_runtime(tmp_path)
    node = fixture_node(tmp_path / "fixture-node", native_version="0.2.1")
    config = config_for(tmp_path, node_bin=str(node))
    monkeypatch.setattr(engine.shutil, "which", lambda *a, **k: pytest.fail("no npm lookup"))
    before = snapshot(tmp_path)
    result = engine.install_engine(isolated_home, config)
    assert result["status"] == "executable-version-mismatch"
    assert snapshot(tmp_path) == before


@pytest.mark.parametrize("version, expected", [(None, "fresh-install-unavailable"),
                                                ("", "failed"), ("0.2.3", "failed")])
def test_cli_dispatcher_preserves_version_and_refuses_without_side_effects(
        tmp_path, isolated_home, monkeypatch, capsys, version, expected):
    import argparse
    import json
    from test_cli import load_cli

    cli = load_cli()
    engine = load_engine()
    config = config_for(tmp_path)
    monkeypatch.setattr(cli, "_configured", lambda: (None, config))
    monkeypatch.setattr(cli, "_hermes_home", lambda: isolated_home)
    monkeypatch.setattr(cli, "_engine_module", lambda: engine)
    parser = argparse.ArgumentParser()
    cli.register_cli(parser)
    argv = ["engine", "install", "--json"]
    if version is not None:
        argv += ["--version", version]
    args = parser.parse_args(argv)
    prohibit_external_work(monkeypatch, engine)
    before = snapshot(tmp_path)
    assert cli.zvec_memory_command(args) == 1
    result = json.loads(capsys.readouterr().out)
    assert result["status"] == expected
    assert result["action"]["status"] == "failed"
    assert result["health"]["status"] == "not_checked"
    assert snapshot(tmp_path) == before
