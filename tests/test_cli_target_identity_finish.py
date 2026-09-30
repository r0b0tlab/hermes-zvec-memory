"""Unknown settings cannot establish a no-override rebuild destination.

Read denial is a scoped synthetic PermissionError at the real settings seam,
not kernel EACCES under the isolated namespace root. Parsing faults are real.
"""
import json
from pathlib import Path

import pytest

from test_cli import load_cli
from test_cli_health_finish import dispatch


@pytest.mark.parametrize("fault", ["native-read", "native-json", "native-array", "legacy-yaml"])
def test_unknown_settings_without_override_cannot_admit_default_rebuild(
        tmp_path, monkeypatch, capsys, fault):
    cli = load_cli()
    home = tmp_path / "selected-home"
    default = home / "zvec-memory"
    external = tmp_path / "actual-external-vault"
    legacy_vault = tmp_path / "legacy-vault"
    default.mkdir(parents=True)
    external.mkdir()
    legacy_vault.mkdir()
    native = default / "config.json"
    legacy = home / "config.yaml"
    sentinel = "opaque-private-unknown-settings-source"
    legacy.write_text(json.dumps({"plugins": {"zvec-memory": {
        "vault": str(legacy_vault), "zg_bin": "/legacy-engine"}}}))
    if fault == "native-read":
        native.write_text(json.dumps({"vault": str(external), "zg_bin": "/selected-engine"}))
    elif fault == "native-json":
        native.write_text(sentinel)
    elif fault == "native-array":
        native.write_text("[]")
    else:
        legacy.write_text("plugins: [" + sentinel + "\n")
    before_native = native.read_bytes() if native.exists() else None
    before_legacy = legacy.read_bytes()
    monkeypatch.setenv("HERMES_HOME", str(home))
    real_read = Path.read_text
    faults_seen = []
    def read(path, *args, **kwargs):
        if fault == "native-read" and path == native:
            faults_seen.append(str(path))
            raise PermissionError(sentinel)
        return real_read(path, *args, **kwargs)
    monkeypatch.setattr(Path, "read_text", read)
    runner_calls = []
    def unavailable(argv, **kwargs):
        runner_calls.append(argv)
        return 1, "", "isolated fixture unavailable"
    monkeypatch.setattr(cli, "_default_runner", unavailable)
    monkeypatch.setattr(cli, "_task_ceiling", lambda: 256)
    actual_request = cli._request_reindex
    admitted = []
    def request(vault):
        admitted.append(vault.resolve())
        return actual_request(vault)
    monkeypatch.setattr(cli, "_request_reindex", request)
    rc = dispatch(cli, ["reindex", "--json"])
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert captured.err == "" and sentinel not in captured.out
    if fault == "native-read":
        assert faults_seen, "The actual settings read-denial seam must be reached"
    assert admitted == [], "Unknown configuration admitted rebuild to substitute default vault"
    assert rc == 1 and payload["action"]["status"] == "failed"
    assert payload["action"]["completed"] is False
    assert payload["ok"] is False and payload["checked"] is False
    assert "request" not in payload["action"]
    assert runner_calls == [], "Unknown target must not initiate runtime diagnostics"
    for vault in (default, external, legacy_vault):
        assert not (vault / cli.REINDEX_REQUEST).exists()
    assert (native.read_bytes() if native.exists() else None) == before_native
    assert legacy.read_bytes() == before_legacy
    assert not list(home.rglob("*.new"))


@pytest.mark.parametrize("raw", [None, False, 0], ids=["null", "false", "zero"])
def test_actual_provider_falsey_vault_matches_cli_default(tmp_path, raw):
    from test_provider import _mod

    cli = load_cli()
    home = tmp_path / "resolver-home"
    home.mkdir()
    config = {"vault": raw, "unknown": {"kept": [False, 0, None]}}
    provider = _mod.ZvecMemoryProvider(config=config)
    expected = (home / "zvec-memory").resolve()
    assert cli.resolve_vault(raw, home) == expected
    assert provider._resolve_vault(str(home)) == expected, "Actual provider disagrees with CLI default vault"
    assert provider._config == config and type(provider._config["vault"]) is type(raw)
    assert provider._vault is None and provider._disk_worker is None and provider._index_worker is None
    assert list(home.iterdir()) == []


