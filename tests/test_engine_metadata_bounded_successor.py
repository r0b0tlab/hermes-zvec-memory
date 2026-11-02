"""Bounded, nonblocking package metadata admission in a synthetic runtime."""
import json
import os
import socket
import subprocess
import sys
from pathlib import Path

import pytest

from test_engine import config_for, fake_runtime, fixture_node, load_engine
from test_fresh_install_refusal_successor import prohibit_external_work, snapshot


@pytest.mark.parametrize("entrypoint", ["direct", "cli"])
def test_fifo_refuses_with_timeout_and_no_side_effects(tmp_path, isolated_home, entrypoint):
    engine = load_engine()
    fake_runtime(tmp_path)
    config = config_for(tmp_path)
    path = engine.package_json_path(isolated_home, config)
    path.unlink()
    os.mkfifo(path)
    before = snapshot(tmp_path)
    script = r'''
import argparse, json, socket, subprocess, sys
from pathlib import Path
from test_engine import load_engine
from test_cli import load_cli
engine = load_engine()
def forbidden(*args, **kwargs):
    raise AssertionError("unexpected subprocess")
subprocess.run = subprocess.Popen = forbidden
socket.create_connection = socket.socket.connect = forbidden
home, config = Path(sys.argv[2]), json.loads(sys.argv[3])
if sys.argv[1] == "direct":
    result = engine.install_engine(home, config)
    assert result["status"] == "invalid-engine-metadata", result
    print(json.dumps(result))
else:
    cli = load_cli()
    cli._configured = lambda: (None, config)
    cli._hermes_home = lambda: home
    cli._engine_module = lambda: engine
    parser = argparse.ArgumentParser()
    cli.register_cli(parser)
    assert cli.zvec_memory_command(parser.parse_args(["engine", "install", "--json"])) == 1
'''
    env = dict(os.environ)
    # Keep the explicitly isolated interpreter's dependency paths for child
    # imports; Python 3.14 may obtain pytest from a separately pinned site dir.
    env["PYTHONPATH"] = os.pathsep.join(filter(None, (str(Path(__file__).parent),
                                                      os.environ.get("PYTHONPATH"))))
    proc = subprocess.run([sys.executable, "-B", "-c", script, entrypoint,
                           str(isolated_home), json.dumps(config)],
                          env=env, capture_output=True, text=True, timeout=5)
    assert proc.returncode == 0, proc.stderr
    result = json.loads(proc.stdout)
    assert result["status"] == "invalid-engine-metadata"
    if entrypoint == "cli":
        assert result["action"]["status"] == "failed"
        assert result["health"]["status"] == "not_checked"
    assert snapshot(tmp_path) == before


@pytest.mark.parametrize("shape", ["directory", "socket", "symlink", "oversize", "malformed", "invalid-utf8", "deep"])
def test_invalid_metadata_refuses_before_external_work(tmp_path, isolated_home, monkeypatch, shape):
    engine = load_engine()
    fake_runtime(tmp_path)
    config = config_for(tmp_path)
    path = engine.package_json_path(isolated_home, config)
    path.unlink()
    sock = None
    if shape == "directory":
        path.mkdir()
    elif shape == "socket":
        sock = socket.socket(socket.AF_UNIX)
        monkeypatch.chdir(path.parent)
        sock.bind(path.name)
    elif shape == "symlink":
        target = tmp_path / "valid.json"
        target.write_text(json.dumps({"name": engine.ENGINE_PACKAGE, "version": engine.PINNED_VERSION}))
        path.symlink_to(target)
    elif shape == "oversize":
        path.write_bytes(b" " * (engine.MAX_PACKAGE_JSON_BYTES + 1))
    elif shape == "invalid-utf8":
        path.write_bytes(b"\xff")
    elif shape == "deep":
        path.write_bytes(b"[" * 2000 + b"]" * 2000)
    else:
        path.write_bytes(b'{"name":')
    try:
        prohibit_external_work(monkeypatch, engine)
        if shape in {"directory", "socket", "symlink", "oversize"}:
            monkeypatch.setattr(engine.os, "open", lambda *a, **k: pytest.fail("invalid leaf must not be opened"))
        before = snapshot(tmp_path)
        assert engine.installed_version(isolated_home, config) is None
        assert engine.install_engine(isolated_home, config)["status"] == "invalid-engine-metadata"
        assert engine.ensure_engine(isolated_home, config)["status"] == "invalid-engine-metadata"
        assert snapshot(tmp_path) == before
    finally:
        if sock:
            sock.close()


