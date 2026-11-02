"""Real dispatcher controls for untrusted settings and built-in file admission."""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from source_support import PROVIDER_ROOT
from test_cli import healthy_vault


CANARY = "secret-builtin-8f71\x1b[31m"
CHILD = r'''
import argparse, importlib.util, os, sys
from pathlib import Path
sys.path.insert(0, os.environ["HERMES_AGENT_DIR"])
spec = importlib.util.spec_from_file_location("cli_fault_child", sys.argv[1])
cli = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = cli
spec.loader.exec_module(cli)
command, fmt, fault = sys.argv[2:5]
leaf = Path(os.environ["HERMES_HOME"]) / "memories" / os.environ["TEST_BUILTIN_LEAF"]
if fault == "stat_error":
    original = Path.lstat
    def failing(path, *args, **kwargs):
        if path == leaf:
            raise PermissionError("secret-builtin-8f71\x1b[31m")
        return original(path, *args, **kwargs)
    Path.lstat = failing
if fault in ("swap_symlink", "swap_fifo"):
    original_open = os.open
    def swapping(path, flags, *args, **kwargs):
        if Path(path) == leaf:
            leaf.rename(leaf.parent / "original")
            if fault == "swap_symlink":
                leaf.symlink_to(leaf.parent / "secret")
            else:
                os.mkfifo(leaf)
        return original_open(path, flags, *args, **kwargs)
    os.open = swapping
cli._default_runner = lambda argv, timeout=15: (0, "0.2.2" if "--version" in argv else "ready", "")
p = argparse.ArgumentParser()
cli.register_cli(p)
a = p.parse_args([command] + (["--json"] if fmt == "json" else []))
sys.exit(a.func(a))
'''


def run_child(home, command, as_json, fault="none", leaf="MEMORY.md"):
    env = os.environ.copy()
    env["HERMES_HOME"] = str(home)
    env["TEST_BUILTIN_LEAF"] = leaf
    result = subprocess.run(
        [sys.executable, "-B", "-c", CHILD, str(PROVIDER_ROOT / "zvec-memory/cli.py"),
         command, "json" if as_json else "human", fault],
        env=env, capture_output=True, text=True, timeout=8,
    )
    assert result.stderr == ""
    assert "secret-builtin-8f71" not in result.stdout
    assert "\x1b" not in result.stdout and "\\u001b" not in result.stdout
    return result.returncode, json.loads(result.stdout) if as_json else result.stdout


def fixture(tmp_path):
    home = tmp_path / "profile"
    (home / "zvec-memory").mkdir(parents=True)
    (home / "memories").mkdir()
    vault = healthy_vault(tmp_path, binary="/fixture-engine")
    config = home / "zvec-memory/config.json"
    config.write_text(json.dumps({"vault": str(vault), "zg_bin": "/fixture-engine"}))
    return home, vault, config


@pytest.mark.parametrize("command", ["doctor", "status", "reindex"])
@pytest.mark.parametrize("as_json", [False, True])
def test_config_error_string_is_redacted_with_known_target(tmp_path, command, as_json):
    home, vault, config = fixture(tmp_path)
    config.write_text(json.dumps({"vault": str(vault), "zg_bin": "/fixture-engine",
                                  "_config_error": CANARY}))
    before = config.read_bytes()
    rc, result = run_child(home, command, as_json)
    assert rc == (1 if command == "doctor" else 0)
    if as_json:
        assert result["checked"] is True and result["ok"] is False
        assert next(c for c in result["checks"] if c["name"] == "config") == {
            "name": "config", "ok": False, "detail": "configuration unavailable or invalid"}
        assert result["action"]["status"] == ("requested" if command == "reindex" else "checked")
    else:
        assert "FAIL config" in result and "configuration unavailable or invalid" in result
    assert (vault / ".reindex-request.json").exists() is (command == "reindex")
    assert config.read_bytes() == before


@pytest.mark.parametrize("command", ["doctor", "status", "reindex"])
@pytest.mark.parametrize("as_json", [False, True])
@pytest.mark.parametrize("filename", ["MEMORY.md", "USER.md"])
@pytest.mark.parametrize("kind", ["symlink", "dangling", "directory", "fifo", "ancestor_denied",
                                  "stat_error", "invalid_utf8", "swap_symlink", "swap_fifo",
                                  "memories_symlink"])
