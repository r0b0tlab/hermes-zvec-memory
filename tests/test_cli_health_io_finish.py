"""Known-vault health I/O and real legacy-YAML dispatcher boundaries.

Health errors are scoped synthetic PermissionErrors at actual pathlib seams,
not claims of kernel EACCES under namespace root. YAML errors are real parsing.
"""
import json
from pathlib import Path

import pytest

from test_cli import load_cli
from test_cli_health_finish import dispatch


@pytest.mark.parametrize("command", ["doctor", "status", "reindex"])
@pytest.mark.parametrize("leaf,operation", [("facts", "stat"), ("sessions", "stat"),
    (".mirror-delivery-failed.json", "stat"), (".zvec-grep/manifest.json", "stat"),
    ("facts", "glob"), ("sessions", "glob")])
def test_known_vault_health_io_error_is_redacted_and_does_not_cancel_real_admission(
        tmp_path, monkeypatch, capsys, command, leaf, operation):
    cli = load_cli()
    home, vault = tmp_path / "selected-home", tmp_path / "selected-vault"
    (home / "zvec-memory").mkdir(parents=True)
    (vault / "facts").mkdir(parents=True)
    (vault / "sessions").mkdir()
    config = home / "zvec-memory/config.json"
    config.write_text(json.dumps({"vault": str(vault), "zg_bin": "/fixture-engine"}))
    before = config.read_bytes()
    monkeypatch.setenv("HERMES_HOME", str(home))
    sentinel = "opaque-private-health-io-diagnostic"
    target = vault / leaf
    original = getattr(Path, operation)
    observations = []
    def fault(path, *args, **kwargs):
        if path == target:
            observations.append(str(path))
            raise PermissionError(sentinel)
        return original(path, *args, **kwargs)
    monkeypatch.setattr(Path, operation, fault)
    calls = []
    def run(argv, **kwargs):
        calls.append(argv)
        assert argv[0] != "systemctl", "Custom profile must not inspect the default unit"
        return 1, "", "fixture unavailable"
    monkeypatch.setattr(cli, "_default_runner", run)
    monkeypatch.setattr(cli, "_task_ceiling", lambda: 256)
    actual_request = cli._request_reindex
    admitted = []
    def request(path):
        admitted.append(path.resolve())
        return actual_request(path)
    monkeypatch.setattr(cli, "_request_reindex", request)
    rc = None
    try:
        rc = dispatch(cli, [command, "--json"])
    except OSError as exc:
        pytest.fail("Known-vault health exception escaped dispatcher: " + type(exc).__name__)
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert captured.err == "" and sentinel not in captured.out
    assert observations, "The real health leaf fault must be reached"
    assert payload["ok"] is False
    assert "PermissionError" in captured.out, "Unreadable health must be reported honestly"
    assert rc == (1 if command == "doctor" else 0)
    if command == "reindex":
        assert admitted == [vault.resolve()]
        assert payload["action"]["status"] == "requested"
        assert payload["action"]["completed"] is False
        marker = vault / cli.REINDEX_REQUEST
        assert marker.stat().st_mode & 0o777 == 0o600
        assert json.loads(marker.read_text())["by"] == "hermes zvec-memory reindex"
        assert Path(payload["action"]["request"]).resolve() == marker.resolve()
    else:
        assert admitted == [] and not (vault / cli.REINDEX_REQUEST).exists()
    assert not (home / "zvec-memory" / cli.REINDEX_REQUEST).exists()
    assert config.read_bytes() == before


