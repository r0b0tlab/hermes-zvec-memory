"""Dispatcher regressions for untrusted diagnostics and builtin read failures."""
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from test_cli import fake_runner, healthy_vault, load_cli
from test_cli_health_finish import dispatch


CANARY = "private-canary-27d4\x1b[31m"


def setup_target(tmp_path, monkeypatch):
    cli = load_cli()
    home = tmp_path / "profile"
    (home / "zvec-memory").mkdir(parents=True)
    (home / "memories").mkdir()
    vault = healthy_vault(tmp_path, binary="/fixture-engine")
    (home / "zvec-memory/config.json").write_text(json.dumps(
        {"vault": str(vault), "zg_bin": "/fixture-engine"}))
    monkeypatch.setenv("HERMES_HOME", str(home))
    return cli, home, vault


def invoke(cli, command, as_json, capsys, extra=()):
    rc = dispatch(cli, [command] + (["--json"] if as_json else []) + list(extra))
    captured = capsys.readouterr()
    assert captured.err == ""
    assert "private-canary-27d4" not in captured.out
    assert "\x1b" not in captured.out
    return rc, json.loads(captured.out) if as_json else captured.out


def assert_action(cli, vault, command, rc):
    assert rc == (1 if command == "doctor" else 0)
    marker = vault / cli.REINDEX_REQUEST
    assert marker.exists() is (command == "reindex")
    if command == "reindex":
        assert json.loads(marker.read_text())["by"] == "hermes zvec-memory reindex"
        assert marker.stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize("command", ["doctor", "status", "reindex"])
@pytest.mark.parametrize("as_json", [False, True])
@pytest.mark.parametrize("leaf", ["MEMORY.md", "USER.md"])
@pytest.mark.parametrize("failure", ["decode", "read"])
@pytest.mark.parametrize("selection", ["configured", "override"])
def test_builtin_failure_is_unhealthy_and_keeps_selected_target(
        tmp_path, monkeypatch, capsys, command, as_json, leaf, failure, selection):
    cli, home, vault = setup_target(tmp_path, monkeypatch)
    other = tmp_path / "not-selected"
    extra = []
    if selection == "override":
        (home / "zvec-memory/config.json").write_text(json.dumps(
            {"vault": str(other), "zg_bin": "/fixture-engine"}))
        extra = ["--vault", str(vault)]
    path = home / "memories" / leaf
    path.write_bytes(b"private-canary-27d4\xff" if failure == "decode" else b"entry")
    if failure == "read":
        original = os.open
        def fault(name, *args, **kwargs):
            if name == path:
                raise PermissionError(CANARY)
            return original(name, *args, **kwargs)
        monkeypatch.setattr(os, "open", fault)
    monkeypatch.setattr(cli, "_default_runner", fake_runner())
    rc, result = invoke(cli, command, as_json, capsys, extra)
    assert_action(cli, vault, command, rc)
    assert not other.exists()
    if as_json:
        assert result["checked"] is True and result["ok"] is False
        assert next(c for c in result["checks"] if c["name"] == "builtin_mirror") == {
            "name": "builtin_mirror", "ok": False, "detail": "unreadable built-in memory"}
        assert "builtin_mirror" in result["failures"]
        assert result["action"]["status"] == ("requested" if command == "reindex" else "checked")
    else:
        assert "FAIL builtin_mirror" in result and "unreadable built-in memory" in result


