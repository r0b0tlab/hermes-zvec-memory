"""CLI diagnostics through real durable files and harmless transport seams."""
import argparse
import json
import os
import sqlite3
from pathlib import Path

import pytest

from test_cli import load_cli, healthy_vault, fake_runner


def dispatch(cli, argv):
    parser = argparse.ArgumentParser()
    cli.register_cli(parser)
    args = parser.parse_args(argv)
    return args.func(args)


def check(cli, vault, name, config=None, runner=None):
    return next(c for c in cli.collect_checks(vault, config or {"zg_bin": "/zg"},
                                            runner=runner or fake_runner()) if c["name"] == name)


@pytest.mark.parametrize("raw", ["secret-not-json", "[]", "{}",
    json.dumps({"records": {"a": {"path": "facts/a.md"}}, "pending_deletes": []}),
    json.dumps({"records": {}, "pending_deletes": [], "last_notification": -1})])
def test_mirror_invalid_present_is_not_healthy(tmp_path, raw):
    cli = load_cli()
    vault = healthy_vault(tmp_path)
    (vault / ".mirror-map.json").write_text(raw)
    result = check(cli, vault, "mirror")
    assert result["ok"] is False
    assert "secret-not-json" not in result["detail"]
    assert cli.vault_state(vault)["mirror_error"]


@pytest.mark.parametrize("kind", ["corrupt", "missing-table", "wrong-columns", "directory"])
def test_inbox_invalid_present_fails_without_crash(tmp_path, kind):
    cli = load_cli()
    vault = healthy_vault(tmp_path)
    inbox = vault / ".mirror-inbox.sqlite3"
    inbox.unlink()
    if kind == "corrupt":
        inbox.write_text("opaque-secret")
    elif kind == "directory":
        inbox.mkdir()
    else:
        with sqlite3.connect(inbox) as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("CREATE TABLE " + ("other (id INTEGER)" if kind == "missing-table"
                                          else "notifications (id INTEGER)"))
    result = check(cli, vault, "inbox")
    assert result["ok"] is False
    assert "opaque-secret" not in result["detail"]


def test_mirror_unreadable_file_fails_without_exception_text(tmp_path, monkeypatch):
    cli = load_cli()
    vault = healthy_vault(tmp_path)
    original = Path.read_text
    def read(path, *args, **kwargs):
        if path.name == ".mirror-map.json":
            raise PermissionError("opaque-secret")
        return original(path, *args, **kwargs)
    monkeypatch.setattr(Path, "read_text", read)
    result = check(cli, vault, "mirror")
    assert result["ok"] is False
    assert "opaque-secret" not in result["detail"]


def managed_fixture(cli, tmp_path, monkeypatch, tasks="1024", *, node_bin=None, **properties):
    """Render actual artifacts; substitute only the systemctl/native boundary."""
    home = Path(os.environ["HOME"]) / ".hermes"
    monkeypatch.setattr(cli, "_hermes_home", lambda: home)
    engine = cli._plugin_module("engine")
    config = {"zg_bin": str(engine.launcher_path(home))}
    if node_bin is not None:
        config["node_bin"] = str(node_bin)
    selected_node = node_bin or engine.NODE_BIN
    launcher = engine.launcher_path(home)
    unit = engine.unit_path()
    launcher.parent.mkdir(parents=True, exist_ok=True)
    unit.parent.mkdir(parents=True, exist_ok=True)
    launcher.write_text(engine.launcher_script(home, config, node=selected_node))
    launcher.chmod(0o700)
    unit.write_text(engine.unit_template(home, config))
    unit.chmod(0o600)
    manifest = {"package": engine.ENGINE_PACKAGE, "version": engine.PINNED_VERSION,
        "node": str(selected_node), "entry": str(engine.entry_path(home, config)),
        "engine_home": str(engine.engine_home(home, config)), "server_url": engine.server_url(config),
        "launcher": str(launcher), "unit": str(unit), "unit_status": "generated",
        "tasks_max": engine.TASKS_MAX}
    (launcher.parent / engine.MANIFEST_NAME).write_text(json.dumps(manifest))
    (launcher.parent / engine.MANIFEST_NAME).chmod(0o600)
    start = next(line.split("=", 1)[1] for line in unit.read_text().splitlines()
                 if line.startswith("ExecStart="))
    values = {"LoadState": "loaded", "TasksMax": tasks, "NeedDaemonReload": "no",
              "ExecStart": "{ path=" + str(launcher) + " ; argv[]=" + start + " ; ignore_errors=no ; }"}
    values.update(properties)
    calls = []
    def run(argv, timeout=15):
        calls.append([str(a) for a in argv])
        if argv[0] == "systemctl":
            assert argv[:3] == ["systemctl", "--user", "show"]
            return 0, "\n".join(f"{k}={v}" for k, v in values.items()), ""
        return fake_runner()(argv, timeout)
    return home, config, engine, values, run, calls


