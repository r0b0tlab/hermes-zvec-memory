"""Configured-vault failures must not escape or redirect the JSON dispatcher."""
import json
from pathlib import Path

import pytest

from test_cli import load_cli
from test_cli_health_finish import dispatch


@pytest.mark.parametrize("command", ["doctor", "status", "reindex"])
@pytest.mark.parametrize("fault,exception_name", [("nul", "ValueError"),
                                                 ("expanduser", "RuntimeError"),
                                                 ("resolve", "PermissionError")])
def test_unknown_configured_vault_is_redacted_json_without_default_admission(
        tmp_path, monkeypatch, capsys, command, fault, exception_name):
    cli = load_cli()
    home = tmp_path / "selected-home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    sentinel = "opaque-private-vault-resolution-detail"
    selected = {"nul": "bad\x00" + sentinel,
                "expanduser": "~zvec_nonexistent_user_726894/" + sentinel,
                "resolve": str(tmp_path / sentinel)}[fault]
    if fault == "resolve":
        original = Path.resolve
        def resolve(path, *args, **kwargs):
            if str(path) == selected:
                raise PermissionError(sentinel)
            return original(path, *args, **kwargs)
        monkeypatch.setattr(Path, "resolve", resolve)
    config_path = home / "zvec-memory" / "config.json"
    config_path.parent.mkdir()
    config_path.write_text(json.dumps({"vault": selected, "zg_bin": "/zvec-test-only"}))
    original_config = config_path.read_bytes()
    monkeypatch.setattr(cli, "_default_runner", lambda *a, **k: pytest.fail(
        "Unknown vault cannot probe an engine or service"))
    monkeypatch.setattr(cli, "_task_ceiling", lambda: pytest.fail(
        "Unknown vault cannot borrow the caller task target"))
    requests = []
    request = cli._request_reindex
    def admit(vault):
        requests.append(str(vault))
        return request(vault)
    monkeypatch.setattr(cli, "_request_reindex", admit)
    try:
        rc = dispatch(cli, [command, "--json"])
    except (OSError, ValueError, TypeError, RuntimeError) as exc:
        pytest.fail("Vault exception escaped JSON dispatcher: " + type(exc).__name__)
    output = capsys.readouterr().out
    payload = json.loads(output)
    assert sentinel not in output and "zvec_nonexistent_user_726894" not in output
    assert payload["ok"] is False
    assert payload["checked"] is False, "Unknown vault is not a complete health check"
    assert payload["action"]["status"] == "failed"
    assert payload["action"]["error"] == exception_name
    assert rc == 1, "An unresolved invocation is not an informational health warning"
    if command == "reindex":
        assert payload["action"]["completed"] is False
    assert requests == [], "No actual vault established: no admission is allowed"
    assert not (home / "zvec-memory" / cli.REINDEX_REQUEST).exists()
    assert config_path.read_bytes() == original_config


def selected_home(tmp_path, monkeypatch, native):
    home = tmp_path / "selected-home"
    path = home / "zvec-memory" / "config.json"
    path.parent.mkdir(parents=True)
    path.write_text(native)
    monkeypatch.setenv("HERMES_HOME", str(home))
    return home, path


def prohibit_unknown_work(cli, monkeypatch):
    for name in ("collect_checks", "_default_runner", "_task_ceiling", "_request_reindex"):
        monkeypatch.setattr(cli, name, lambda *a, **k: pytest.fail(
            "Unknown selection must not inspect or admit any target"))


def failed_document(cli, capsys, argv, exception_name):
    rc = None
    try:
        rc = dispatch(cli, argv)
    except (OSError, ValueError, TypeError, RuntimeError) as exc:
        pytest.fail("Selection exception escaped JSON dispatcher: " + type(exc).__name__)
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert captured.err == ""
    assert "opaque-private" not in captured.out
    assert rc == 1
    assert payload["ok"] is False and payload["checked"] is False
    assert payload["checks"] == []
    assert payload["action"]["status"] == "failed"
    assert payload["action"]["error"] == exception_name
    if argv[0] == "reindex":
        assert payload["action"]["completed"] is False
    return payload