@pytest.mark.parametrize("malformed", ["parser", "scanner"])
def test_real_malformed_legacy_yaml_migration_is_private_json_without_publication(
        tmp_path, monkeypatch, capsys, malformed):
    import yaml
    cli = load_cli()
    home = tmp_path / "selected-home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    sentinel = "opaque-private-legacy-parser-source"
    text = ("plugins: [" + sentinel + "\n" if malformed == "parser" else
            "plugins:\n\tzvec-memory: " + sentinel + "\n")
    legacy = home / "config.yaml"
    legacy.write_text(text)
    before = legacy.read_bytes()
    native = home / "zvec-memory/config.json"
    assert not native.exists()
    monkeypatch.setattr(cli, "_default_runner", lambda *a, **k: pytest.fail("Migration must be cold"))
    monkeypatch.setattr(cli, "_request_reindex", lambda *a, **k: pytest.fail("Migration is not admission"))
    rc = None
    try:
        rc = dispatch(cli, ["migrate-config", "--json"])
    except yaml.YAMLError as exc:
        pytest.fail("Legacy YAML exception escaped migration: " + type(exc).__name__)
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert captured.err == "" and sentinel not in captured.out
    assert rc == 1 and payload["status"] == "failed"
    assert payload["action"]["status"] == "failed"
    assert payload["health"]["status"] == "not_checked"
    assert payload["error"] == ("ParserError" if malformed == "parser" else "ScannerError")
    assert legacy.read_bytes() == before and not native.exists()
    assert not list(home.rglob("*.new"))


# Append-only controls: the independent parent's 20-case prefix above is frozen.
def selected_fixture(tmp_path, monkeypatch):
    cli = load_cli()
    home, vault = tmp_path / "home", tmp_path / "vault"
    (home / "zvec-memory").mkdir(parents=True)
    (vault / "facts").mkdir(parents=True)
    (vault / "sessions").mkdir()
    native = home / "zvec-memory/config.json"
    native.write_text(json.dumps({"vault": str(vault), "zg_bin": "/fixture-engine"}))
    monkeypatch.setenv("HERMES_HOME", str(home))
    return cli, home, vault, native


@pytest.mark.parametrize("command", ["doctor", "status", "reindex"])
@pytest.mark.parametrize("selection", ["configured", "override"])
@pytest.mark.parametrize("error_type", [OSError, BlockingIOError, IsADirectoryError])
@pytest.mark.parametrize("leaf,operation", [("facts", "stat"), ("sessions", "stat"),
    (".mirror-delivery-failed.json", "stat"), (".zvec-grep/manifest.json", "stat"),
    ("facts", "glob"), ("sessions", "glob")])
def test_typed_health_error_preserves_selected_target_and_incomplete_observation(
        tmp_path, monkeypatch, capsys, command, selection, error_type, leaf, operation):
    cli, home, vault, native = selected_fixture(tmp_path, monkeypatch)
    before = native.read_bytes()
    wrong = tmp_path / "wrong-legacy-vault"
    legacy = home / "config.yaml"
    legacy.write_text(json.dumps({"plugins": {"zvec-memory": {"vault": str(wrong)}}}))
    legacy_before = legacy.read_bytes()
    if selection == "override":
        native.write_text(json.dumps({"vault": str(home / "configured-not-selected")}))
        before = native.read_bytes()
    original = getattr(Path, operation)
    reached = []
    def fault(path, *args, **kwargs):
        if path == vault / leaf:
            reached.append(True)
            raise error_type("private-health-diagnostic")
        return original(path, *args, **kwargs)
    monkeypatch.setattr(Path, operation, fault)
    monkeypatch.setattr(cli, "_default_runner", lambda *a, **k: pytest.fail("Incomplete health must not claim later probes"))
    argv = [command, "--json"] + (["--vault", str(vault)] if selection == "override" else [])
    try:
        rc = dispatch(cli, argv)
    except OSError as exc:
        pytest.fail("Typed health I/O escaped: " + type(exc).__name__)
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert captured.err == "" and "private-health-diagnostic" not in captured.out
    assert reached and payload["ok"] is False and payload["checked"] is False
    assert payload["checks"] == [{"name": "health", "ok": False,
                                  "detail": "unreadable: " + error_type.__name__}]
    assert payload["failures"] == ["health"]
    assert rc == (1 if command == "doctor" else 0)
    marker = vault / cli.REINDEX_REQUEST
    if command == "reindex":
        assert payload["action"]["status"] == "requested"
        assert payload["action"]["completed"] is False
        assert Path(payload["action"]["request"]) == marker
        assert marker.stat().st_mode & 0o777 == 0o600
        assert json.loads(marker.read_text())["by"] == "hermes zvec-memory reindex"
    else:
        assert payload["action"]["status"] == "checked" and not marker.exists()
    assert not (home / "zvec-memory" / cli.REINDEX_REQUEST).exists()
    assert not (home / "configured-not-selected" / cli.REINDEX_REQUEST).exists()
    assert not wrong.exists()
    assert native.read_bytes() == before and legacy.read_bytes() == legacy_before


