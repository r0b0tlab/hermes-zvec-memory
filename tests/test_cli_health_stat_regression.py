"""Cross-version filesystem observation controls for cold CLI diagnostics."""
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from test_cli import fake_runner, healthy_vault, load_cli
from test_cli_health_finish import dispatch
from test_cli_health_io_finish import inject_health_fault
from test_cli_selection_finish import rendered_managed_artifacts, write_settings


@pytest.mark.parametrize("leaf", [".mirror-delivery-failed.json", ".reindex-request.json",
                                  ".index-refresh.json", ".zvec-grep/manifest.json"])
@pytest.mark.parametrize("command", ["doctor", "status", "reindex"])
def test_stat_errors_are_incomplete_and_reindex_remains_deferred(tmp_path, monkeypatch, capsys, leaf, command):
    cli = load_cli()
    vault = healthy_vault(tmp_path)
    home = tmp_path / "profile"
    write_settings(home, vault, {"zg_bin": "/zg"})
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(cli, "_default_runner", fake_runner())
    reached = []
    inject_health_fault(monkeypatch, vault / leaf, "stat",
                        lambda: PermissionError("secret-stat-error\x1b[31m"), reached)
    rc = dispatch(cli, [command, "--json"])
    output = capsys.readouterr().out
    payload = json.loads(output)
    assert reached and "secret-stat-error" not in output
    assert payload["checked"] is False and payload["ok"] is False
    assert payload["checks"] == [{"name": "health", "ok": False, "detail": "unreadable: PermissionError"}]
    if command == "reindex":
        assert payload["action"]["completed"] is False
        # The persistent fault can also deny admission at its own marker.
        if payload["action"]["status"] == "failed":
            assert leaf == ".reindex-request.json" and rc == 1
            assert payload["action"]["error"] == "PermissionError"
        else:
            assert payload["action"]["status"] == "requested" and rc == 0
    else:
        assert rc == (1 if command == "doctor" else 0)


@pytest.mark.parametrize("leaf", [".mirror-delivery-failed.json", ".reindex-request.json", ".index-refresh.json"])
def test_absent_and_dangling_marker_are_distinct(tmp_path, leaf):
    cli = load_cli()
    vault = healthy_vault(tmp_path)
    key = {".mirror-delivery-failed.json": "delivery_failed", ".reindex-request.json": "rebuild_pending",
           ".index-refresh.json": "refresh_pending"}[leaf]
    assert cli.vault_state(vault)[key] is False
    (vault / leaf).symlink_to("missing-target")
    assert cli.vault_state(vault)[key] is True
    result = cli.report(cli.collect_checks(vault, {"zg_bin": "/zg"}, fake_runner()))
    assert result["ok"] is False and "identity" in result["failures"]


@pytest.mark.parametrize("where", ["node", "launcher", "unit", "conflict"])
@pytest.mark.parametrize("command", ["doctor", "status", "reindex"])
def test_managed_stat_failure_is_not_missing_or_healthy(tmp_path, monkeypatch, capsys, where, command):
    cli = load_cli()
    home, config, node, launcher, unit, manifest = rendered_managed_artifacts(cli, tmp_path)
    vault = healthy_vault(tmp_path)
    write_settings(home, vault, config)
    target = {"node": node, "launcher": launcher, "unit": unit,
              "conflict": launcher.with_name(launcher.name + ".new")}[where]
    reached = []
    inject_health_fault(monkeypatch, target, "stat", lambda: PermissionError("secret-service"), reached)
    monkeypatch.setattr(cli, "_default_runner", fake_runner())
    rc = dispatch(cli, [command, "--json"])
    output = capsys.readouterr().out
    payload = json.loads(output)
    service = next(c for c in payload["checks"] if c["name"] == "service")
    assert reached and service["ok"] is False and "PermissionError" in service["detail"]
    assert "secret-service" not in output and payload["ok"] is False
    assert rc == (1 if command == "doctor" else 0)
    if command == "reindex":
        assert payload["action"]["status"] == "requested" and payload["action"]["completed"] is False


# The child performs a real stat/scandir permission denial, then runs the public
# command dispatcher without monkeypatching filesystem operations.
_CHILD = r'''
import argparse, errno, importlib.util, json, os, sys
from pathlib import Path
root, vault, home, target, command = map(str, sys.argv[1:])
sys.path.insert(0, os.environ["HERMES_AGENT_DIR"])
spec = importlib.util.spec_from_file_location("cold_cli", Path(root)/"zvec-memory/cli.py")
cli = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cli)
os.environ["HERMES_HOME"] = home
try:
    if target in ("facts", "sessions"):
        with os.scandir(Path(vault)/target) as entries:
            list(entries)
    else:
        os.stat(Path(vault)/".zvec-grep/manifest.json")
except OSError as exc:
    assert exc.errno == errno.EACCES, (type(exc).__name__, exc.errno)
else:
    raise AssertionError("expected kernel EACCES")
cli._default_runner = lambda *a, **k: (0, "0.2.2\n", "")
parser = argparse.ArgumentParser()
cli.register_cli(parser)
args = parser.parse_args([command, "--json"])
raise SystemExit(args.func(args))
'''


@pytest.mark.parametrize("target", ["facts", "sessions", "index"])
@pytest.mark.parametrize("command", ["doctor", "status", "reindex"])
def test_kernel_eacces_is_incomplete_in_real_subprocess(tmp_path, target, command):
    assert os.geteuid() != 0, "Permission control requires a non-root process"
    vault = healthy_vault(tmp_path)
    home = tmp_path / "profile"
    write_settings(home, vault, {"zg_bin": "/zg"})
    denied = vault / ({"facts": "facts", "sessions": "sessions", "index": ".zvec-grep"}[target])
    old_mode = denied.stat().st_mode & 0o7777
    denied.chmod(0)
    try:
        root = Path(__file__).resolve().parents[1]
        child = subprocess.run([sys.executable, "-B", "-c", _CHILD, str(root), str(vault), str(home), target, command],
                               capture_output=True, text=True, timeout=20, env=os.environ.copy())
    finally:
        denied.chmod(old_mode)
    assert child.returncode == (1 if command == "doctor" else 0), child.stderr
    assert child.stderr == ""
    payload = json.loads(child.stdout)
    assert payload["ok"] is False and payload["checked"] is False
    assert payload["checks"] == [{"name": "health", "ok": False, "detail": "unreadable: PermissionError"}]
    if command == "reindex":
        assert payload["action"]["status"] == "requested" and payload["action"]["completed"] is False
        assert (vault / ".reindex-request.json").is_file()