@pytest.mark.parametrize("drift", ["none", "unit", "launcher", "manifest", "new", "loaded", "reload", "noop"])
def test_service_reports_real_artifact_drift_read_only(tmp_path, monkeypatch, drift):
    cli = load_cli()
    vault = healthy_vault(tmp_path)
    home, config, engine, values, run, calls = managed_fixture(cli, tmp_path, monkeypatch)
    unit, launcher = engine.unit_path(), engine.launcher_path(home, config)
    if drift == "unit":
        unit.write_text(unit.read_text() + "# operator edit\n")
    elif drift == "launcher":
        launcher.write_text("#!/bin/sh\nexit 0\n")
    elif drift == "manifest":
        (launcher.parent / engine.MANIFEST_NAME).write_text("{}")
    elif drift == "new":
        unit.with_name(unit.name + ".new").write_text("pending")
    elif drift == "loaded":
        values["ExecStart"] = "{ path=/wrong/profile/zg-default ; argv[]=/wrong/profile/zg-default server run ; }"
    elif drift == "reload":
        values["NeedDaemonReload"] = "yes"
    elif drift == "noop":
        unit.write_text("[Service]\nExecStart=/usr/bin/true\n")
        values["ExecStart"] = "/usr/bin/true"
    before = {str(p): p.read_bytes() for p in [unit, launcher, launcher.parent / engine.MANIFEST_NAME]}
    result = check(cli, vault, "service", config, run)
    assert result["ok"] is (drift == "none"), result
    assert all(p.read_bytes() == before[str(p)] for p in [unit, launcher, launcher.parent / engine.MANIFEST_NAME])
    assert all(c[:3] == ["systemctl", "--user", "show"] for c in calls if c[0] == "systemctl")


@pytest.mark.parametrize("scope", ["named", "custom-home", "direct"])
def test_nonmanaged_scope_never_inspects_default_service(tmp_path, monkeypatch, scope):
    cli = load_cli()
    vault = healthy_vault(tmp_path)
    home, config, engine, values, run, calls = managed_fixture(cli, tmp_path, monkeypatch)
    if scope == "direct":
        config["zg_bin"] = "/custom/zg"
    else:
        other = home / "profiles/secondary" if scope == "named" else tmp_path / "other"
        monkeypatch.setattr(cli, "_hermes_home", lambda: other)
        config["zg_bin"] = str(engine.launcher_path(other))
    result = check(cli, vault, "service", config, run)
    assert result["ok"] is True
    assert "not_managed" in result["detail"]
    assert not any(c[0] == "systemctl" for c in calls)


@pytest.mark.parametrize("raw,ok", [("1024", True), ("128", False), ("infinity", True),
                                  ("", False), ("garbled-secret", False), ("-1", False)])
def test_managed_task_limit_requires_known_target(tmp_path, monkeypatch, raw, ok):
    cli = load_cli()
    _, config, _, _, run, _ = managed_fixture(cli, tmp_path, monkeypatch, tasks=raw)
    monkeypatch.setattr(cli, "_task_ceiling", lambda: 1024)
    result = check(cli, healthy_vault(tmp_path), "tasks", config, run)
    assert result["ok"] is ok
    assert "garbled-secret" not in result["detail"]


@pytest.mark.parametrize("raw,expected", [("max", "unlimited"), ("1024", 1024),
                                        (None, None), ("broken-secret", None)])
def test_direct_task_limit_distinguishes_unlimited_and_unknown(monkeypatch, raw, expected):
    cli = load_cli()
    def read(path, *args, **kwargs):
        if str(path) == "/proc/self/cgroup":
            return "0::/example\n"
        if path.name == "pids.max":
            if raw is None:
                raise PermissionError("secret")
            return raw
        raise AssertionError(str(path))
    monkeypatch.setattr(Path, "read_text", read)
    assert cli._task_ceiling() == expected