@pytest.mark.parametrize("command", ["doctor", "status", "reindex"])
@pytest.mark.parametrize("seam", ["stat", "glob", "collect_checks"])
def test_new_health_boundary_does_not_swallow_logic_assertions(
        tmp_path, monkeypatch, capsys, command, seam):
    cli, home, vault, native = selected_fixture(tmp_path, monkeypatch)
    before = native.read_bytes()
    def logic(*args, **kwargs):
        raise AssertionError("intentional logic control")
    if seam == "collect_checks":
        monkeypatch.setattr(cli, seam, logic)
    else:
        original = getattr(Path, seam)
        def fault(path, *args, **kwargs):
            if path == vault / "facts":
                return logic()
            return original(path, *args, **kwargs)
        monkeypatch.setattr(Path, seam, fault)
    with pytest.raises(AssertionError, match="intentional logic control"):
        dispatch(cli, [command, "--json"])
    captured = capsys.readouterr()
    assert captured.out == captured.err == ""
    assert native.read_bytes() == before and not (vault / cli.REINDEX_REQUEST).exists()


@pytest.mark.parametrize("leaf,check_name", [(".mirror-inbox.sqlite3", "inbox"),
    (".mirror-map.json", "mirror"), (".zvec-grep/.zvec-memory-state.json", "identity")])
@pytest.mark.parametrize("condition", ["absent", "unreadable"])
def test_existing_durable_health_error_is_not_reported_as_absence(
        tmp_path, monkeypatch, capsys, leaf, check_name, condition):
    from test_cli import fake_runner
    cli, home, vault, native = selected_fixture(tmp_path, monkeypatch)
    original = Path.stat if check_name != "identity" else Path.read_text
    operation = "stat" if check_name != "identity" else "read_text"
    reached = []
    def fault(path, *args, **kwargs):
        if path == vault / leaf and condition == "unreadable":
            reached.append(True)
            raise PermissionError("private-durable-diagnostic")
        return original(path, *args, **kwargs)
    monkeypatch.setattr(Path, operation, fault)
    monkeypatch.setattr(cli, "_default_runner", fake_runner())
    monkeypatch.setattr(cli, "_task_ceiling", lambda: 512)
    assert dispatch(cli, ["status", "--json"]) == 0
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    check = next(c for c in payload["checks"] if c["name"] == check_name)
    assert check["ok"] is (condition == "absent")
    assert payload["checked"] is True
    if condition == "unreadable":
        assert reached and "PermissionError" in check["detail"] and payload["ok"] is False
    assert captured.err == "" and "private-durable-diagnostic" not in captured.out


@pytest.mark.parametrize("text,error_name", [
    ("plugins: {zvec-memory: [private-source\n", "ParserError"),
    ("plugins:\n  zvec-memory: @private-source\n", "ScannerError"),
    ("plugins: *private-source\n", "ComposerError"),
    ("plugins: private-source\x00\n", "ReaderError")])
def test_migration_handles_real_yaml_domain_family_without_source_leak(
        tmp_path, monkeypatch, capsys, text, error_name):
    import yaml
    cli = load_cli()
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    legacy = home / "config.yaml"
    legacy.write_text(text)
    before = legacy.read_bytes()
    real_module = cli._plugin_module
    loaded = []
    def cold_module(name):
        assert name in ("settings", "hostio"), "Migration must not initialize provider/native/service helpers"
        loaded.append(name)
        return real_module(name)
    monkeypatch.setattr(cli, "_plugin_module", cold_module)
    monkeypatch.setattr(cli, "collect_checks", lambda *a: pytest.fail("Migration must not inspect health"))
    try:
        rc = dispatch(cli, ["migrate-config", "--json"])
    except yaml.YAMLError as exc:
        pytest.fail("Real YAML domain error escaped: " + type(exc).__name__)
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert captured.err == "" and "private-source" not in captured.out
    assert rc == 1 and payload["error"] == error_name
    assert payload["status"] == payload["action"]["status"] == "failed"
    assert payload["health"] == {"status": "not_checked"}
    assert loaded == ["settings"]
    assert legacy.read_bytes() == before and not (home / "zvec-memory").exists()