@pytest.mark.parametrize("command", ["doctor", "status", "reindex"])
@pytest.mark.parametrize("bad_config", ["nul", "expanduser", "resolve"])
@pytest.mark.parametrize("override_kind", ["absolute", "relative", "tilde"])
def test_valid_override_bypasses_bad_configured_path_with_real_admission(
        tmp_path, monkeypatch, capsys, command, bad_config, override_kind):
    from test_cli import healthy_vault
    cli = load_cli()
    bad = {"nul": "opaque-private\x00", "expanduser": "~zvec_nonexistent_user_726894/x",
           "resolve": str(tmp_path / "opaque-private-denied")}[bad_config]
    home, path = selected_home(tmp_path, monkeypatch, json.dumps({"vault": bad, "zg_bin": "/zg"}))
    before = path.read_bytes()
    if bad_config == "resolve":
        original = Path.resolve
        def resolve(p, *a, **k):
            if str(p) == bad:
                raise PermissionError("opaque-private")
            return original(p, *a, **k)
        monkeypatch.setattr(Path, "resolve", resolve)
    vault = healthy_vault(tmp_path / "actual")
    cwd = tmp_path / "actual"
    monkeypatch.chdir(cwd)
    monkeypatch.setenv("HOME", str(cwd))
    override = {"absolute": str(vault), "relative": "vault", "tilde": "~/vault"}[override_kind]
    calls = []
    def run(argv, **kwargs):
        calls.append(argv)
        assert argv[0] != "systemctl", "Custom home cannot inspect the default service"
        if "status" in argv:
            assert Path(argv[2]).resolve() == vault.resolve()
        return 1, "", "not ready"
    monkeypatch.setattr(cli, "_default_runner", run)
    monkeypatch.setattr(cli, "_task_ceiling", lambda: 128)
    settings = cli._plugin_module("settings")
    actual_load = settings.load_settings
    reads = []
    def load(h):
        reads.append(h)
        return actual_load(h)
    monkeypatch.setattr(settings, "load_settings", load)
    rc = dispatch(cli, [command, "--json", "--vault", override])
    output = capsys.readouterr().out
    payload = json.loads(output)
    assert "opaque-private" not in output
    assert payload["checked"] is True and payload["ok"] is False
    assert next(c for c in payload["checks"] if c["name"] == "config")["ok"] is True
    assert next(c for c in payload["checks"] if c["name"] == "vault")["ok"] is True
    assert rc == (1 if command == "doctor" else 0)
    assert reads == [home]
    assert len(calls) == 2
    if command == "reindex":
        assert payload["action"]["status"] == "requested"
        assert payload["action"]["completed"] is False
        marker = vault / cli.REINDEX_REQUEST
        assert Path(payload["action"]["request"]).resolve() == marker.resolve()
        assert json.loads(marker.read_text())["by"] == "hermes zvec-memory reindex"
        assert marker.stat().st_mode & 0o777 == 0o600
    assert not (home / "zvec-memory" / cli.REINDEX_REQUEST).exists()
    assert path.read_bytes() == before


@pytest.mark.parametrize("command", ["doctor", "status", "reindex"])
@pytest.mark.parametrize("exception", [OSError, ValueError, TypeError, RuntimeError])
@pytest.mark.parametrize("target", ["home-with-override", "configured", "override"])
def test_typed_path_resolution_fault_never_admits_an_unknown_target(
        tmp_path, monkeypatch, capsys, command, exception, target):
    cli = load_cli()
    configured = tmp_path / "configured-vault"
    explicit = tmp_path / "explicit-vault"
    home, path = selected_home(tmp_path, monkeypatch, json.dumps({"vault": str(configured)}))
    before = path.read_bytes()
    bad = home if target.startswith("home") else (configured if target == "configured" else explicit)
    original = Path.resolve
    def resolve(p, *a, **k):
        if p == bad:
            raise exception("opaque-private-path-resolution")
        return original(p, *a, **k)
    monkeypatch.setattr(Path, "resolve", resolve)
    prohibit_unknown_work(cli, monkeypatch)
    argv = [command, "--json"]
    if target in ("home-with-override", "override"):
        argv += ["--vault", str(explicit)]
    failed_document(cli, capsys, argv, exception.__name__)
    assert path.read_bytes() == before
    assert not any((v / cli.REINDEX_REQUEST).exists() for v in (configured, explicit, home / "zvec-memory"))


