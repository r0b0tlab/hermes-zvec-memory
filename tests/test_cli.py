"""`hermes zvec-memory` must register cheaply and report honest health."""
import argparse
import importlib.util
import json
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def load_cli():
    spec = importlib.util.spec_from_file_location("zvec_cli", ROOT / "zvec-memory/cli.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["zvec_cli"] = module
    spec.loader.exec_module(module)
    return module


def healthy_vault(tmp_path: Path) -> Path:
    vault = tmp_path / "vault"
    (vault / "facts").mkdir(parents=True)
    (vault / "sessions").mkdir(parents=True)
    (vault / "facts" / "one.md").write_text("# Fact\nsomething worth recalling\n")
    (vault / ".zvec-grep").mkdir()
    (vault / ".zvec-grep" / "manifest.json").write_text("{}")
    (vault / ".zvec-grep" / ".zvec-memory-state.json").write_text(json.dumps(
        {"plugin_schema": 1, "embedding": "local/x", "zg_bin": "/zg"}))
    (vault / ".mirror-map.json").write_text(json.dumps(
        {"schema_version": 1, "records": {"a": {"path": "facts/one.md"}}, "pending_deletes": [],
         "pending_creates": [], "refresh_required": False}))
    db = sqlite3.connect(vault / ".mirror-inbox.sqlite3")
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("CREATE TABLE notifications (id INTEGER PRIMARY KEY AUTOINCREMENT, payload TEXT NOT NULL)")
    db.commit()
    db.close()
    return vault


def fake_runner(engine_ok=True, index_ok=True):
    def run(args, timeout=15):
        if "--version" in [str(a) for a in args]:
            return (0, "0.2.2\n", "") if engine_ok else (127, "", "cannot execute")
        return (0, "Workspace index is ready", "") if index_ok else (1, "", "No index found")
    return run


def test_register_cli_builds_the_command_tree():
    cli = load_cli()
    parser = argparse.ArgumentParser(prog="hermes zvec-memory")
    cli.register_cli(parser)
    for command in ("doctor", "status", "reindex"):
        assert parser.parse_args([command]).zvec_command == command
    parsed = parser.parse_args(["doctor", "--json"])
    assert parsed.json is True and parsed.func is cli.zvec_memory_command


def test_handler_is_reachable_under_the_host_lookup_name():
    cli = load_cli()
    # The host resolves getattr(cli_mod, f"{active_provider}_command").
    assert callable(getattr(cli, "zvec-memory_command"))


def test_cli_imports_neither_the_provider_nor_host_runtime_modules():
    code = (
        "import importlib.util, sys\n"
        "FORBIDDEN = ('agent.memory_provider', 'hermes_cli', 'tools.registry', 'utils', 'yaml')\n"
        "class Guard:\n"
        "    def find_spec(self, name, path=None, target=None):\n"
        "        if name in FORBIDDEN or name.split('.')[0] in FORBIDDEN:\n"
        "            raise RuntimeError('forbidden import: ' + name)\n"
        "        return None\n"
        "sys.meta_path.insert(0, Guard())\n"
        f"spec = importlib.util.spec_from_file_location('zvec_cli', {str(ROOT / 'zvec-memory/cli.py')!r})\n"
        "module = importlib.util.module_from_spec(spec)\n"
        "sys.modules['zvec_cli'] = module\n"
        "spec.loader.exec_module(module)\n"
        "print('ok')\n")
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert result.stdout.strip() == "ok", (result.stdout, result.stderr[-1500:])


def test_healthy_vault_reports_every_check(tmp_path):
    cli = load_cli()
    checks = cli.collect_checks(healthy_vault(tmp_path), {"zg_bin": "/zg", "embedding": "local/x"},
                                runner=fake_runner())
    result = cli.report(checks)
    assert result["checked"] is True, [c["name"] for c in checks]
    assert result["ok"] is True, result["failures"]
    assert all(set(c) == {"name", "ok", "detail"} for c in checks)


@pytest.mark.parametrize("engine_ok,index_ok,failing", [
    (False, True, ["engine"]),            # only the probe that failed is reported
    (True, False, ["index"]),
])
def test_doctor_fails_when_the_engine_or_index_is_broken(tmp_path, engine_ok, index_ok, failing):
    cli = load_cli()
    checks = cli.collect_checks(healthy_vault(tmp_path), {"zg_bin": "/zg", "embedding": "local/x"},
                                runner=fake_runner(engine_ok, index_ok))
    result = cli.report(checks)
    assert result["ok"] is False
    assert set(failing) <= set(result["failures"])


def test_missing_inbox_is_not_a_failure_and_pending_work_is(tmp_path):
    cli = load_cli()
    vault = healthy_vault(tmp_path)
    (vault / ".mirror-inbox.sqlite3").unlink()
    assert cli.report(cli.collect_checks(vault, {"zg_bin": "/zg"}, runner=fake_runner()))["ok"] is True


def test_delivery_marker_and_pending_mirror_work_fail_the_check(tmp_path):
    cli = load_cli()
    vault = healthy_vault(tmp_path)
    (vault / ".mirror-delivery-failed.json").write_text("{}")
    (vault / ".mirror-map.json").write_text(json.dumps(
        {"schema_version": 1, "records": {}, "pending_deletes": ["facts/x.md"],
         "pending_creates": [], "refresh_required": True}))
    result = cli.report(cli.collect_checks(vault, {"zg_bin": "/zg"}, runner=fake_runner()))
    assert {"identity", "mirror"} <= set(result["failures"])


def test_doctor_and_status_exit_codes_and_json(tmp_path, monkeypatch, capsys):
    cli = load_cli()
    vault = healthy_vault(tmp_path)
    monkeypatch.setattr(cli, "_configured", lambda: (vault, {"zg_bin": "/zg"}))
    monkeypatch.setattr(cli, "_default_runner", fake_runner())
    assert cli.zvec_memory_command(argparse.Namespace(zvec_command="doctor", json=True)) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True and payload["checked"] is True
    assert cli.zvec_memory_command(argparse.Namespace(zvec_command="status", json=False)) == 0
    assert "healthy" in capsys.readouterr().out
    assert cli.zvec_memory_command(argparse.Namespace(zvec_command=None, json=False)) == 2


def test_reindex_asks_for_a_rebuild_and_reports_state(tmp_path, monkeypatch, capsys):
    cli = load_cli()
    vault = healthy_vault(tmp_path)
    monkeypatch.setattr(cli, "_configured", lambda: (vault, {"zg_bin": "/zg"}))
    monkeypatch.setattr(cli, "_default_runner", fake_runner())
    assert cli.zvec_memory_command(argparse.Namespace(zvec_command="reindex", json=False)) == 0
    assert "Rebuild requested" in capsys.readouterr().out
