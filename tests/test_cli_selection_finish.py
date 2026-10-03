"""Real JSON dispatcher must survive unknown service selection."""
import json
import os
from pathlib import Path

import pytest

from test_cli import healthy_vault, load_cli
from test_cli_health_finish import dispatch


@pytest.mark.parametrize("command", ["doctor", "status", "reindex"])
@pytest.mark.parametrize("fault,exception_name", [("nul", "ValueError"),
                                                 ("expanduser", "RuntimeError"),
                                                 ("resolve", "PermissionError")])
def test_selection_failure_is_unhealthy_json_and_reindex_still_admitted(
        tmp_path, monkeypatch, capsys, command, fault, exception_name):
    cli = load_cli()
    user_home = tmp_path / "user"
    home = user_home / ".hermes"
    home.mkdir(parents=True)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: user_home))
    monkeypatch.setenv("HERMES_HOME", str(home))
    vault = healthy_vault(tmp_path)
    sentinel = "opaque-private-resolution-detail"
    selected = {"nul": "bad\x00" + sentinel,
                "expanduser": "~zvec_nonexistent_user_726894/private",
                "resolve": str(tmp_path / sentinel)}[fault]
    if fault == "resolve":
        original = Path.resolve
        def resolve(path, *args, **kwargs):
            if str(path) == selected:
                raise PermissionError(sentinel)
            return original(path, *args, **kwargs)
        monkeypatch.setattr(Path, "resolve", resolve)
    path = home / "zvec-memory" / "config.json"
    path.parent.mkdir()
    path.write_text(json.dumps({"vault": str(vault), "zg_bin": selected}))
    original_config = path.read_bytes()
    calls = []
    def run(argv, timeout=15):
        calls.append(argv)
        if argv[0] == "systemctl":
            pytest.fail("Unknown executable selection must not inspect default service")
        raise ValueError(sentinel)
    monkeypatch.setattr(cli, "_default_runner", run)
    try:
        rc = dispatch(cli, [command, "--json"])
    except (ValueError, OSError, RuntimeError) as exc:
        pytest.fail("Selection exception escaped JSON dispatcher: " + type(exc).__name__)
    output = capsys.readouterr().out
    payload = json.loads(output)
    assert sentinel not in output
    assert "zvec_nonexistent_user_726894" not in output
    assert payload["ok"] is False
    service = next(check for check in payload["checks"] if check["name"] == "service")
    assert service["ok"] is False
    assert exception_name in service["detail"]
    assert "not_managed" not in service["detail"]
    assert path.read_bytes() == original_config
    if command == "reindex":
        assert rc == 0
        assert payload["action"]["status"] == "requested"
        assert payload["action"]["completed"] is False
        assert json.loads((vault / cli.REINDEX_REQUEST).read_text())["by"] == "hermes zvec-memory reindex"
    else:
        assert rc == (1 if command == "doctor" else 0)
        assert not (vault / cli.REINDEX_REQUEST).exists()
    assert calls
    assert not any(argv[0] == "systemctl" for argv in calls)


@pytest.mark.parametrize("command", ["doctor", "status", "reindex"])
@pytest.mark.parametrize("caller_ceiling", [512, 1024, "unlimited", None])
def test_unknown_selection_does_not_claim_caller_task_target(
        tmp_path, monkeypatch, capsys, command, caller_ceiling):
    cli = load_cli()
    home = Path(os.environ["HOME"]) / ".hermes"
    vault = healthy_vault(tmp_path)
    config_path = home / "zvec-memory" / "config.json"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(json.dumps({"vault": str(vault), "zg_bin": "bad\x00path"}))
    calls = []
    def run(argv, timeout=15):
        calls.append(argv)
        return 0, "fixture ready", ""
    caller_reads = []
    def caller():
        caller_reads.append(True)
        return caller_ceiling
    monkeypatch.setattr(cli, "_default_runner", run)
    monkeypatch.setattr(cli, "_task_ceiling", caller)
    rc = dispatch(cli, [command, "--json"])
    payload = json.loads(capsys.readouterr().out)
    tasks = next(c for c in payload["checks"] if c["name"] == "tasks")
    assert not caller_reads, "Unknown selection cannot establish caller cgroup as engine target"
    assert tasks["ok"] is False
    assert "unknown" in tasks["detail"] and "selection" in tasks["detail"]
    assert "caller cgroup" not in tasks["detail"]
    assert cli.UNIT_NAME not in tasks["detail"]
    assert payload["ok"] is False
    assert not any(argv[0] == "systemctl" for argv in calls)
    assert rc == (1 if command == "doctor" else 0)
    if command == "reindex":
        assert payload["action"]["status"] == "requested"
        assert payload["action"]["completed"] is False
        assert json.loads((vault / cli.REINDEX_REQUEST).read_text())["by"] == "hermes zvec-memory reindex"