@pytest.mark.parametrize("command", ["doctor", "status", "reindex", "engine", "migrate-config"])
@pytest.mark.parametrize("exception", [OSError, ValueError, TypeError, RuntimeError])
def test_canonical_home_helper_fault_is_single_class_only_json(
        tmp_path, monkeypatch, capsys, command, exception):
    import hermes_constants
    cli = load_cli()
    home, path = selected_home(tmp_path, monkeypatch, "{}")
    before = path.read_bytes()
    def fail_home():
        raise exception("opaque-private-canonical-home")
    monkeypatch.setattr(hermes_constants, "get_hermes_home", fail_home)
    prohibit_unknown_work(cli, monkeypatch)
    monkeypatch.setattr(cli, "_engine_module", lambda: pytest.fail("Unknown home cannot install"))
    argv = [command, "--json"]
    if command == "engine":
        argv = [command, "install", "--json"]
    elif command in ("doctor", "status", "reindex"):
        argv += ["--vault", str(tmp_path / "actual-vault")]
    if command in ("engine", "migrate-config"):
        rc = None
        try:
            rc = dispatch(cli, argv)
        except (OSError, ValueError, TypeError, RuntimeError) as exc:
            pytest.fail("Home exception escaped JSON dispatcher: " + type(exc).__name__)
        captured = capsys.readouterr()
        payload = json.loads(captured.out)
        assert captured.err == "" and "opaque-private" not in captured.out
        assert rc == 1 and payload["action"]["status"] == "failed"
        assert payload["error"] == exception.__name__
        assert payload["health"]["status"] == "not_checked"
    else:
        failed_document(cli, capsys, argv, exception.__name__)
    assert path.read_bytes() == before


@pytest.mark.parametrize("command", ["doctor", "status", "reindex"])
@pytest.mark.parametrize("fault", ["nul", "expanduser"])
def test_invalid_explicit_override_is_not_a_default_selection(
        tmp_path, monkeypatch, capsys, command, fault):
    cli = load_cli()
    home, path = selected_home(tmp_path, monkeypatch, "{}")
    before = path.read_bytes()
    override = "opaque-private\x00" if fault == "nul" else "~zvec_nonexistent_user_726894/opaque-private"
    prohibit_unknown_work(cli, monkeypatch)
    failed_document(cli, capsys, [command, "--json", "--vault", override],
                    "ValueError" if fault == "nul" else "RuntimeError")
    assert path.read_bytes() == before
    assert not (home / "zvec-memory" / cli.REINDEX_REQUEST).exists()


@pytest.mark.parametrize("command", ["migrate-config"])
@pytest.mark.parametrize("exception", [OSError, ValueError, TypeError, RuntimeError])
def test_migration_refuses_unknown_resolved_home(
        tmp_path, monkeypatch, capsys, command, exception):
    cli = load_cli()
    home, path = selected_home(tmp_path, monkeypatch, "{}")
    before = path.read_bytes()
    original = Path.resolve
    def resolve(p, *a, **k):
        if p == home:
            raise exception("opaque-private-home-resolution")
        return original(p, *a, **k)
    monkeypatch.setattr(Path, "resolve", resolve)
    argv = [command, "--json"]
    assert dispatch(cli, argv) == 1
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert captured.err == "" and "opaque-private" not in captured.out
    assert payload["action"]["status"] == "failed"
    assert payload["error"] == exception.__name__
    assert payload["health"]["status"] == "not_checked"
    assert path.read_bytes() == before


@pytest.mark.parametrize("command", ["doctor", "status", "reindex"])
@pytest.mark.parametrize("home_kind", ["explicit", "relative", "tilde", "variable", "default"])
def test_valid_canonical_home_forms_preserve_configured_home_relative_vault(
        tmp_path, monkeypatch, capsys, command, home_kind):
    cli = load_cli()
    user = tmp_path / "user"
    home = user / ".hermes"
    home.mkdir(parents=True)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HOME", str(user))
    if home_kind == "default":
        monkeypatch.delenv("HERMES_HOME")
    else:
        monkeypatch.setenv("TEST_CANONICAL_HOME", str(home))
        raw = {"explicit": str(home), "relative": "user/.hermes", "tilde": "~/.hermes",
               "variable": "${TEST_CANONICAL_HOME}"}[home_kind]
        monkeypatch.setenv("HERMES_HOME", raw)
    path = home / "zvec-memory/config.json"
    path.parent.mkdir()
    path.write_text(json.dumps({"vault": "vault", "zg_bin": "/zg"}))
    before = path.read_bytes()
    vault = home / "vault"
    (vault / "facts").mkdir(parents=True)
    (vault / "sessions").mkdir()
    calls = []
    def run(argv, **kwargs):
        calls.append(argv)
        assert argv[0] != "systemctl"
        if "status" in argv:
            assert Path(argv[2]) == vault
        return 1, "", "not ready"
    monkeypatch.setattr(cli, "_default_runner", run)
    monkeypatch.setattr(cli, "_task_ceiling", lambda: 128)
    assert cli._hermes_home().resolve() == home
    assert dispatch(cli, [command, "--json"]) == (1 if command == "doctor" else 0)
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert captured.err == ""
    assert payload["checked"] is True and payload["ok"] is False
    assert len(calls) == 2
    if command == "reindex":
        assert payload["action"]["status"] == "requested"
        assert payload["action"]["completed"] is False
        assert (vault / cli.REINDEX_REQUEST).is_file()
    assert not (home / "zvec-memory" / cli.REINDEX_REQUEST).exists()
    assert path.read_bytes() == before