@pytest.mark.parametrize("command", ["doctor", "status", "reindex"])
@pytest.mark.parametrize("as_json", [False, True])
@pytest.mark.parametrize("probe", ["engine", "index"])
@pytest.mark.parametrize("response", ["stdout-fail", "stderr-fail", "exception", "stdout-success", "stderr-success"])
def test_untrusted_engine_output_is_never_emitted(
        tmp_path, monkeypatch, capsys, command, as_json, probe, response):
    cli, home, vault = setup_target(tmp_path, monkeypatch)
    def runner(argv):
        current = "engine" if "--version" in argv else "index"
        if current != probe:
            return fake_runner()(argv)
        if response == "exception":
            raise RuntimeError(CANARY)
        out = CANARY if response.startswith("stdout") else ("0.2.2\n" if probe == "engine" else "")
        err = CANARY if response.startswith("stderr") else ""
        return (0 if response.endswith("success") else 1), out, err
    monkeypatch.setattr(cli, "_default_runner", runner)
    rc, result = invoke(cli, command, as_json, capsys)
    healthy = response.endswith("success") and not (probe == "engine" and response == "stdout-success")
    assert rc == (0 if command != "doctor" or healthy else 1)
    assert (vault / cli.REINDEX_REQUEST).exists() is (command == "reindex")
    if as_json:
        check = next(c for c in result["checks"] if c["name"] == probe)
        assert check["ok"] is healthy
        assert check["detail"] in {"0.2.2", "version mismatch", "unavailable", "ready", "not ready"}
        assert result["checked"] is True
        assert result["ok"] is healthy


@pytest.mark.parametrize("command", ["doctor", "status", "reindex"])
@pytest.mark.parametrize("as_json", [False, True])
@pytest.mark.parametrize("failure", ["result", "exception", "decode"])
def test_default_runner_subprocess_diagnostics_are_redacted(
        tmp_path, monkeypatch, capsys, command, as_json, failure):
    cli, home, vault = setup_target(tmp_path, monkeypatch)
    calls = []
    def subprocess_run(argv, **kwargs):
        calls.append(argv)
        if failure == "exception":
            raise OSError(CANARY)
        if failure == "decode":
            raise UnicodeDecodeError("utf-8", b"private-canary-27d4\xff", 19, 20, CANARY)
        return SimpleNamespace(returncode=1, stdout=CANARY, stderr=CANARY)
    monkeypatch.setattr(cli.subprocess, "run", subprocess_run)
    rc, result = invoke(cli, command, as_json, capsys)
    assert len(calls) == 2 and all(call[0] == "/fixture-engine" for call in calls)
    assert_action(cli, vault, command, rc)
    if as_json:
        assert result["checked"] is True and result["ok"] is False
        checks = {c["name"]: c for c in result["checks"]}
        assert checks["engine"]["detail"] == "unavailable"
        assert checks["index"]["detail"] == "not ready"


@pytest.mark.parametrize("command", ["doctor", "status", "reindex"])
@pytest.mark.parametrize("case", ["missing", "empty", "mirrored", "gap"])
def test_builtin_success_and_coverage_controls(tmp_path, monkeypatch, capsys, command, case):
    cli, home, vault = setup_target(tmp_path, monkeypatch)
    if case != "missing":
        (home / "memories/MEMORY.md").write_text(
            {"empty": "", "mirrored": "something worth recalling", "gap": "unmirrored fact"}[case])
    monkeypatch.setattr(cli, "_default_runner", fake_runner())
    rc, result = invoke(cli, command, True, capsys)
    healthy = case != "gap"
    assert rc == (0 if command != "doctor" or healthy else 1)
    assert result["checked"] is True and result["ok"] is healthy
    check = next(c for c in result["checks"] if c["name"] == "builtin_mirror")
    assert check == {"name": "builtin_mirror", "ok": healthy,
                     "detail": "unmirrored=" + ("0" if healthy else "1")}
    assert (vault / cli.REINDEX_REQUEST).exists() is (command == "reindex")


def test_unknown_selection_is_not_admitted_even_when_builtin_is_invalid(tmp_path, monkeypatch, capsys):
    cli, home, vault = setup_target(tmp_path, monkeypatch)
    (home / "zvec-memory/config.json").write_bytes(b"not-json")
    (home / "memories/MEMORY.md").write_bytes(b"\xff")
    monkeypatch.setattr(cli, "_request_reindex", lambda path: pytest.fail("unknown target admitted"))
    rc, result = invoke(cli, "reindex", True, capsys)
    assert rc == 1 and result["ok"] is False and result["action"]["status"] == "failed"
    assert not (vault / cli.REINDEX_REQUEST).exists()