def write_settings(home, vault, config):
    path = home / "zvec-memory" / "config.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"vault": str(vault), **config}))
    return path


def rendered_managed_artifacts(cli, tmp_path):
    """Actual helper rendering, no ensure/install/native or service operation."""
    home = Path(os.environ["HOME"]) / ".hermes"
    engine = cli._plugin_module("engine")
    node = tmp_path / "admitted-node"
    node.write_text("#!/bin/sh\nexit 0\n")
    node.chmod(0o700)
    config = {"zg_bin": str(engine.launcher_path(home)), "node_bin": str(node)}
    launcher, unit = engine.launcher_path(home, config), engine.unit_path()
    launcher.parent.mkdir(parents=True, exist_ok=True)
    unit.parent.mkdir(parents=True, exist_ok=True)
    launcher.write_text(engine.launcher_script(home, config, node=node))
    launcher.chmod(0o700)
    unit.write_text(engine.unit_template(home, config))
    manifest_path = engine.runtime_root(home, config) / engine.MANIFEST_NAME
    manifest_path.write_text(json.dumps({
        "package": engine.ENGINE_PACKAGE, "version": engine.PINNED_VERSION,
        "node": str(node), "entry": str(engine.entry_path(home, config)),
        "engine_home": str(engine.engine_home(home, config)),
        "server_url": engine.server_url(config), "launcher": str(launcher),
        "unit": str(unit), "unit_status": "generated", "tasks_max": engine.TASKS_MAX}))
    return home, config, node, launcher, unit, manifest_path


@pytest.mark.parametrize("command", ["doctor", "status", "reindex"])
@pytest.mark.parametrize("seam", ["configured-node", "admitted-node", "artifact-path", "artifact-read"])
def test_managed_artifact_runtime_error_is_class_only_json(
        tmp_path, monkeypatch, capsys, command, seam):
    cli = load_cli()
    home, config, node, launcher, unit, manifest_path = rendered_managed_artifacts(cli, tmp_path)
    sentinel = "opaque-artifact-resolution-detail"
    if seam == "configured-node":
        config["node_bin"] = "~zvec_nonexistent_user_726894/node"
    elif seam == "admitted-node":
        original = Path.stat
        def stat(path, *args, **kwargs):
            if path == node:
                raise RuntimeError(sentinel)
            return original(path, *args, **kwargs)
        monkeypatch.setattr(Path, "stat", stat)
    elif seam == "artifact-path":
        config["engine_home"] = "~zvec_nonexistent_user_726894/state"
    else:
        original = Path.read_text
        def read(path, *args, **kwargs):
            if path == manifest_path:
                raise RuntimeError(sentinel)
            return original(path, *args, **kwargs)
        monkeypatch.setattr(Path, "read_text", read)
    vault = healthy_vault(tmp_path)
    config_path = write_settings(home, vault, config)
    paths = [config_path, launcher, unit, manifest_path]
    before = [p.read_bytes() for p in paths]
    calls = []
    def run(argv, timeout=15):
        calls.append(argv)
        if argv[0] == "systemctl":
            assert argv[:3] == ["systemctl", "--user", "show"]
            assert "--property=TasksMax" in argv, "Artifact failure must not query loaded definition"
            return 0, "LoadState=loaded\nTasksMax=512\n", ""
        return 0, "fixture ready", ""
    monkeypatch.setattr(cli, "_default_runner", run)
    monkeypatch.setattr(cli, "_task_ceiling", lambda: pytest.fail("Known managed target cannot use caller"))
    try:
        rc = dispatch(cli, [command, "--json"])
    except RuntimeError:
        pytest.fail("Artifact RuntimeError escaped JSON dispatcher")
    output = capsys.readouterr().out
    payload = json.loads(output)
    service = next(c for c in payload["checks"] if c["name"] == "service")
    tasks = next(c for c in payload["checks"] if c["name"] == "tasks")
    assert service["ok"] is False
    assert service["detail"] == "unreadable artifacts: RuntimeError"
    assert tasks["ok"] is True and cli.UNIT_NAME in tasks["detail"]
    assert sentinel not in output and "zvec_nonexistent_user_726894" not in output
    assert before == [p.read_bytes() for p in paths]
    assert rc == (1 if command == "doctor" else 0)
    if command == "reindex":
        assert payload["action"]["status"] == "requested"
        assert payload["action"]["completed"] is False
        assert json.loads((vault / cli.REINDEX_REQUEST).read_text())["by"] == "hermes zvec-memory reindex"