VAULT_VALUES = [pytest.param("__missing__", id="missing"),
    pytest.param(None, id="null"), pytest.param(False, id="false"),
    pytest.param(0, id="zero"), pytest.param("", id="empty"),
    pytest.param("  ", id="blank"), pytest.param([], id="empty-list"),
    pytest.param({}, id="empty-object"), pytest.param(True, id="true"),
    pytest.param(1, id="one"), pytest.param(-1, id="negative"),
    pytest.param([1], id="list"), pytest.param({"x": 1}, id="object"),
    pytest.param("relative/vault", id="relative"),
    pytest.param("$HERMES_HOME/vault", id="dollar-home"),
    pytest.param("${HERMES_HOME}/vault", id="braced-home"),
    pytest.param("~/vault", id="tilde"), pytest.param("__absolute__", id="absolute")]


@pytest.mark.parametrize("raw", VAULT_VALUES)
@pytest.mark.parametrize("store", ["native", "legacy"])
@pytest.mark.parametrize("command", ["doctor", "status", "reindex"])
def test_real_dispatch_cold_settings_and_provider_select_identical_vault(
        tmp_path, monkeypatch, capsys, raw, store, command):
    import yaml
    from test_provider import _mod

    cli = load_cli()
    home = tmp_path / "settings-home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    native = home / "zvec-memory/config.json"
    legacy = home / "config.yaml"
    other = tmp_path / "different-legacy-vault"
    value = str(tmp_path / "absolute-vault") if raw == "__absolute__" else raw
    config = {"unknown": {"kept": [None, False, 0]}, "zg_bin": "/unavailable-fixture-engine"}
    if raw != "__missing__":
        config["vault"] = value
    if store == "native":
        native.parent.mkdir()
        native.write_text(json.dumps(config))
        legacy.write_text(yaml.safe_dump({"plugins": {"zvec-memory": {"vault": str(other)}}}))
    else:
        legacy.write_text(yaml.safe_dump({"unrelated": "keep", "plugins": {"zvec-memory": config}}))
    before = {p: p.read_bytes() if p.exists() else None for p in (native, legacy)}
    settings = cli._plugin_module("settings")
    readback = settings.load_settings(home)
    assert readback == config
    if "vault" in config:
        assert type(readback["vault"]) is type(value)
    provider = _mod.ZvecMemoryProvider()
    assert provider._config == config
    if "vault" in config:
        assert type(provider._config["vault"]) is type(value)
    selected = provider._resolve_vault(str(home))
    assert selected == cli.resolve_vault(config.get("vault"), home)
    if not config.get("vault") or (isinstance(value, str) and not value.strip()):
        assert selected == (home / "zvec-memory").resolve()
    provider_config = json.dumps(provider._config, sort_keys=True)
    def forbidden(*a, **k):
        pytest.fail("CLI must not initialize provider or native engine")
    monkeypatch.setattr(_mod.ZvecMemoryProvider, "initialize", forbidden)
    monkeypatch.setattr(_mod.ZvecMemoryProvider, "_run_zg", forbidden)
    monkeypatch.setattr(cli, "_default_runner", lambda *a, **k: (1, "", "fixture unavailable"))
    monkeypatch.setattr(cli, "_task_ceiling", lambda: 256)
    real_state, real_request = cli.vault_state, cli._request_reindex
    observed, admitted = [], []
    def state(vault):
        observed.append(vault.resolve())
        return real_state(vault)
    def request(vault):
        admitted.append(vault.resolve())
        return real_request(vault)
    monkeypatch.setattr(cli, "vault_state", state)
    monkeypatch.setattr(cli, "_request_reindex", request)
    rc = dispatch(cli, [command, "--json"])
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert captured.err == "" and payload["checked"] is True
    assert observed == [selected]
    assert admitted == ([selected] if command == "reindex" else [])
    assert rc == (1 if command == "doctor" else 0)
    if command == "reindex":
        marker = selected / cli.REINDEX_REQUEST
        assert payload["action"]["status"] == "requested" and payload["action"]["completed"] is False
        assert payload["action"]["request"] == str(marker)
        intent = json.loads(marker.read_bytes())
        assert intent["by"] == "hermes zvec-memory reindex" and "identity" not in intent
        assert marker.stat().st_mode & 0o777 == 0o600
    else:
        assert payload["action"]["status"] == "checked" and not (selected / cli.REINDEX_REQUEST).exists()
    assert not (other / cli.REINDEX_REQUEST).exists()
    for name in ("None", "False", "0"):
        assert not (home / name / cli.REINDEX_REQUEST).exists()
    assert {p: p.read_bytes() if p.exists() else None for p in (native, legacy)} == before
    assert settings.load_settings(home) == config
    assert json.dumps(provider._config, sort_keys=True) == provider_config
    assert provider._vault is None and provider._disk_worker is None and provider._index_worker is None