def test_character_device_rejected(tmp_path, isolated_home, monkeypatch):
    engine = load_engine()
    monkeypatch.setattr(engine, "package_json_path", lambda *a: Path("/dev/null"))
    monkeypatch.setattr(engine.os, "open", lambda *a, **k: pytest.fail("device must not be opened"))
    assert engine.installed_version(isolated_home, {}) is None


def test_replacement_after_open_is_rejected(tmp_path, isolated_home, monkeypatch):
    engine = load_engine()
    fake_runtime(tmp_path)
    config = config_for(tmp_path)
    path = engine.package_json_path(isolated_home, config)
    replacement = path.with_name("replacement")
    replacement.write_bytes(path.read_bytes())
    real_open = os.open
    def replacing_open(*args, **kwargs):
        fd = real_open(*args, **kwargs)
        os.replace(replacement, path)
        return fd
    monkeypatch.setattr(engine.os, "open", replacing_open)
    assert engine.installed_version(isolated_home, config) is None


@pytest.mark.parametrize("replacement", ["fifo", "symlink", "regular"])
def test_leaf_replaced_between_lstat_and_open(tmp_path, isolated_home, monkeypatch, replacement):
    engine = load_engine()
    fake_runtime(tmp_path)
    config = config_for(tmp_path)
    path = engine.package_json_path(isolated_home, config)
    target = path.with_name("target")
    target.write_bytes(path.read_bytes())
    real_open = os.open
    def replacing_open(*args, **kwargs):
        path.unlink()
        if replacement == "fifo":
            os.mkfifo(path)
        elif replacement == "symlink":
            path.symlink_to(target)
        else:
            path.write_bytes(target.read_bytes())
        return real_open(*args, **kwargs)
    monkeypatch.setattr(engine.os, "open", replacing_open)
    assert engine.installed_version(isolated_home, config) is None


@pytest.mark.parametrize("fault", ["short", "drift"])
def test_short_read_and_metadata_drift_rejected(tmp_path, isolated_home, monkeypatch, fault):
    engine = load_engine()
    fake_runtime(tmp_path)
    config = config_for(tmp_path)
    path = engine.package_json_path(isolated_home, config)
    real_read = os.read
    def faulty_read(fd, size):
        if fault == "short":
            return b""
        data = real_read(fd, size)
        if data:
            path.write_bytes(data + b" ")
        return data
    monkeypatch.setattr(engine.os, "read", faulty_read)
    assert engine.installed_version(isolated_home, config) is None


def test_limit_and_partial_reads(tmp_path, isolated_home, monkeypatch):
    engine = load_engine()
    fake_runtime(tmp_path)
    config = config_for(tmp_path)
    path = engine.package_json_path(isolated_home, config)
    raw = path.read_bytes()
    path.write_bytes(raw + b" " * (engine.MAX_PACKAGE_JSON_BYTES - len(raw)))
    real_read = os.read
    requests = []
    def partial_read(fd, size):
        requests.append(size)
        return real_read(fd, min(size, 8192))
    monkeypatch.setattr(engine.os, "read", partial_read)
    assert engine.installed_version(isolated_home, config) == engine.PINNED_VERSION
    assert max(requests) == engine.MAX_PACKAGE_JSON_BYTES + 1
    path.write_bytes(raw + b" " * (engine.MAX_PACKAGE_JSON_BYTES + 1 - len(raw)))
    requests.clear()
    assert engine.installed_version(isolated_home, config) is None
    assert not requests


def test_normal_runtime_reuse_positive(tmp_path, isolated_home, monkeypatch):
    engine = load_engine()
    fake_runtime(tmp_path)
    node = fixture_node(tmp_path / "fixture-node")
    config = config_for(tmp_path, node_bin=str(node))
    monkeypatch.setattr(engine.shutil, "which", lambda *a: pytest.fail("unexpected executable lookup"))
    assert engine.installed_version(isolated_home, config) == engine.PINNED_VERSION
    assert engine.install_engine(isolated_home, config)["status"] == "updated"
    assert engine.install_engine(isolated_home, config)["status"] == "present"