@pytest.mark.parametrize("command", ["doctor", "status", "reindex"])
@pytest.mark.parametrize("fault,exception_name", [
    ("home-resolve", "PermissionError"), ("default-home", "RuntimeError"),
    ("runtime-nul", "ValueError"), ("runtime-expanduser", "RuntimeError"),
    ("runtime-resolve", "PermissionError"), ("runtime-loop", "RuntimeError")])
def test_guarded_home_and_runtime_selection_boundaries(
        tmp_path, monkeypatch, capsys, command, fault, exception_name):
    cli = load_cli()
    home = Path(os.environ["HOME"]) / ".hermes"
    vault = healthy_vault(tmp_path)
    sentinel = "opaque-home-runtime-detail"
    config = {"zg_bin": "/custom/zg"}
    if fault == "runtime-nul":
        config["runtime_dir"] = "bad\x00" + sentinel
    elif fault == "runtime-expanduser":
        config["runtime_dir"] = "~zvec_nonexistent_user_726894/runtime"
    elif fault in ("runtime-resolve", "runtime-loop"):
        config["runtime_dir"] = str(tmp_path / sentinel)
    config_path = write_settings(home, vault, config)
    before = config_path.read_bytes()
    if fault == "default-home":
        def missing(cls):
            raise RuntimeError(sentinel)
        monkeypatch.setattr(Path, "home", classmethod(missing))
    elif fault in ("home-resolve", "runtime-resolve", "runtime-loop"):
        original = Path.resolve
        target = home if fault == "home-resolve" else Path(config["runtime_dir"])
        def resolve(path, *args, **kwargs):
            if path == target:
                if fault == "runtime-loop":
                    raise RuntimeError(sentinel)
                raise PermissionError(sentinel)
            return original(path, *args, **kwargs)
        monkeypatch.setattr(Path, "resolve", resolve)
    calls = []
    def run(argv, timeout=15):
        calls.append(argv)
        return 0, "fixture ready", ""
    monkeypatch.setattr(cli, "_default_runner", run)
    monkeypatch.setattr(cli, "_task_ceiling", lambda: pytest.fail("Unknown target cannot inspect caller"))
    rc = dispatch(cli, [command, "--json"])
    output = capsys.readouterr().out
    payload = json.loads(output)
    service = next(c for c in payload["checks"] if c["name"] == "service")
    tasks = next(c for c in payload["checks"] if c["name"] == "tasks")
    assert service == {"name": "service", "ok": False,
                       "detail": "unreadable service selection: " + exception_name}
    assert tasks["ok"] is False and "selection" in tasks["detail"]
    assert "not_managed" not in output and "caller cgroup" not in output
    assert sentinel not in output and "zvec_nonexistent_user_726894" not in output
    assert not any(argv[0] == "systemctl" for argv in calls)
    assert config_path.read_bytes() == before
    assert rc == (1 if command == "doctor" else 0)
    if command == "reindex":
        assert payload["action"]["status"] == "requested"
        assert payload["action"]["completed"] is False
        assert json.loads((vault / cli.REINDEX_REQUEST).read_text())["by"] == "hermes zvec-memory reindex"


@pytest.mark.parametrize("scope", ["named", "custom-home", "direct", "unselected"])
@pytest.mark.parametrize("ceiling,ok", [(511, False), (512, True), (1024, True),
                                      ("unlimited", True), (None, False)])