@pytest.mark.parametrize("source", ["native", "legacy"])
@pytest.mark.parametrize("error", [PermissionError, OSError, BlockingIOError, UnicodeError])
def test_unknown_read_refuses_before_any_address_or_health_step(
        tmp_path, monkeypatch, capsys, source, error):
    cli = load_cli()
    home = tmp_path / "unknown-home"
    native = home / "zvec-memory/config.json"
    native.parent.mkdir(parents=True)
    legacy = home / "config.yaml"
    target = tmp_path / "external"
    legacy.write_text(json.dumps({"plugins": {"zvec-memory": {"vault": str(target)}}}))
    if source == "native":
        native.write_text(json.dumps({"vault": str(target)}))
    selected_source = native if source == "native" else legacy
    before = {p:p.read_bytes() if p.exists() else None for p in (native, legacy)}
    monkeypatch.setenv("HERMES_HOME", str(home))
    import builtins
    real_read, real_open = Path.read_text, builtins.open
    seen = []
    def read(path, *a, **k):
        if source == "native" and path == selected_source:
            seen.append(path)
            raise error("opaque-private-read-fault")
        return real_read(path, *a, **k)
    def open_legacy(path, *a, **k):
        if source == "legacy" and Path(path) == selected_source:
            seen.append(Path(path))
            raise error("opaque-private-read-fault")
        return real_open(path, *a, **k)
    monkeypatch.setattr(Path, "read_text", read)
    monkeypatch.setattr(builtins, "open", open_legacy)
    def forbidden(*a, **k):
        pytest.fail("Unknown settings must not select an address, diagnose, or admit")
    for name in ("resolve_vault", "collect_checks", "vault_state", "_service_diagnostics",
                 "_task_ceiling", "_default_runner", "_request_reindex"):
        monkeypatch.setattr(cli, name, forbidden)
    assert dispatch(cli, ["reindex", "--json"]) == 1
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert seen == [selected_source] and captured.err == ""
    assert "opaque-private" not in captured.out
    assert payload["checked"] is False and payload["ok"] is False and payload["checks"] == []
    assert payload["action"] == {"name":"reindex", "status":"failed", "completed":False, "error":"ValueError"}
    assert {p:p.read_bytes() if p.exists() else None for p in (native, legacy)} == before
    assert not list(home.rglob(cli.REINDEX_REQUEST)) and not target.exists()


@pytest.mark.parametrize("text,error_name", [
    ("plugins: {zvec-memory: [private-source\n", "ParserError"),
    ("plugins:\n  zvec-memory: @private-source\n", "ScannerError"),
    ("plugins: *private-source\n", "ComposerError"),
    ("plugins: private-source\x00\n", "ReaderError")])