@pytest.mark.parametrize("index_ok", [True, False])
def test_reindex_dispatcher_json_requests_not_completed(tmp_path, monkeypatch, capsys, index_ok):
    cli = load_cli()
    vault = healthy_vault(tmp_path)
    monkeypatch.setattr(cli, "_configured", lambda: (vault, {"zg_bin": "/zg"}))
    monkeypatch.setattr(cli, "_default_runner", fake_runner(index_ok=index_ok))
    monkeypatch.setattr(cli, "_task_ceiling", lambda: 1024)
    assert dispatch(cli, ["reindex", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is index_ok
    assert payload["action"]["status"] == "requested"
    assert payload["action"]["completed"] is False
    marker = vault / cli.REINDEX_REQUEST
    assert json.loads(marker.read_text())["by"] == "hermes zvec-memory reindex"
    assert marker.stat().st_mode & 0o777 == 0o600


def test_reindex_dispatcher_failure_is_single_redacted_json(tmp_path, monkeypatch, capsys):
    cli = load_cli()
    vault = healthy_vault(tmp_path)
    marker = vault / cli.REINDEX_REQUEST
    marker.write_text('{"identity":"opaque-secret"}')
    monkeypatch.setattr(cli, "_configured", lambda: (vault, {"zg_bin": "/zg"}))
    monkeypatch.setattr(cli, "_default_runner", fake_runner())
    assert dispatch(cli, ["reindex", "--json"]) == 1
    output = capsys.readouterr().out
    payload = json.loads(output)
    assert payload["action"]["status"] == "failed"
    assert "opaque-secret" not in output
    assert marker.read_text() == '{"identity":"opaque-secret"}'


@pytest.mark.parametrize("placement", ["before", "after"])
@pytest.mark.parametrize("status", ["updated", "no-npm", "exception", "conflict"])
def test_engine_install_dispatcher_emits_one_json(monkeypatch, capsys, placement, status):
    from test_cli import FakeEngine
    cli = load_cli()
    fake = FakeEngine(status=status)
    if status == "exception":
        def install(*args, **kwargs):
            raise RuntimeError("opaque-secret")
        fake.install_engine = install
    elif status == "conflict":
        fake.install_engine = lambda *a, **k: {"status": "updated", "unit_status": "conflict"}
    monkeypatch.setattr(cli, "_engine_module", lambda: fake)
    monkeypatch.setattr(cli, "_configured", lambda: (Path("/unused-vault"), {}))
    monkeypatch.setattr(cli, "_hermes_home", lambda: Path("/unused-home"))
    argv = ["engine", "--json", "install"] if placement == "before" else ["engine", "install", "--json"]
    assert dispatch(cli, argv) == (0 if status == "updated" else 1)
    output = capsys.readouterr().out
    payload = json.loads(output)
    assert payload["health"]["status"] == "not_checked"
    assert payload["action"]["status"] == ("updated" if status == "updated" else "failed")
    assert "opaque-secret" not in output


@pytest.mark.parametrize("native", [None, "{}", '{"context_chars":0}', "opaque-secret", "[]"])
def test_migrate_config_dispatcher_uses_shared_settings_without_health(tmp_path, monkeypatch, capsys, native):
    cli = load_cli()
    home = Path(os.environ["HERMES_HOME"])
    path = home / "zvec-memory/config.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    legacy = {"auto_extract": False, "context_chars": 0, "unknown": [1, "keep"]}
    yaml_path = home / "config.yaml"
    yaml_path.write_text(json.dumps({"plugins": {"zvec-memory": legacy}, "unrelated": "keep"}))
    if native is not None:
        path.write_text(native)
    before_yaml = yaml_path.read_bytes()
    monkeypatch.setattr(cli, "_hermes_home", lambda: home)
    monkeypatch.setattr(cli, "collect_checks", lambda *a, **k: pytest.fail("migration must not probe health"))
    rc = dispatch(cli, ["migrate-config", "--json"])
    output = capsys.readouterr().out
    payload = json.loads(output)
    if native in ("opaque-secret", "[]"):
        assert rc == 1
        assert payload["status"] == "failed"
        assert path.read_text() == native
    else:
        assert rc == 0
        assert payload["status"] == ("migrated" if native is None else "current")
        assert payload["config"] == str(path)
        assert cli._plugin_module("settings").load_settings(home) == (legacy if native is None else json.loads(native))
        if native is None:
            assert path.stat().st_mode & 0o777 == 0o600
    assert payload["health"]["status"] == "not_checked"
    assert "opaque-secret" not in output
    assert yaml_path.read_bytes() == before_yaml


@pytest.mark.parametrize("native", ["{}", "[]", "opaque-secret", None])
def test_dispatcher_reads_authoritative_settings_once(tmp_path, monkeypatch, capsys, native):
    cli = load_cli()
    home = Path(os.environ["HERMES_HOME"])
    path = home / "zvec-memory/config.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    vault = healthy_vault(tmp_path)
    legacy = {"zg_bin": "/zg", "vault": str(vault), "auto_extract": False}
    (home / "config.yaml").write_text(json.dumps({"plugins": {"zvec-memory": legacy}}))
    if native is not None:
        path.write_text(native)
    monkeypatch.setattr(cli, "_hermes_home", lambda: home)
    monkeypatch.setattr(cli, "_default_runner", fake_runner())
    monkeypatch.setattr(cli, "_task_ceiling", lambda: 1024)
    actual_vault, config = cli._configured()
    if native is None:
        assert config == legacy
        assert actual_vault == vault.resolve()
    elif native == "{}":
        assert config == {}
    else:
        assert config.get("_config_error")
        assert "zg_bin" not in config
    assert dispatch(cli, ["doctor", "--json"]) == (0 if native is None else 1)
    output = capsys.readouterr().out
    payload = json.loads(output)
    assert payload["action"]["status"] == "checked"
    assert "opaque-secret" not in output
    assert dispatch(cli, ["status", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["ok"] is (native is None)
    if native is not None:
        assert path.read_text() == native


@pytest.mark.parametrize("fault", ["inbox-open", "mirror-stat", "sidecar-read", "engine-empty", "probe-exception", "service-exception"])
def test_doctor_json_survives_unreadable_boundaries(tmp_path, monkeypatch, capsys, fault):
    cli = load_cli()
    vault = healthy_vault(tmp_path)
    config = {"zg_bin": "/zg"}
    run = fake_runner()
    failing = None
    if fault == "inbox-open":
        def connect(*args, **kwargs):
            raise PermissionError("opaque-secret")
        monkeypatch.setattr(cli.sqlite3, "connect", connect)
        failing = "inbox"
    elif fault == "mirror-stat":
        original = Path.stat
        def stat(path, *args, **kwargs):
            if path.name == ".mirror-map.json":
                raise PermissionError("opaque-secret")
            return original(path, *args, **kwargs)
        monkeypatch.setattr(Path, "stat", stat)
        failing = "mirror"
    elif fault == "sidecar-read":
        original = Path.read_text
        def read(path, *args, **kwargs):
            if path.name == ".zvec-memory-state.json":
                raise PermissionError("opaque-secret")
            return original(path, *args, **kwargs)
        monkeypatch.setattr(Path, "read_text", read)
        failing = "identity"
    elif fault == "engine-empty":
        run = lambda *a, **k: (0, "\n", "")
    elif fault == "probe-exception":
        def run(*args, **kwargs):
            raise OSError("opaque-secret")
        failing = "engine"
    else:
        _, config, _, _, managed_run, _ = managed_fixture(cli, tmp_path, monkeypatch)
        def run(argv, **kwargs):
            if argv[0] == "systemctl":
                raise RuntimeError("opaque-secret")
            return managed_run(argv, **kwargs)
        failing = "service"
    monkeypatch.setattr(cli, "_configured", lambda: (vault, config))
    monkeypatch.setattr(cli, "_default_runner", run)
    monkeypatch.setattr(cli, "_task_ceiling", lambda: 1024)
    rc = dispatch(cli, ["doctor", "--json"])
    output = capsys.readouterr().out
    payload = json.loads(output)
    assert "opaque-secret" not in output
    assert payload["checked"] is True
    if failing:
        assert rc == 1
        assert failing in payload["failures"]


def test_direct_unknown_task_limit_is_not_healthy(tmp_path, monkeypatch):
    cli = load_cli()
    monkeypatch.setattr(cli, "_task_ceiling", lambda: None)
    result = check(cli, healthy_vault(tmp_path), "tasks")
    assert result["ok"] is False
    assert "unknown" in result["detail"]


def test_engine_helper_stays_cold_in_standalone_dispatch():
    import subprocess
    import sys
    from test_cli import PROVIDER_ROOT
    code = f'''
import importlib.util, sys
class Guard:
    def find_spec(self, name, path=None, target=None):
        if name.split('.')[0] in ('agent', 'hermes_cli', 'tools', 'yaml'):
            raise RuntimeError('provider initializer reached')
sys.meta_path.insert(0, Guard())
spec = importlib.util.spec_from_file_location('cold_cli', {str(PROVIDER_ROOT / 'zvec-memory/cli.py')!r})
cli = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cli)
assert cli._engine_module().UNIT_NAME == cli.UNIT_NAME
print('cold')
'''
    result = subprocess.run([sys.executable, '-I', '-B', '-c', code], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == 'cold'


@pytest.mark.parametrize("output", ["OtherLoadState=loaded\nTasksMax=1024\n",
                                     "LoadState=not-found\n# LoadState=loaded\nTasksMax=1024\n"])
def test_service_task_limit_requires_exact_loaded_property(output):
    cli = load_cli()
    ceiling, source = cli._unit_tasks_max(lambda *a: (0, output, ""))
    assert ceiling is None
    assert source == cli.UNIT_NAME


def test_engine_install_refuses_invalid_present_settings(monkeypatch, capsys):
    from test_cli import FakeEngine
    cli = load_cli()
    fake = FakeEngine()
    monkeypatch.setattr(cli, "_engine_module", lambda: fake)
    monkeypatch.setattr(cli, "_configured", lambda: (Path("/unused"), {"_config_error": "JSONDecodeError"}))
    monkeypatch.setattr(cli, "_hermes_home", lambda: Path("/unused-home"))
    assert dispatch(cli, ["engine", "install", "--json"]) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["action"]["status"] == "failed"
    assert fake.versions == []


def test_mirror_absent_and_valid_empty_are_distinct(tmp_path):
    cli = load_cli()
    vault = healthy_vault(tmp_path)
    mapping = vault / ".mirror-map.json"
    mapping.unlink()
    assert check(cli, vault, "mirror")["ok"] is True
    assert cli.vault_state(vault)["mirror_records"] is None
    mapping.write_text(json.dumps({"records": {}, "pending_deletes": []}))
    assert check(cli, vault, "mirror")["ok"] is True
    assert cli.vault_state(vault)["mirror_records"] == 0
@pytest.mark.parametrize("case", ["selected", "config-absent", "configured-drift", "missing", "relative"])
def test_service_uses_installed_admitted_node_not_fixed_system_node(tmp_path, monkeypatch, case):
    cli = load_cli()
    node = tmp_path / "version-manager" / "bin" / "node"
    node.parent.mkdir(parents=True)
    node.write_text("#!/bin/sh\nif [ \"$1\" = \"--version\" ]; then printf 'v22.16.0\\n'; else printf '0.2.2\\n'; fi\n")
    node.chmod(0o700)
    # Start with one matching generation; strict setup must not overwrite an
    # already installed launcher from a different Node selection.
    home, config, engine, values, run, calls = managed_fixture(
        cli, tmp_path, monkeypatch, node_bin=node)
    entry = engine.entry_path(home, config)
    entry.parent.mkdir(parents=True, exist_ok=True)
    entry.write_text("// harmless readiness fixture; no native execution\n")
    engine.package_json_path(home, config).write_text(json.dumps({
        "name": engine.ENGINE_PACKAGE, "version": engine.PINNED_VERSION}))
    laid_out = engine.ensure_engine(home, config)
    assert laid_out["status"] in ("updated", "present")
    manifest_path = engine.runtime_root(home, config) / engine.MANIFEST_NAME
    manifest = json.loads(manifest_path.read_text())
    assert manifest["node"] == str(node.resolve())
    if case == "config-absent":
        config.pop("node_bin")
    elif case == "configured-drift":
        config["node_bin"] = engine.NODE_BIN
    elif case == "missing":
        node.unlink()
    elif case == "relative":
        manifest["node"] = "version-manager/bin/node"
        manifest_path.write_text(json.dumps(manifest))
    paths = [engine.launcher_path(home, config), engine.unit_path(), manifest_path]
    before = [(path.read_bytes(), path.stat().st_mtime_ns, path.stat().st_mode) for path in paths]
    ok, detail, managed = cli._service_diagnostics(config, run)
    assert managed
    assert ok is (case in ("selected", "config-absent")), detail
    assert before == [(path.read_bytes(), path.stat().st_mtime_ns, path.stat().st_mode) for path in paths]
    assert all(argv[:3] == ["systemctl", "--user", "show"] for argv in calls)