@pytest.mark.parametrize("command", ["doctor", "status", "reindex"])
@pytest.mark.parametrize("kind", ["default", "blank", "relative", "absolute", "tilde", "variable", "braced"])
def test_configured_vault_valid_forms_keep_their_existing_resolution_semantics(
        tmp_path, monkeypatch, capsys, command, kind):
    cli = load_cli()
    home, path = selected_home(tmp_path, monkeypatch, "{}")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path))
    vault = home / "zvec-memory" if kind in ("default", "blank") else home / "vault"
    if kind == "tilde":
        vault = tmp_path / "vault"
    raw = {"default": None, "blank": "  ", "relative": "vault", "absolute": str(vault),
           "tilde": "~/vault", "variable": "$HERMES_HOME/vault", "braced": "${HERMES_HOME}/vault"}[kind]
    path.write_text(json.dumps({"vault": raw, "zg_bin": "/zg"}))
    before = path.read_bytes()
    (vault / "facts").mkdir(parents=True)
    (vault / "sessions").mkdir()
    calls = []
    def run(argv, **kwargs):
        calls.append(argv)
        assert argv[0] != "systemctl"
        if "status" in argv:
            assert Path(argv[2]) == vault
        return 1, "", "not ready"
    monkeypatch.setattr(cli, "_default_runner", run)
    monkeypatch.setattr(cli, "_task_ceiling", lambda: 128)
    assert dispatch(cli, [command, "--json"]) == (1 if command == "doctor" else 0)
    payload = json.loads(capsys.readouterr().out)
    assert payload["checked"] is True and payload["ok"] is False
    assert len(calls) == 2
    if command == "reindex":
        assert payload["action"]["status"] == "requested"
        assert payload["action"]["completed"] is False
        assert (vault / cli.REINDEX_REQUEST).is_file()
    assert path.read_bytes() == before


@pytest.mark.parametrize("command", ["doctor", "status", "reindex"])
@pytest.mark.parametrize("native", ["opaque-private-invalid-json", "[]", "{}", '{"context_chars":0}', None])
def test_override_does_not_replace_native_authority_with_legacy_settings(
        tmp_path, monkeypatch, capsys, command, native):
    cli = load_cli()
    home, path = selected_home(tmp_path, monkeypatch, native or "{}")
    actual = tmp_path / "actual-vault"
    (actual / "facts").mkdir(parents=True)
    (actual / "sessions").mkdir()
    legacy_vault = tmp_path / "legacy-vault"
    legacy = home / "config.yaml"
    legacy.write_text(json.dumps({"plugins": {"zvec-memory": {
        "vault": str(legacy_vault), "zg_bin": "/legacy-zg"}}}))
    if native is None:
        path.unlink()
    before = path.read_bytes() if path.exists() else None
    before_legacy = legacy.read_bytes()
    calls = []
    def run(argv, **kwargs):
        calls.append(argv)
        assert native is None and argv[0] == "/legacy-zg"
        if "status" in argv:
            assert Path(argv[2]).resolve() == actual
        return 1, "", "not ready"
    monkeypatch.setattr(cli, "_default_runner", run)
    monkeypatch.setattr(cli, "_task_ceiling", lambda: 128)
    assert dispatch(cli, [command, "--json", "--vault", str(actual)]) == (1 if command == "doctor" else 0)
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert captured.err == "" and "opaque-private" not in captured.out
    assert payload["checked"] is True and payload["ok"] is False
    config_check = next(c for c in payload["checks"] if c["name"] == "config")
    assert config_check["ok"] is (native is None or native == '{"context_chars":0}')
    if native in ("opaque-private-invalid-json", "[]"):
        assert config_check["detail"] == "unreadable: " + ("ValueError" if native == "[]" else "JSONDecodeError")
    assert len(calls) == (2 if native is None else 0)
    if command == "reindex":
        assert payload["action"]["status"] == "requested"
        assert payload["action"]["completed"] is False
        assert json.loads((actual / cli.REINDEX_REQUEST).read_text())["by"] == "hermes zvec-memory reindex"
    assert not (legacy_vault / cli.REINDEX_REQUEST).exists()
    assert not (home / "zvec-memory" / cli.REINDEX_REQUEST).exists()
    assert (path.read_bytes() if path.exists() else None) == before
    assert legacy.read_bytes() == before_legacy