@pytest.mark.parametrize("native_text", ["{}", '{"auto_extract":false,"context_chars":0,"unknown":[1,"keep"]}',
    "[]", "null", "not-json"])
@pytest.mark.parametrize("malformed", ["plugins: [private-source\n", "plugins:\n\tprivate-source\n"])
def test_migration_present_native_authority_never_parses_bad_legacy(
        tmp_path, monkeypatch, capsys, native_text, malformed):
    import yaml
    cli, home, vault, native = selected_fixture(tmp_path, monkeypatch)
    native.write_text(native_text)
    legacy = home / "config.yaml"
    legacy.write_text(malformed)
    before_native, before_legacy = native.read_bytes(), legacy.read_bytes()
    def no_legacy(*a, **k):
        pytest.fail("Present native JSON must not parse legacy YAML")
    monkeypatch.setattr(yaml, "safe_load", no_legacy)
    valid = native_text.startswith("{")
    assert dispatch(cli, ["migrate-config", "--json"]) == (0 if valid else 1)
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert captured.err == "" and "private-source" not in captured.out
    assert payload["status"] == ("current" if valid else "failed")
    assert payload["health"] == {"status": "not_checked"}
    assert native.read_bytes() == before_native and legacy.read_bytes() == before_legacy
    assert sorted(p.name for p in native.parent.iterdir()) == ["config.json"]


def test_valid_legacy_yaml_migrates_losslessly_without_provider_or_health(
        tmp_path, monkeypatch, capsys):
    cli = load_cli()
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    legacy = home / "config.yaml"
    legacy.write_text("unrelated: keep\nplugins:\n  zvec-memory:\n    auto_extract: false\n"
                      "    context_chars: 0\n    unknown: [1, keep]\n")
    before = legacy.read_bytes()
    real_module = cli._plugin_module
    def cold_module(name):
        assert name in ("settings", "hostio")
        return real_module(name)
    monkeypatch.setattr(cli, "_plugin_module", cold_module)
    monkeypatch.setattr(cli, "collect_checks", lambda *a: pytest.fail("No health in migration"))
    assert dispatch(cli, ["migrate-config", "--json"]) == 0
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    native = home / "zvec-memory/config.json"
    assert payload["status"] == "migrated" and payload["health"] == {"status": "not_checked"}
    assert json.loads(native.read_text()) == {"auto_extract": False, "context_chars": 0, "unknown": [1, "keep"]}
    assert native.stat().st_mode & 0o777 == 0o600 and legacy.read_bytes() == before
    assert captured.err == ""


@pytest.mark.parametrize("seam", ["settings", "yaml-parser"])
def test_new_yaml_boundary_does_not_swallow_logic_assertions(
        tmp_path, monkeypatch, capsys, seam):
    import yaml
    cli = load_cli()
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    legacy = home / "config.yaml"
    legacy.write_text("plugins: {}\n")
    before = legacy.read_bytes()
    def logic(*args, **kwargs):
        raise AssertionError("intentional migration logic control")
    if seam == "settings":
        monkeypatch.setattr(cli._plugin_module("settings"), "load_settings", logic)
    else:
        monkeypatch.setattr(yaml, "safe_load", logic)
    with pytest.raises(AssertionError, match="intentional migration logic control"):
        dispatch(cli, ["migrate-config", "--json"])
    captured = capsys.readouterr()
    assert captured.out == captured.err == ""
    assert legacy.read_bytes() == before and not (home / "zvec-memory").exists()
