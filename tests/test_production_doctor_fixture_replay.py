"""Append-only replays of newly stale doubles, preserving original assertions.

Original files and failed receipts are immutable. Only fixture identity/version
and the formerly absent read-denial target are corrected; no product boundary
or assertion is replaced. These do not turn the raw historical suite green.
"""
import json
from pathlib import Path
import os

import pytest
import test_cli as old_cli
import test_cli_health_finish as old_health
import test_cli_health_io_finish as old_io
import test_cli_selection_finish as old_selection

MODEL = "local/potion-retrieval-32m"


def aligned_vault(tmp_path, binary="/zg"):
    vault = old_cli.healthy_vault(tmp_path)
    sidecar = vault / ".zvec-grep/.zvec-memory-state.json"
    sidecar.write_text(json.dumps({"plugin_schema": 1, "embedding": MODEL,
                                 "zg_bin": binary, "zg_version": "0.2.2"}))
    return vault


@pytest.mark.parametrize("case", ["missing-inbox", "doctor-status", "requested-not-completed", "settings-once"])
def test_F3_original_health_oracles_replay_with_pinned_fixture_identity(tmp_path, monkeypatch, capsys, case):
    monkeypatch.setattr(old_health, "healthy_vault", aligned_vault)
    # Retain the old helper reference; recursion would not be a product failure.
    original = old_cli.healthy_vault
    def healthy(path):
        vault = original(path)
        sidecar = vault / ".zvec-grep/.zvec-memory-state.json"
        sidecar.write_text(json.dumps({"plugin_schema": 1, "embedding": MODEL,
                                     "zg_bin": "/zg", "zg_version": "0.2.2"}))
        return vault
    monkeypatch.setattr(old_cli, "healthy_vault", healthy)
    monkeypatch.setattr(old_health, "healthy_vault", healthy)
    if case == "missing-inbox":
        old_cli.test_missing_inbox_is_not_a_failure_and_pending_work_is(tmp_path, monkeypatch)
    elif case == "doctor-status":
        old_cli.test_doctor_and_status_exit_codes_and_json(tmp_path, monkeypatch, capsys)
    elif case == "requested-not-completed":
        old_health.test_reindex_dispatcher_json_requests_not_completed(tmp_path, monkeypatch, capsys, True)
    else:
        old_health.test_dispatcher_reads_authoritative_settings_once(tmp_path, monkeypatch, capsys, None)


def test_F3_original_read_error_oracle_replay_with_a_present_sidecar(tmp_path, monkeypatch, capsys):
    fixture = old_io.selected_fixture
    def present(path, patch):
        cli, home, vault, native = fixture(path, patch)
        index = vault / ".zvec-grep"
        index.mkdir()
        (index / ".zvec-memory-state.json").write_text(json.dumps({
            "plugin_schema": 1, "embedding": MODEL, "zg_bin": "/fixture-engine"}))
        return cli, home, vault, native
    monkeypatch.setattr(old_io, "selected_fixture", present)
    old_io.test_existing_durable_health_error_is_not_reported_as_absence(
        tmp_path, monkeypatch, capsys, ".zvec-grep/.zvec-memory-state.json", "identity", "unreadable")


@pytest.mark.parametrize("scope", ["named", "custom-home", "direct", "unselected"])
@pytest.mark.parametrize("ceiling,ok", [(511, False), (512, True), (1024, True), ("unlimited", True), (None, False)])
def test_F3_nonmanaged_selection_original_oracles_with_real_version_shape(
        tmp_path, monkeypatch, capsys, scope, ceiling, ok):
    cli = old_cli.load_cli()
    default = Path(os.environ["HOME"]) / ".hermes"
    home = (default / "profiles/secondary" if scope == "named" else
            tmp_path / "custom-profile" if scope == "custom-home" else default)
    monkeypatch.setenv("HERMES_HOME", str(home))
    engine = cli._plugin_module("engine")
    config = {"zg_bin": str(engine.launcher_path(home)) if scope in ("named", "custom-home") else "/custom/zg"}
    vault = aligned_vault(tmp_path, config["zg_bin"])
    if scope == "unselected":
        config.pop("zg_bin")
    config_path = old_selection.write_settings(home, vault, config)
    before = config_path.read_bytes()
    calls, caller_reads = [], []
    def run(argv, timeout=15):
        calls.append(argv)
        return 0, "0.2.2\n" if argv[-1] == "--version" else "fixture ready", ""
    def caller():
        caller_reads.append(True)
        return ceiling
    monkeypatch.setattr(cli, "_default_runner", run)
    monkeypatch.setattr(cli, "_task_ceiling", caller)
    assert old_health.dispatch(cli, ["doctor", "--json"]) == (0 if ok and scope != "unselected" else 1)
    payload = json.loads(capsys.readouterr().out)
    service = next(c for c in payload["checks"] if c["name"] == "service")
    tasks = next(c for c in payload["checks"] if c["name"] == "tasks")
    assert service["ok"] is True and "not_managed" in service["detail"]
    assert tasks["ok"] is ok and "caller cgroup" in tasks["detail"]
    assert caller_reads == [True]
    assert not any(argv[0] == "systemctl" for argv in calls)
    assert config_path.read_bytes() == before
    assert cli.MIN_TASK_CEILING == 512


@pytest.mark.parametrize("ceiling,ok", [("511", False), ("512", True), ("1024", True), ("infinity", True), ("max", True), ("", False)])
def test_F3_managed_selection_original_oracles_with_real_version_shape(tmp_path, monkeypatch, capsys, ceiling, ok):
    cli = old_cli.load_cli()
    home, config, node, launcher, unit, manifest_path = old_selection.rendered_managed_artifacts(cli, tmp_path)
    vault = aligned_vault(tmp_path, config["zg_bin"])
    config_path = old_selection.write_settings(home, vault, config)
    paths = [config_path, launcher, unit, manifest_path]
    before = [p.read_bytes() for p in paths]
    start = next(line.split("=", 1)[1] for line in unit.read_text().splitlines() if line.startswith("ExecStart="))
    calls = []
    def run(argv, timeout=15):
        calls.append(argv)
        if argv[0] == "systemctl":
            assert argv[:4] == ["systemctl", "--user", "show", cli.UNIT_NAME]
            return 0, ("LoadState=loaded\nNeedDaemonReload=no\nTasksMax=" + ceiling +
                       "\nExecStart={ path=" + str(launcher) + " ; argv[]=" + start + " ; ignore_errors=no ; }\n"), ""
        return 0, "0.2.2\n" if argv[-1] == "--version" else "fixture ready", ""
    monkeypatch.setattr(cli, "_default_runner", run)
    monkeypatch.setattr(cli, "_task_ceiling", lambda: pytest.fail("Known managed target cannot inspect caller"))
    assert old_health.dispatch(cli, ["doctor", "--json"]) == (0 if ok else 1)
    payload = json.loads(capsys.readouterr().out)
    service = next(c for c in payload["checks"] if c["name"] == "service")
    tasks = next(c for c in payload["checks"] if c["name"] == "tasks")
    assert service["ok"] is True
    assert tasks["ok"] is ok and cli.UNIT_NAME in tasks["detail"]
    assert len([argv for argv in calls if argv[0] == "systemctl"]) == 2
    assert before == [p.read_bytes() for p in paths]
    assert cli.MIN_TASK_CEILING == 512