@pytest.mark.parametrize("override", [False, True])
def test_real_yaml_unknown_target_refuses_but_valid_override_admits(
        tmp_path, monkeypatch, capsys, text, error_name, override):
    cli = load_cli()
    home = tmp_path / "yaml-home"
    home.mkdir()
    legacy = home / "config.yaml"
    legacy.write_text(text)
    before = legacy.read_bytes()
    target = tmp_path / "explicit-target"
    monkeypatch.setenv("HERMES_HOME", str(home))
    admitted = []
    real_request = cli._request_reindex
    def request(vault):
        admitted.append(vault.resolve())
        return real_request(vault)
    monkeypatch.setattr(cli, "_request_reindex", request)
    monkeypatch.setattr(cli, "_default_runner", lambda *a, **k: (1, "", "fixture unavailable"))
    monkeypatch.setattr(cli, "_task_ceiling", lambda: 256)
    if not override:
        monkeypatch.setattr(cli, "collect_checks", lambda *a: pytest.fail("Unknown target inspected"))
    rc = dispatch(cli, ["reindex", "--json"] + (["--vault", str(target)] if override else []))
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert captured.err == "" and "private-source" not in captured.out
    assert admitted == ([target.resolve()] if override else [])
    assert payload["ok"] is False and payload["checked"] is override
    assert payload["action"]["completed"] is False and rc == (0 if override else 1)
    if override:
        assert payload["action"]["status"] == "requested"
        check = next(c for c in payload["checks"] if c["name"] == "config")
        assert check == {"name":"config", "ok":False, "detail":"unreadable: " + error_name}
        marker = target / cli.REINDEX_REQUEST
        assert marker.stat().st_mode & 0o777 == 0o600
        assert json.loads(marker.read_bytes())["by"] == "hermes zvec-memory reindex"
    else:
        assert payload["action"]["status"] == "failed" and not target.exists()
    assert legacy.read_bytes() == before and not (home / "zvec-memory").exists()


