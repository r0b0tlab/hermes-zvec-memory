"""F3 read-only doctor: real durable fixtures, isolated native/service doubles."""
import json
import sqlite3
from pathlib import Path

import pytest

from test_cli import load_cli
from test_cli_health_finish import dispatch

MODEL = "local/potion-retrieval-32m"


def doctor_fixture(tmp_path, monkeypatch, version="0.2.2\n"):
    cli = load_cli()
    home = tmp_path / "selected-home"
    vault = tmp_path / "vault"
    (home / "zvec-memory").mkdir(parents=True)
    (vault / "facts").mkdir(parents=True)
    (vault / "sessions").mkdir()
    (vault / ".mirror-staging").mkdir()
    index = vault / ".zvec-grep"
    index.mkdir()
    (index / "manifest.json").write_text("{}")
    sidecar = index / ".zvec-memory-state.json"
    sidecar.write_text(json.dumps({"plugin_schema": 1, "embedding": MODEL,
        "zg_bin": "/fixture-zg", "zg_version": "0.2.2"}))
    mapping = vault / ".mirror-map.json"
    mapping.write_text(json.dumps({"schema_version": 1, "records": {},
        "pending_deletes": [], "pending_creates": [], "last_notification": 0}))
    native = home / "zvec-memory/config.json"
    native.write_text(json.dumps({"vault": str(vault), "embedding": MODEL, "zg_bin": "/fixture-zg"}))
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(cli, "_task_ceiling", lambda: 1024)
    calls = []

    def engine(args, timeout=15):
        calls.append(list(args))
        assert args[0] != "systemctl", "named profile must not inspect default service"
        if args == ["/fixture-zg", "--version"]:
            return 0, version, ""
        assert args == ["/fixture-zg", "status", str(vault), "--check-ready"]
        return 0, "Workspace index is ready", ""

    monkeypatch.setattr(cli, "_default_runner", engine)
    return cli, vault, sidecar, mapping, calls


def observe(cli, capsys, command="doctor"):
    rc = dispatch(cli, [command, "--json"])
    captured = capsys.readouterr()
    assert captured.err == ""
    result = json.loads(captured.out)
    return rc, result, {c["name"]: c for c in result["checks"]}


def retained_files(vault):
    return {p: p.read_bytes() for p in vault.rglob("*") if p.is_file() and not p.is_symlink()}


@pytest.mark.parametrize("marker", [".reindex-request.json", ".index-refresh.json"])
@pytest.mark.parametrize("kind", ["valid", "malformed", "dangling"])
def test_F3_pending_marker_blocks_doctor_without_discarding_obligation(
        tmp_path, monkeypatch, capsys, marker, kind):
    cli, vault, sidecar, mapping, calls = doctor_fixture(tmp_path, monkeypatch)
    path = vault / marker
    if kind == "dangling":
        path.symlink_to(tmp_path / "missing-target")
    else:
        path.write_text("{}" if kind == "valid" else "{private-marker")
    before = retained_files(vault)
    for command in ("doctor", "status"):
        rc, result, checks = observe(cli, capsys, command)
        assert checks["identity"]["ok"] is False, "doctor overlooked a provider recall gate"
        assert result["ok"] is False and result["checked"] is True
        assert rc == (1 if command == "doctor" else 0)
        assert "pending" in checks["identity"]["detail"].lower()
        assert "private-marker" not in json.dumps(result)
        assert all(p.read_bytes() == data for p, data in before.items())
        assert path.is_symlink() if kind == "dangling" else path.exists()
    assert not [a for a in calls if a[1] in ("index", "server")]


@pytest.mark.parametrize("sidecar_present", [True, False], ids=["valid-v021", "missing-legacy"])
def test_F3_normal_legacy_health_is_an_explicit_read_only_positive(
        tmp_path, monkeypatch, capsys, sidecar_present):
    cli, vault, sidecar, mapping, calls = doctor_fixture(tmp_path, monkeypatch)
    if not sidecar_present:
        sidecar.unlink()
    before = retained_files(vault)
    rc, result, checks = observe(cli, capsys)
    assert rc == 0 and result["ok"] is True and result["checked"] is True
    assert checks["identity"]["ok"] is True
    if not sidecar_present:
        assert "legacy" in checks["identity"]["detail"].lower()
        assert not sidecar.exists(), "doctor must not adopt or stamp legacy state"
    assert all(p.read_bytes() == data for p, data in before.items())


