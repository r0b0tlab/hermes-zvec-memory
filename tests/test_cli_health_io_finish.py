"""Known-vault health I/O and real legacy-YAML dispatcher boundaries.

Health errors are scoped synthetic PermissionErrors at real filesystem seams,
not claims of kernel EACCES under namespace root. YAML errors are real parsing.
"""
import json
import os
from pathlib import Path

import pytest

from test_cli import load_cli
from test_cli_health_finish import dispatch


def inject_health_fault(monkeypatch, target, operation, make_error, reached):
    """Instrument the OS stat boundary or actual directory enumeration.

    Python versions implement lstat through different functions; instrument both
    OS entry points so the target is observed without replacing path predicates.
    """
    def wrap(original):
        def fault(path, *args, **kwargs):
            if isinstance(path, (str, bytes, os.PathLike)) and Path(os.fsdecode(path)) == target:
                reached.append(str(path))
                raise make_error()
            return original(path, *args, **kwargs)
        return fault

    if operation == "stat":
        monkeypatch.setattr(os, "stat", wrap(os.stat))
        monkeypatch.setattr(os, "lstat", wrap(os.lstat))
    elif operation == "glob":
        # Keep the historical IDs while instrumenting the actual replacement.
        monkeypatch.setattr(os, "scandir", wrap(os.scandir))
    else:
        monkeypatch.setattr(Path, operation, wrap(getattr(Path, operation)))


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
    observations = []
    inject_health_fault(monkeypatch, target, operation,
                        lambda: PermissionError(sentinel), observations)
    calls = []
    def run(argv, **kwargs):
        calls.append(argv)
        assert argv[0] != "systemctl", "Custom profile must not inspect the default unit"
        return 1, "", "fixture unavailable"
    monkeypatch.setattr(cli, "_default_runner", run)
    monkeypatch.setattr(cli, "_unit_tasks_max", lambda run: pytest.fail("Nonmanaged target must not query the managed unit"))
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


# Additional controls for typed errors and target selection.
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
    reached = []
    inject_health_fault(monkeypatch, vault / leaf, operation,
                        lambda: error_type("private-health-diagnostic"), reached)
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
        inject_health_fault(monkeypatch, vault / "facts", seam,
                            lambda: AssertionError("intentional logic control"), [])
    with pytest.raises(AssertionError, match="intentional logic control"):
        dispatch(cli, [command, "--json"])
    captured = capsys.readouterr()
    assert captured.out == captured.err == ""
    assert native.read_bytes() == before and not (vault / cli.REINDEX_REQUEST).exists()


@pytest.mark.parametrize("command", ["doctor", "status", "reindex"])
@pytest.mark.parametrize("raw", ["opaque-private-invalid-json", '{"records": []}'])
def test_dispatcher_malformed_mirror_is_unhealthy_redacted_and_preserves_target(
        tmp_path, monkeypatch, capsys, command, raw):
    cli, home, vault, native = selected_fixture(tmp_path, monkeypatch)
    mapping = vault / ".mirror-map.json"
    mapping.write_text(raw)
    before = mapping.read_bytes(), native.read_bytes()
    monkeypatch.setattr(cli, "_default_runner", lambda *a, **k: (1, "", "unavailable"))
    monkeypatch.setattr(cli, "_unit_tasks_max", lambda run: pytest.fail("Unexpected managed-unit probe"))
    rc = dispatch(cli, [command, "--json"])
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert captured.err == "" and "opaque-private" not in captured.out
    assert payload["checked"] is True and payload["ok"] is False
    assert {"mirror", "builtin_mirror"} <= set(payload["failures"])
    assert rc == (1 if command == "doctor" else 0)
    marker = vault / cli.REINDEX_REQUEST
    assert marker.exists() is (command == "reindex")
    if command == "reindex":
        assert payload["action"]["status"] == "requested"
        assert payload["action"]["completed"] is False
        assert marker.stat().st_mode & 0o777 == 0o600
    assert (mapping.read_bytes(), native.read_bytes()) == before


@pytest.mark.parametrize("leaf,check_name", [(".mirror-inbox.sqlite3", "inbox"),
    (".mirror-map.json", "mirror"), (".zvec-grep/.zvec-memory-state.json", "identity")])
@pytest.mark.parametrize("condition", ["absent", "unreadable"])
def test_existing_durable_health_error_is_not_reported_as_absence(
        tmp_path, monkeypatch, capsys, leaf, check_name, condition):
    from test_cli import fake_runner
    cli, home, vault, native = selected_fixture(tmp_path, monkeypatch)
    if check_name == "identity" and condition == "unreadable":
        index = vault / ".zvec-grep"
        index.mkdir(exist_ok=True)
        (index / ".zvec-memory-state.json").write_text(json.dumps({
            "plugin_schema": 1, "embedding": "local/potion-retrieval-32m", "zg_bin": "/fixture-engine"}))
    reached = []
    if condition == "unreadable":
        operation = "stat" if check_name != "identity" else "read_text"
        inject_health_fault(monkeypatch, vault / leaf, operation,
                            lambda: PermissionError("private-durable-diagnostic"), reached)
    monkeypatch.setattr(cli, "_default_runner", fake_runner())
    monkeypatch.setattr(cli, "_unit_tasks_max", lambda run: pytest.fail("Nonmanaged target must not query the managed unit"))
    assert dispatch(cli, ["status", "--json"]) == 0
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    check = next(c for c in payload["checks"] if c["name"] == check_name)
    assert check["ok"] is (condition == "absent")
    assert payload["checked"] is True
    if condition == "unreadable":
        assert reached and "PermissionError" in check["detail"] and payload["ok"] is False
    assert captured.err == "" and "private-durable-diagnostic" not in captured.out