@pytest.mark.parametrize("store", ["absent", "native-empty", "legacy-empty"])
def test_readable_empty_or_absent_settings_establish_real_default(tmp_path, monkeypatch, capsys, store):
    cli = load_cli()
    home = tmp_path / "empty-home"
    home.mkdir()
    native = home / "zvec-memory/config.json"
    legacy = home / "config.yaml"
    if store == "native-empty":
        native.parent.mkdir()
        native.write_text("{}")
        legacy.write_text(json.dumps({"plugins": {"zvec-memory": {"vault": str(tmp_path / "other")}}}))
    elif store == "legacy-empty":
        legacy.write_text("plugins: {zvec-memory: {}}\n")
    before = {p:p.read_bytes() if p.exists() else None for p in (native, legacy)}
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(cli, "_default_runner", lambda *a, **k: (1, "", "fixture unavailable"))
    monkeypatch.setattr(cli, "_task_ceiling", lambda: 256)
    assert dispatch(cli, ["reindex", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["checked"] is True and payload["action"]["status"] == "requested"
    marker = home / "zvec-memory" / cli.REINDEX_REQUEST
    assert marker.stat().st_mode & 0o777 == 0o600
    assert json.loads(marker.read_bytes())["by"] == "hermes zvec-memory reindex"
    assert {p:p.read_bytes() if p.exists() else None for p in (native, legacy)} == before
    assert not (tmp_path / "other").exists()


@pytest.mark.parametrize("raw", ["operator-reserved-value", False, 0, None])
def test_readable_reserved_diagnostic_field_does_not_make_target_unknown(
        tmp_path, monkeypatch, capsys, raw):
    cli = load_cli()
    home = tmp_path / "reserved-home"
    native = home / "zvec-memory/config.json"
    native.parent.mkdir(parents=True)
    target = tmp_path / "known-target"
    native.write_text(json.dumps({"vault":str(target), "_config_error":raw}))
    before = native.read_bytes()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(cli, "_default_runner", lambda *a, **k: (1, "", "fixture unavailable"))
    monkeypatch.setattr(cli, "_task_ceiling", lambda: 256)
    assert dispatch(cli, ["reindex", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["action"]["status"] == "requested" and payload["checked"] is True
    assert (target / cli.REINDEX_REQUEST).stat().st_mode & 0o777 == 0o600
    assert native.read_bytes() == before and not (native.parent / cli.REINDEX_REQUEST).exists()


@pytest.mark.parametrize("raw", [None, False, 0], ids=["null", "false", "zero"])
@pytest.mark.parametrize("store", ["native", "legacy"])
def test_cold_provider_canonical_home_and_backup_share_falsey_default(
        tmp_path, monkeypatch, raw, store):
    import yaml
    from test_provider import _mod

    home = tmp_path / "cold-home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    config = {"vault":raw, "unknown":[None, False, 0]}
    if store == "native":
        source = home / "zvec-memory/config.json"
        source.parent.mkdir()
        source.write_text(json.dumps(config))
    else:
        source = home / "config.yaml"
        source.write_text(yaml.safe_dump({"plugins":{"zvec-memory":config}}))
    before = source.read_bytes()
    provider = _mod.ZvecMemoryProvider()
    default = (home / "zvec-memory").resolve()
    assert provider._resolve_vault(None) == default
    assert provider.backup_paths() == [str(default)]
    assert type(provider._config["vault"]) is type(raw) and provider._config == config
    assert provider._vault is None and provider._disk_worker is None and provider._index_worker is None
    assert source.read_bytes() == before and not (home / "None").exists()
    assert not (home / "False").exists() and not (home / "0").exists()
    assert not (default / ".reindex-request.json").exists()


@pytest.mark.parametrize("error", [PermissionError, OSError])
def test_native_read_failure_valid_cwd_override_still_admits_real_request(
        tmp_path, monkeypatch, capsys, error):
    cli = load_cli()
    home = tmp_path / "override-home"
    native = home / "zvec-memory/config.json"
    native.parent.mkdir(parents=True)
    native.write_text(json.dumps({"vault":str(tmp_path / "external"), "unknown":None}))
    before = native.read_bytes()
    monkeypatch.setenv("HERMES_HOME", str(home))
    cwd = tmp_path / "caller-directory"
    cwd.mkdir()
    monkeypatch.chdir(cwd)
    real_read = Path.read_text
    seen = []
    def read(path, *a, **k):
        if path == native:
            seen.append(path)
            raise error("private-native-read-fault")
        return real_read(path, *a, **k)
    monkeypatch.setattr(Path, "read_text", read)
    monkeypatch.setattr(cli, "_default_runner", lambda *a, **k: (1, "", "fixture unavailable"))
    monkeypatch.setattr(cli, "_task_ceiling", lambda: 256)
    admitted = []
    real_request = cli._request_reindex
    def request(vault):
        admitted.append(vault.resolve())
        return real_request(vault)
    monkeypatch.setattr(cli, "_request_reindex", request)
    assert dispatch(cli, ["reindex", "--json", "--vault", "override-vault"]) == 0
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert seen == [native] and captured.err == "" and "private-native" not in captured.out
    assert admitted == [(cwd / "override-vault").resolve()]
    assert payload["checked"] is True and payload["action"]["completed"] is False
    assert payload["action"]["status"] == "requested"
    assert next(c for c in payload["checks"] if c["name"] == "config") == {
        "name":"config", "ok":False, "detail":"unreadable: " + error.__name__}
    marker = cwd / "override-vault" / cli.REINDEX_REQUEST
    assert marker.stat().st_mode & 0o777 == 0o600
    assert json.loads(marker.read_bytes())["by"] == "hermes zvec-memory reindex"
    assert native.read_bytes() == before and not (home / "override-vault").exists()
    assert not (native.parent / cli.REINDEX_REQUEST).exists() and not (tmp_path / "external").exists()



L7_TRUTHY_DIAGNOSTIC_VALUES = [
    pytest.param(True, id="bool-true"),
    pytest.param(1, id="int-positive"),
    pytest.param(-1, id="int-negative"),
    pytest.param(1.5, id="float-positive"),
    pytest.param(["opaque-l7-fixture-content"], id="list-nonempty"),
    pytest.param({"opaque": "opaque-l7-fixture-content"}, id="object-nonempty"),
]


@pytest.mark.parametrize("raw", L7_TRUTHY_DIAGNOSTIC_VALUES)
@pytest.mark.parametrize("store", ["native", "legacy"])
@pytest.mark.parametrize("command", ["doctor", "status", "reindex"])
def test_truthy_nonstring_readable_diagnostic_preserves_known_target_json(
        tmp_path, monkeypatch, capsys, raw, store, command):
    import yaml

    cli = load_cli()
    home = tmp_path / "l7-selected-home"
    default = home / "zvec-memory"
    external = tmp_path / "l7-known-external-vault"
    sibling = tmp_path / "l7-unselected-vault"
    default.mkdir(parents=True)
    for target in (external, sibling):
        (target / "facts").mkdir(parents=True)
        (target / "sessions").mkdir()
    native, legacy = default / "config.json", home / "config.yaml"
    config = {"vault": str(external), "_config_error": raw,
              "zg_bin": "/isolated-unavailable-l7-engine",
              "unknown": {"keep": [None, False, 0]}}
    if store == "native":
        native.write_text(json.dumps(config))
        legacy.write_text(yaml.safe_dump({"plugins": {"zvec-memory": {"vault": str(sibling)}}}))
    else:
        legacy.write_text(yaml.safe_dump({"plugins": {"zvec-memory": config}}))
    before = {p: p.read_bytes() if p.exists() else None for p in (native, legacy)}
    sibling_before = {str(p.relative_to(sibling)): p.read_bytes()
                      for p in sibling.rglob("*") if p.is_file()}
    monkeypatch.setenv("HERMES_HOME", str(home))
    settings = cli._plugin_module("settings")
    cold = settings.load_settings(home)
    assert cold == config and type(cold["_config_error"]) is type(raw)
    monkeypatch.setattr(cli, "_default_runner", lambda *a, **k: (1, "", "fixture unavailable"))
    monkeypatch.setattr(cli, "_task_ceiling", lambda: 256)
    real_state, real_request = cli.vault_state, cli._request_reindex
    observed, admitted = [], []
    def state(vault):
        observed.append(vault.resolve())
        return real_state(vault)
    def request(vault):
        admitted.append(vault.resolve())
        return real_request(vault)
    monkeypatch.setattr(cli, "vault_state", state)
    monkeypatch.setattr(cli, "_request_reindex", request)
    rc = dispatch(cli, [command, "--json"])
    captured = capsys.readouterr()
    # json.loads rejects multiple documents/trailing non-whitespace, not just one line.
    payload = json.loads(captured.out)
    assert captured.err == "" and "opaque-l7-fixture-content" not in captured.out
    assert observed == [external.resolve()]
    assert admitted == ([external.resolve()] if command == "reindex" else [])
    assert payload["checked"] is True and payload["ok"] is False
    assert next(c for c in payload["checks"] if c["name"] == "config") == {
        "name": "config", "ok": False, "detail": "unreadable: " + type(raw).__name__}
    assert rc == (1 if command == "doctor" else 0)
    marker = external / cli.REINDEX_REQUEST
    if command == "reindex":
        assert payload["action"]["status"] == "requested"
        assert payload["action"]["completed"] is False
        assert payload["action"]["request"] == str(marker)
        intent = json.loads(marker.read_bytes())
        assert intent["by"] == "hermes zvec-memory reindex" and "identity" not in intent
        from datetime import datetime
        from uuid import UUID
        assert set(intent) == {"by", "request_id", "requested"}
        assert UUID(intent["request_id"]).hex == intent["request_id"]
        assert datetime.fromisoformat(intent["requested"]).utcoffset().total_seconds() == 0
        assert marker.stat().st_mode & 0o777 == 0o600
    else:
        assert payload["action"]["status"] == "checked" and not marker.exists()
    assert not (default / cli.REINDEX_REQUEST).exists()
    assert not (sibling / cli.REINDEX_REQUEST).exists()
    assert {p: p.read_bytes() if p.exists() else None for p in (native, legacy)} == before
    assert settings.load_settings(home) == config
    assert {str(p.relative_to(sibling)): p.read_bytes()
            for p in sibling.rglob("*") if p.is_file()} == sibling_before
    assert not list(home.rglob("*.new"))