def inbox_fixture(vault, allocated=0, autoincrement=True):
    path = vault / ".mirror-inbox.sqlite3"
    db = sqlite3.connect(path)
    try:
        with db:
            db.execute("PRAGMA journal_mode=WAL")
            suffix = " AUTOINCREMENT" if autoincrement else ""
            db.execute("CREATE TABLE notifications (id INTEGER PRIMARY KEY" + suffix + ", payload TEXT NOT NULL)")
            for _ in range(allocated):
                db.execute("INSERT INTO notifications(payload) VALUES ('[]')")
            db.execute("DELETE FROM notifications")
    finally:
        # sqlite3's context manager commits but does not close; a live writer
        # leaves shared-memory read marks in the byte-retention oracle.
        db.close()
    return path


@pytest.mark.parametrize("history", ["missing-inbox", "empty-inbox", "lower-allocated"])
def test_F3_journal_watermark_cannot_exceed_allocated_history(tmp_path, monkeypatch, capsys, history):
    cli, vault, sidecar, mapping, calls = doctor_fixture(tmp_path, monkeypatch)
    if history != "missing-inbox":
        inbox_fixture(vault, allocated=1 if history == "lower-allocated" else 0)
    journal = json.loads(mapping.read_text())
    journal["last_notification"] = 2
    mapping.write_text(json.dumps(journal))
    before = retained_files(vault)
    rc, result, checks = observe(cli, capsys)
    assert checks["mirror"]["ok"] is False, "doctor missed mismatched inbox/journal generations"
    assert result["ok"] is False and rc == 1
    assert all(p.read_bytes() == data for p, data in before.items())
    if history == "missing-inbox":
        assert not (vault / ".mirror-inbox.sqlite3").exists()


def test_F3_acknowledged_allocated_history_is_not_max_pending_id(tmp_path, monkeypatch, capsys):
    cli, vault, sidecar, mapping, calls = doctor_fixture(tmp_path, monkeypatch)
    inbox_fixture(vault, allocated=2)
    journal = json.loads(mapping.read_text())
    journal["last_notification"] = 2
    mapping.write_text(json.dumps(journal))
    rc, result, checks = observe(cli, capsys)
    assert rc == 0 and result["ok"] is True
    state = cli.vault_state(vault)
    assert state["inbox_pending"] == 0 and state["inbox_high_watermark"] == 2


def test_F3_inbox_without_allocated_history_is_not_a_valid_replay_source(tmp_path, monkeypatch, capsys):
    cli, vault, sidecar, mapping, calls = doctor_fixture(tmp_path, monkeypatch)
    inbox_fixture(vault, autoincrement=False)
    rc, result, checks = observe(cli, capsys)
    assert checks["inbox"]["ok"] is False
    assert rc == 1 and result["ok"] is False


@pytest.mark.parametrize("kind", ["outside", "traversal", "absolute", "wrong-suffix", "symlink-file",
    "symlink-parent", "symlink-root", "staging-as-fact", "nul"])
def test_F3_record_path_requires_live_canonical_fact_scope(tmp_path, monkeypatch, capsys, kind):
    cli, vault, sidecar, mapping, calls = doctor_fixture(tmp_path, monkeypatch)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "one.md").write_text("outside retained bytes")
    path = "facts/one.md"
    if kind == "outside":
        path = "sessions/one.md"
    elif kind == "traversal":
        path = "facts/../sessions/one.md"
    elif kind == "absolute":
        path = str(outside / "one.md")
    elif kind == "wrong-suffix":
        path = "facts/one.txt"
    elif kind == "staging-as-fact":
        path = ".mirror-staging/one.md"
    elif kind == "nul":
        path = "facts/one\x00.md"
    elif kind == "symlink-file":
        (vault / path).symlink_to(outside / "one.md")
    elif kind == "symlink-parent":
        (vault / "facts/sub").symlink_to(outside, target_is_directory=True)
        path = "facts/sub/one.md"
    elif kind == "symlink-root":
        (vault / "facts").rmdir()
        (vault / "facts").symlink_to(outside, target_is_directory=True)
    journal = json.loads(mapping.read_text())
    journal["records"] = {"owned": {"target": "memory", "content": "retained", "path": path}}
    mapping.write_text(json.dumps(journal))
    before = mapping.read_bytes()
    state = cli.vault_state(vault)
    assert state.get("mirror_error") == "ValueError", "doctor omitted provider path validation"
    rc, result, checks = observe(cli, capsys)
    assert checks["mirror"]["ok"] is False and rc == 1
    assert mapping.read_bytes() == before and (outside / "one.md").read_text() == "outside retained bytes"