def test_builtin_admission_fails_closed_without_hanging(tmp_path, command, as_json, filename, kind):
    home, vault, _ = fixture(tmp_path)
    memories = home / "memories"
    leaf = memories / filename
    secret = memories / "secret"
    secret.write_text(CANARY)
    before = secret.read_bytes()
    fault = kind if kind in ("stat_error", "swap_symlink", "swap_fifo") else "none"
    if kind == "symlink":
        leaf.symlink_to(secret)
    elif kind == "dangling":
        leaf.symlink_to(memories / "missing")
    elif kind == "directory":
        leaf.mkdir()
    elif kind == "fifo":
        os.mkfifo(leaf)
    elif kind == "memories_symlink":
        memories.rename(home / "actual-memories")
        memories.symlink_to(home / "actual-memories", target_is_directory=True)
    elif kind == "invalid_utf8":
        leaf.write_bytes(b"secret-builtin-8f71\xff")
    elif kind != "ancestor_denied":
        leaf.write_text("something worth recalling")
    if kind == "ancestor_denied":
        memories.chmod(0)
        try:
            with pytest.raises(PermissionError):
                leaf.lstat()
            rc, result = run_child(home, command, as_json, fault, filename)
        finally:
            memories.chmod(0o700)
    else:
        rc, result = run_child(home, command, as_json, fault, filename)
    assert rc == (1 if command == "doctor" else 0)
    if as_json:
        assert result["checked"] is True and result["ok"] is False
        assert next(c for c in result["checks"] if c["name"] == "builtin_mirror") == {
            "name": "builtin_mirror", "ok": False, "detail": "unreadable built-in memory"}
        assert "builtin_mirror" in result["failures"]
        assert result["action"]["status"] == ("requested" if command == "reindex" else "checked")
    else:
        assert "FAIL builtin_mirror" in result and "unreadable built-in memory" in result
    assert (vault / ".reindex-request.json").exists() is (command == "reindex")
    assert secret.read_bytes() == before


@pytest.mark.parametrize("command", ["doctor", "status", "reindex"])
@pytest.mark.parametrize("as_json", [False, True])
@pytest.mark.parametrize("kind", ["missing_dir", "missing_files", "empty", "mirrored", "gap"])
def test_optional_builtin_success_controls(tmp_path, command, as_json, kind):
    home, vault, _ = fixture(tmp_path)
    memories = home / "memories"
    if kind == "missing_dir":
        memories.rmdir()
    elif kind in ("empty", "mirrored", "gap"):
        value = {"empty": "", "mirrored": "something worth recalling", "gap": "unmirrored"}[kind]
        (memories / "MEMORY.md").write_text(value)
        (memories / "USER.md").write_text("")
    rc, result = run_child(home, command, as_json)
    healthy = kind != "gap"
    assert rc == (1 if command == "doctor" and not healthy else 0)
    if as_json:
        assert result["checked"] is True and result["ok"] is healthy
        assert next(c for c in result["checks"] if c["name"] == "builtin_mirror") == {
            "name": "builtin_mirror", "ok": healthy,
            "detail": "unmirrored=" + ("0" if healthy else "1")}
    else:
        assert ("FAIL builtin_mirror" in result) is not healthy
        assert ("healthy" if healthy else "unhealthy") in result
    assert (vault / ".reindex-request.json").exists() is (command == "reindex")


@pytest.mark.parametrize("as_json", [False, True])
def test_unreadable_configuration_never_admits_unknown_reindex_target(tmp_path, as_json):
    home, vault, config = fixture(tmp_path)
    config.write_bytes(b"invalid json secret-builtin-8f71\x1b[31m")
    os.mkfifo(home / "memories/MEMORY.md")
    rc, result = run_child(home, "reindex", as_json)
    assert rc == 1
    if as_json:
        assert result["action"]["status"] == "failed"
        assert result["action"]["completed"] is False and result["checked"] is False
    else:
        assert "Could not select the vault" in result
    assert not (vault / ".reindex-request.json").exists()
    assert not (home / "zvec-memory/.reindex-request.json").exists()