def test_valid_nonmanaged_selection_keeps_noninspection_and_task_floor(
        tmp_path, monkeypatch, capsys, scope, ceiling, ok):
    cli = load_cli()
    default = Path(os.environ["HOME"]) / ".hermes"
    home = (default / "profiles/secondary" if scope == "named" else
            tmp_path / "custom-profile" if scope == "custom-home" else default)
    monkeypatch.setenv("HERMES_HOME", str(home))
    vault = healthy_vault(tmp_path)
    engine = cli._plugin_module("engine")
    config = {"zg_bin": str(engine.launcher_path(home)) if scope in ("named", "custom-home") else "/custom/zg"}
    if scope == "unselected":
        config.pop("zg_bin")
    config_path = write_settings(home, vault, config)
    before = config_path.read_bytes()
    calls, caller_reads = [], []
    def run(argv, timeout=15):
        calls.append(argv)
        return 0, "fixture ready", ""
    def caller():
        caller_reads.append(True)
        return ceiling
    monkeypatch.setattr(cli, "_default_runner", run)
    monkeypatch.setattr(cli, "_task_ceiling", caller)
    assert dispatch(cli, ["doctor", "--json"]) == (0 if ok and scope != "unselected" else 1)
    payload = json.loads(capsys.readouterr().out)
    service = next(c for c in payload["checks"] if c["name"] == "service")
    tasks = next(c for c in payload["checks"] if c["name"] == "tasks")
    assert service["ok"] is True and "not_managed" in service["detail"]
    assert tasks["ok"] is ok and "caller cgroup" in tasks["detail"]
    assert caller_reads == [True]
    assert not any(argv[0] == "systemctl" for argv in calls)
    assert config_path.read_bytes() == before
    assert cli.MIN_TASK_CEILING == 512


@pytest.mark.parametrize("ceiling,ok", [("511", False), ("512", True), ("1024", True),
                                      ("infinity", True), ("max", True), ("", False)])
def test_valid_managed_selection_keeps_exact_target_and_task_floor(
        tmp_path, monkeypatch, capsys, ceiling, ok):
    cli = load_cli()
    home, config, node, launcher, unit, manifest_path = rendered_managed_artifacts(cli, tmp_path)
    vault = healthy_vault(tmp_path)
    config_path = write_settings(home, vault, config)
    paths = [config_path, launcher, unit, manifest_path]
    before = [p.read_bytes() for p in paths]
    start = next(line.split("=", 1)[1] for line in unit.read_text().splitlines()
                 if line.startswith("ExecStart="))
    calls = []
    def run(argv, timeout=15):
        calls.append(argv)
        if argv[0] == "systemctl":
            assert argv[:4] == ["systemctl", "--user", "show", cli.UNIT_NAME]
            return 0, ("LoadState=loaded\nNeedDaemonReload=no\nTasksMax=" + ceiling +
                       "\nExecStart={ path=" + str(launcher) + " ; argv[]=" + start + " ; ignore_errors=no ; }\n"), ""
        return 0, "fixture ready", ""
    monkeypatch.setattr(cli, "_default_runner", run)
    monkeypatch.setattr(cli, "_task_ceiling", lambda: pytest.fail("Known managed target cannot inspect caller"))
    assert dispatch(cli, ["doctor", "--json"]) == (0 if ok else 1)
    payload = json.loads(capsys.readouterr().out)
    service = next(c for c in payload["checks"] if c["name"] == "service")
    tasks = next(c for c in payload["checks"] if c["name"] == "tasks")
    assert service["ok"] is True
    assert tasks["ok"] is ok and cli.UNIT_NAME in tasks["detail"]
    assert len([argv for argv in calls if argv[0] == "systemctl"]) == 2
    assert before == [p.read_bytes() for p in paths]
    assert cli.MIN_TASK_CEILING == 512


def test_service_boundary_does_not_swallow_unrelated_source_defects(tmp_path, monkeypatch):
    cli = load_cli()
    engine = cli._plugin_module("engine")
    def defect(*args, **kwargs):
        raise AssertionError("source defect is not an unreadable path")
    monkeypatch.setattr(engine, "launcher_path", defect)
    with pytest.raises(AssertionError, match="source defect"):
        cli.collect_checks(healthy_vault(tmp_path), {"zg_bin": "/custom/zg"},
                           runner=lambda argv: (0, "fixture ready", ""))