@pytest.mark.parametrize("slot", ["pending-delete", "pending-create-destination", "pending-create-staged"])
def test_F3_pending_paths_are_validated_even_though_work_already_blocks_health(tmp_path, monkeypatch, slot):
    cli, vault, sidecar, mapping, calls = doctor_fixture(tmp_path, monkeypatch)
    journal = json.loads(mapping.read_text())
    if slot == "pending-delete":
        journal["pending_deletes"] = ["sessions/unowned.md"]
    else:
        journal["pending_creates"] = [{"path": "facts/one.md", "staged": ".mirror-staging/one.md"}]
        journal["pending_creates"][0]["path" if slot.endswith("destination") else "staged"] = "sessions/unowned.md"
    mapping.write_text(json.dumps(journal))
    before = mapping.read_bytes()
    state = cli.vault_state(vault)
    assert state.get("mirror_error") == "ValueError", "pending count alone is not path validation"
    assert mapping.read_bytes() == before


@pytest.mark.parametrize("identity", [[], {}, None,
    {"plugin_schema": True, "embedding": MODEL, "zg_bin": "/fixture-zg"},
    {"plugin_schema": 1.0, "embedding": MODEL, "zg_bin": "/fixture-zg"},
    {"plugin_schema": 2, "embedding": MODEL, "zg_bin": "/fixture-zg"},
    {"plugin_schema": 1, "embedding": "local/different", "zg_bin": "/fixture-zg"},
    {"plugin_schema": 1, "embedding": MODEL, "zg_bin": "/different-engine"},
    {"plugin_schema": 1, "embedding": MODEL, "zg_bin": "/fixture-zg", "zg_version": "0.2.1"},
    {"plugin_schema": 1, "embedding": MODEL, "zg_bin": "/fixture-zg", "index_generation": ""},
    {"plugin_schema": 1, "embedding": MODEL, "zg_bin": "/fixture-zg", "index_generation": 2}],
    ids=["array", "empty-object", "null", "bool-schema", "float-schema", "new-schema",
         "wrong-model", "wrong-binary", "wrong-engine", "empty-generation", "integer-generation"])
def test_F3_present_identity_requires_shape_and_effective_compatibility(
        tmp_path, monkeypatch, capsys, identity):
    cli, vault, sidecar, mapping, calls = doctor_fixture(tmp_path, monkeypatch)
    sidecar.write_text(json.dumps(identity))
    before = sidecar.read_bytes()
    rc, result, checks = observe(cli, capsys)
    assert checks["identity"]["ok"] is False, "doctor accepted invalid/incompatible sidecar"
    assert rc == 1 and result["ok"] is False
    assert sidecar.read_bytes() == before


@pytest.mark.parametrize("version", ["0.2.1\n", "0.2.3\n", "", "garbage\n", "0.2.2\nextra"],
                         ids=["old", "new", "empty", "invalid", "extra-output"])
def test_F3_successful_version_command_requires_exact_pinned_value(tmp_path, monkeypatch, capsys, version):
    cli, vault, sidecar, mapping, calls = doctor_fixture(tmp_path, monkeypatch, version=version)
    rc, result, checks = observe(cli, capsys)
    assert checks["engine"]["ok"] is False, "rc=0 is not the executable version pin"
    assert rc == 1 and result["ok"] is False


@pytest.mark.parametrize("error", [PermissionError, OSError, FileNotFoundError],
                         ids=["permission", "io", "present-then-read-missing"])
def test_F3_actual_identity_read_failure_is_not_legacy_absence(tmp_path, monkeypatch, capsys, error):
    cli, vault, sidecar, mapping, calls = doctor_fixture(tmp_path, monkeypatch)
    before = sidecar.read_bytes()
    real_read = Path.read_text
    reached = []

    def fault(path, *args, **kwargs):
        if path == sidecar:
            reached.append(True)
            raise error("private-sidecar-detail")
        return real_read(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", fault)
    rc, result, checks = observe(cli, capsys)
    assert reached, "actual identity reader was not reached"
    assert checks["identity"]["ok"] is False and rc == 1
    assert error.__name__ in checks["identity"]["detail"]
    assert "private-sidecar-detail" not in json.dumps(result)
    assert sidecar.read_bytes() == before


@pytest.mark.parametrize("field", ["zg_bin", "embedding", "plugin_schema", "index_generation", "zg_version"])
def test_F3_valid_generation_and_v021_optional_version_are_compatible(tmp_path, monkeypatch, capsys, field):
    cli, vault, sidecar, mapping, calls = doctor_fixture(tmp_path, monkeypatch)
    state = json.loads(sidecar.read_text())
    if field == "index_generation":
        state[field] = "new-valid-generation"
    elif field == "zg_version":
        state.pop(field)
    sidecar.write_text(json.dumps(state))
    before = sidecar.read_bytes()
    rc, result, checks = observe(cli, capsys)
    assert checks["identity"]["ok"] is True and rc == 0 and result["ok"] is True
    assert sidecar.read_bytes() == before