@pytest.mark.parametrize("command", ["doctor", "status", "reindex"])
def test_actual_nul_context_home_with_valid_override_cannot_borrow_default_profile(
        tmp_path, monkeypatch, capsys, command):
    import hermes_constants
    cli = load_cli()
    home, path = selected_home(tmp_path, monkeypatch, "{}")
    before = path.read_bytes()
    # OS environments reject NUL before CLI dispatch; the canonical context
    # override accepts it, exercising the actual cold helper instead.
    token = hermes_constants.set_hermes_home_override(str(home) + "\x00opaque-private")
    try:
        prohibit_unknown_work(cli, monkeypatch)
        failed_document(cli, capsys, [command, "--json", "--vault", str(tmp_path / "explicit")], "ValueError")
    finally:
        hermes_constants.reset_hermes_home_override(token)
    assert path.read_bytes() == before


@pytest.mark.parametrize("command,target", [
    (command, target) for command in ("doctor", "status", "reindex")
    for target in ("home", "configured", "override")
] + [("migrate-config", "home")])
def test_selection_assertion_errors_are_real_logic_errors_not_public_failures(
        tmp_path, monkeypatch, capsys, command, target):
    cli = load_cli()
    configured, explicit = tmp_path / "configured", tmp_path / "explicit"
    home, path = selected_home(tmp_path, monkeypatch, json.dumps({"vault": str(configured)}))
    before = path.read_bytes()
    original = Path.resolve
    bad = {"home": home, "configured": configured, "override": explicit}[target]
    # Migration selects only the home, not a vault: test its actual boundary.
    if command == "migrate-config":
        bad = home
    def resolve(p, *a, **k):
        if p == bad:
            raise AssertionError("opaque-private-logic-control")
        return original(p, *a, **k)
    monkeypatch.setattr(Path, "resolve", resolve)
    prohibit_unknown_work(cli, monkeypatch)
    argv = [command, "--json"]
    if target in ("home", "override") and command != "migrate-config":
        argv += ["--vault", str(explicit)]
    with pytest.raises(AssertionError, match="opaque-private-logic-control"):
        dispatch(cli, argv)
    assert capsys.readouterr().out == ""
    assert path.read_bytes() == before


def test_service_selection_type_error_preserves_unknown_tasks_without_caller_fallback(
        tmp_path, monkeypatch):
    from test_cli import healthy_vault
    cli = load_cli()
    home, _ = selected_home(tmp_path, monkeypatch, "{}")
    original = Path.resolve
    def resolve(p, *a, **k):
        if p == home:
            raise TypeError("opaque-private-service-selection")
        return original(p, *a, **k)
    monkeypatch.setattr(Path, "resolve", resolve)
    calls = []
    def run(argv):
        calls.append(argv)
        assert argv[0] != "systemctl"
        return 1, "", "not ready"
    monkeypatch.setattr(cli, "_task_ceiling", lambda: pytest.fail("Unknown service cannot borrow caller"))
    checks = cli.collect_checks(healthy_vault(tmp_path), {"zg_bin": "/zg"}, runner=run)
    service = next(c for c in checks if c["name"] == "service")
    tasks = next(c for c in checks if c["name"] == "tasks")
    assert service["ok"] is False and service["detail"] == "unreadable service selection: TypeError"
    assert tasks["ok"] is False and "unknown task target" in tasks["detail"]
    assert len(calls) == 2
