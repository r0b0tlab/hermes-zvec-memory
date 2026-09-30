"""F9/INT08: actual-host, quiescent archive/import and cold durable recovery.

Only the native command boundary is substituted. No online backup guarantee:
all admitted work is drained, workers stopped and inbox anchors closed before
snapshot. The .sqlite3 inbox keeps its real name; host .db heuristics are not
worked around. External support means outside HERMES_HOME but under HOME.
Privacy requires the explicit fresh/private-target, umask-077 restore recipe.
Stock umask-022 and existing-broad-file imports remain rejected controls, not
host fixes. Permission verification runs before any cold provider load.
"""
import hashlib
import importlib
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import threading
from types import SimpleNamespace
import zipfile
import sqlite3

import pytest

# Explicit fixture import: this module runs the real selected host, not doubles.
from test_host_contract import HOST_ROOT, host, load, write_native


OLD = "F9 original exact preference"
NEW = "F9 replacement exact preference"
PENDING = "F9 durable pending inbox fact"
SOURCE = "F9 independently stored source fact"


def _rows(path):
    db = sqlite3.connect(path)
    try:
        assert db.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        return [(number, json.loads(payload)) for number, payload in db.execute(
            "SELECT id, payload FROM notifications ORDER BY id")]
    finally:
        # sqlite3's transaction context manager does NOT close a connection.
        # This reader must also be closed before the quiescent snapshot.
        db.close()


def _files(root):
    return {p.relative_to(root).as_posix(): p.read_bytes()
            for p in sorted(root.rglob("*")) if p.is_file()}


def _permission_manifest(root):
    entries = {}
    for path in [root, *sorted(root.rglob("*"))]:
        info = path.lstat()
        entries[path.relative_to(root).as_posix()] = {
            "mode": format(stat.S_IMODE(info.st_mode), "04o"),
            "uid": info.st_uid,
            "kind": "directory" if stat.S_ISDIR(info.st_mode) else
                    "file" if stat.S_ISREG(info.st_mode) else "unsupported",
        }
    return entries


def _assert_private_tree(root):
    assert not any(path.is_symlink() for path in (root, *root.parents)), \
        "private restore verification rejected: symlink target path"
    entries = _permission_manifest(root)
    unsafe = {name: info for name, info in entries.items()
              if int(info["mode"], 8) & 0o7077 or info["uid"] != os.getuid()
              or info["kind"] == "unsupported"}
    assert not unsafe, f"private restore verification rejected: {unsafe}"
    return entries


def _assert_private_state(vault):
    entries = _assert_private_tree(vault)
    assert (vault / ".mirror-inbox.sqlite3").is_file()
    assert (vault / ".mirror-map.json").is_file()
    assert (vault / "facts").is_dir()
    # Empty directories are not archived; pending staging files, when present,
    # are covered by this recursive oracle and the exact-byte snapshot oracle.
    return entries


def _assert_no_background_threads():
    assert threading.enumerate() == [threading.current_thread()], \
        "process-global umask requires complete worker shutdown"


def _prepare_fresh_restore(home, vault):
    # Never use umask as a repair for an existing, possibly broad file mode.
    for root in dict.fromkeys((home, vault)):
        assert not root.is_symlink(), "fresh restore refuses symlink targets"
        if root.exists():
            assert root.is_dir() and not any(root.iterdir()), \
                "fresh restore requires empty targets"
            info = root.stat()
            assert info.st_uid == os.getuid() and not stat.S_IMODE(info.st_mode) & 0o077, \
                "fresh restore requires private owned targets"
    home.mkdir(mode=0o700, exist_ok=True)
    vault.mkdir(mode=0o700, exist_ok=True)
    # Host archives omit empty directories. Provision private empty provider
    # and host config-backup directories before import, not a chmod repair.
    # Cold host load_config creates backups/config under its ambient mask.
    (home / "backups").mkdir(mode=0o700)
    (home / "backups/config").mkdir(mode=0o700)
    for name in ("facts", "sessions", ".mirror-staging", ".zvec-grep"):
        (vault / name).mkdir(mode=0o700)


def _verify_documented_private_roots(home, vault, monkeypatch, record_property, phase):
    # Execute the exact reviewed read-only snippet in-process: archive_host
    # deliberately forbids subprocess/service attempts, including swallowed ones.
    docs = Path(__file__).resolve().parents[1] / "docs/maintenance.md"
    snippet = docs.read_text().split("<!-- f9-permission-verifier -->", 1)[1]
    snippet = snippet.split("```python\n", 1)[1].split("\n```", 1)[0] + "\n"
    _assert_no_background_threads()
    with monkeypatch.context() as context:
        context.setattr(sys, "argv", ["verify-private.py", str(home), str(vault)])
        exec(compile(snippet, "docs/maintenance.md#f9-permission-verifier", "exec"), {})
    record_property(phase + "_docs_verifier_sha256", hashlib.sha256(snippet.encode()).hexdigest())
    record_property(phase + "_docs_verifier_exit", 0)


def _drain(provider):
    assert provider._disk_worker.drain(3)
    assert provider._index_worker.drain(3)


def _stop(runtime):
    # Host flush alone does not drain the nested provider workers.
    assert runtime.manager.flush_pending(timeout=3)
    _drain(runtime.provider)
    runtime.manager.shutdown_all()
    p = runtime.provider
    assert p._shutdown
    for worker in (p._disk_worker, p._index_worker):
        assert worker._closed
        assert worker._queue.unfinished_tasks == 0
        assert not worker._thread.is_alive()
        assert worker.submit(lambda: None) is False
    assert p._mirror_inbox._anchor is None
    assert p._vault_lock._depth == 0 and p._vault_lock._fd is None


@pytest.fixture
def archive_host(host, record_property):
    """Keep the host's import service branch inert via its actual scope guard."""
    # The reused host fixture chooses HOME=host.tmp and HERMES_HOME=.../profile.
    # A harmless default-install marker makes real run_import leave services
    # alone. Do not patch run_import, Path.home, or any backup/restore helper.
    default = host.tmp / ".hermes"
    default.mkdir(exist_ok=True)
    marker = default / "config.yaml"
    marker.write_bytes(b"# synthetic default-install exclusion sentinel\n")
    before = _files(default)
    from hermes_cli import backup
    assert Path(backup.__file__).resolve() == HOST_ROOT / "hermes_cli/backup.py"
    assert backup.get_default_hermes_root() == host.home
    assert backup.get_hermes_home() == host.home
    assert Path.home() == host.tmp
    record_property("selected_host_root", str(HOST_ROOT))
    record_property("host_backup_sha256", hashlib.sha256(
        Path(backup.__file__).read_bytes()).hexdigest())
    from hermes_cli import backup_restore
    assert Path(backup_restore.__file__).resolve() == HOST_ROOT / "hermes_cli/backup_restore.py"
    record_property("host_backup_restore_sha256", hashlib.sha256(
        Path(backup_restore.__file__).read_bytes()).hexdigest())
    record_property("certification", "quiescent-only; offline native seam; no online guarantee")
    yield SimpleNamespace(host=host, backup=backup)
    assert _files(default) == before


@pytest.fixture
def runtime_factory(archive_host, monkeypatch):
    """Same real loader/manager lifecycle as managed; command-complete zg seam.

    The older managed fixture only handles query/index, not --version, and fixes
    the vault internally. This local native-only seam supports both locations.
    Index success is an offline fixture, never proof of native readiness.
    """
    from agent.memory_manager import MemoryManager
    runtimes = []

    def start():
        provider = load(archive_host.host)
        assert provider._vault is None, "cold loader must construct a new instance"
        calls = []

        def native(args, timeout):
            args = list(args)
            calls.append(args)
            if args == ["--version"]:
                return 0, "0.2.2\n", ""
            if args[0] == "index":
                # Harmless manifest at the only substituted engine boundary.
                index = provider._vault / ".zvec-grep"
                index.mkdir(exist_ok=True)
                (index / "manifest.json").write_text("{}", encoding="utf-8")
                return 0, "offline index fixture", ""
            if args[0] == "query":
                assert args[args.index("--refresh") + 1] == "off"
                return 0, "facts/offline.md:1: offline query fixture", ""
            raise AssertionError(f"unexpected native command: {args}")

        monkeypatch.setattr(provider, "_run_zg", native)
        manager = MemoryManager()
        manager.add_provider(provider)
        runtime = SimpleNamespace(manager=manager, provider=provider, calls=calls)
        runtimes.append(runtime)
        manager.initialize_all("f9-cold-session", agent_context="primary")
        assert provider._vault is not None
        assert provider._mirror_inbox is not None
        _drain(provider)
        return runtime

    yield start
    for runtime in reversed(runtimes):
        _stop(runtime)


def _configure(host, external):
    vault = host.tmp / "external-vault" if external else host.home / "zvec-memory"
    write_native(host, {"vault": str(vault), "zg_bin": sys.executable,
                        "auto_extract": False, "reindex_min_seconds": 3600})
    (host.home / "config.yaml").write_text(
        "memory:\n  provider: zvec-memory\n", encoding="utf-8")
    return vault


def _archive_restore(archive_host, vault, record_property, restore_case):
    host, backup = archive_host.host, archive_host.backup
    before_vault = _files(vault)
    # The host deliberately excludes its own backups/ config history. Bind
    # every selected profile byte, not intentionally excluded recovery copies.
    before_profile = {name: data for name, data in _files(host.home).items()
                      if not backup._should_exclude(Path(name))}
    output = host.tmp / "quiescent.zip"
    assert backup.run_backup(SimpleNamespace(output=str(output), keep=0)) is True
    prefix = (vault.relative_to(host.home).as_posix()
              if vault.is_relative_to(host.home) else
              "_external/" + vault.relative_to(Path.home()).as_posix())
    with zipfile.ZipFile(output) as archive:
        assert archive.testzip() is None
        for relative, data in before_vault.items():
            assert archive.read(prefix + "/" + relative) == data
        archive_modes = {info.filename: format((info.external_attr >> 16) & 0o7777, "04o")
                         for info in archive.infolist()}
        assert archive_modes[prefix + "/.mirror-inbox.sqlite3"] == "0600"
        assert archive_modes[prefix + "/.mirror-map.json"] == "0600"
        assert all(archive_modes[prefix + "/" + name] == "0600"
                   for name in before_vault
                   if name.startswith(("facts/", ".mirror-staging/")))
        record_property("archive_modes", json.dumps(archive_modes, sort_keys=True))
        record_property("source_vault_modes", json.dumps(_permission_manifest(vault), sort_keys=True))
        record_property("archive_members", json.dumps(sorted(archive.namelist())))
        record_property("archive_sha256", hashlib.sha256(output.read_bytes()).hexdigest())
    # Destroy the complete synthetic source generation, then use the supported
    # host importer. Nothing is repaired/copied by a custom archive routine.
    if not vault.is_relative_to(host.home):
        shutil.rmtree(vault)
    shutil.rmtree(host.home)
    assert not vault.exists() and not host.home.exists()
    _assert_no_background_threads()
    _prepare_fresh_restore(host.home, vault)
    broad_names = [name for name in before_vault
                   if name == ".mirror-inbox.sqlite3" or name.startswith("facts/")]
    if restore_case == "077-existing-broad":
        # Deliberately unsupported import, solely to demonstrate that umask
        # cannot repair existing modes. Broadening is BEFORE import, synthetic.
        for name in broad_names:
            target = vault / name
            target.write_bytes(b"F9 synthetic existing-target control\n")
            target.chmod(0o644)
        existing = _files(vault)
        with pytest.raises(AssertionError, match="requires empty targets"):
            _prepare_fresh_restore(host.home, vault)
        assert _files(vault) == existing, "refusal must not change target bytes"
        record_property("existing_target_modes_before_import", json.dumps(
            _permission_manifest(vault), sort_keys=True))
    mask = 0o022 if restore_case == "022-fresh" else 0o077
    previous_mask = os.umask(mask)
    try:
        assert backup.run_import(SimpleNamespace(zipfile=str(output), force=True)) is None
    finally:
        os.umask(previous_mask)
    _assert_no_background_threads()
    record_property("restore_case", restore_case)
    record_property("restore_umask", format(mask, "04o"))
    record_property("restored_vault_modes_before_load", json.dumps(_permission_manifest(vault), sort_keys=True))
    assert _files(vault) == before_vault
    restored_profile = _files(host.home)
    assert {k: restored_profile[k] for k in before_profile} == before_profile
    if restore_case == "077-fresh":
        _assert_private_state(vault)  # BEFORE any provider loading/recovery
        record_property("restored_profile_modes_before_load", json.dumps(
            _assert_private_tree(host.home), sort_keys=True))
        record_property("preactivation_privacy", "accepted before provider load")
    else:
        assert all(stat.S_IMODE((vault / name).stat().st_mode) == 0o644
                   for name in broad_names), "stock importer must remain an honest unsafe control"
        with pytest.raises(AssertionError, match="private restore verification rejected"):
            _assert_private_state(vault)
        record_property("preactivation_privacy", "rejected; no provider load or recovery")
    return before_vault


@pytest.mark.parametrize("external", [False, True], ids=["internal", "external-under-home"])
@pytest.mark.parametrize("interrupted", [False, True], ids=["committed", "interrupted-replace"])
@pytest.mark.parametrize("restore_case", ["077-fresh", "022-fresh", "077-existing-broad"])
def test_quiescent_host_archive_restore_cold_recovery_once(
        archive_host, runtime_factory, monkeypatch, record_property, external, interrupted, restore_case):
    host = archive_host.host
    vault = _configure(host, external)
    runtime = runtime_factory()
    p = runtime.provider
    assert p._vault == vault.resolve()
    stored = json.loads(runtime.manager.handle_tool_call("memory_store", {"content": SOURCE}))
    assert stored["status"] == "stored"
    source = Path(stored["path"])
    source_bytes = source.read_bytes()
    runtime.manager.on_memory_write("add", "user", OLD, {})
    _drain(p)
    committed = p._mirror_state()
    assert [r["content"] for r in committed["records"].values()] == [OLD]
    assert p._mirror_inbox.first() is None
    old_path = vault / next(iter(committed["records"].values()))["path"]
    old_bytes = old_path.read_bytes()

    if interrupted:
        save = p._save_mirror_state
        fault_seen = []

        def commit_then_interrupt(state):
            save(state)  # Real atomic/fsync journal publication lands first.
            if state["pending_creates"] and state["pending_deletes"]:
                fault_seen.append(json.loads((vault / ".mirror-map.json").read_text()))
                raise OSError("F9 interruption after journal commit before publication")

        with monkeypatch.context() as fault:
            fault.setattr(p, "_save_mirror_state", commit_then_interrupt)
            runtime.manager.on_memory_write("replace", "user", NEW,
                                            {"previous_content": OLD, "old_text": OLD})
            _drain(p)
        assert len(fault_seen) == 1
        state = p._mirror_state()
        assert state == fault_seen[0]
        assert len(state["pending_creates"]) == len(state["pending_deletes"]) == 1
        assert state["pending_deletes"] == [old_path.relative_to(vault).as_posix()]
        assert old_path.read_bytes() == old_bytes
        create = state["pending_creates"][0]
        assert NEW in (vault / create["staged"]).read_text()
        assert not (vault / create["path"]).exists()
        assert p._mirror_inbox.first() is not None
        assert p._recall_token() is None

    _stop(runtime)  # no provider/host worker or native owner crosses the snapshot
    # Use the actual durable inbox helper to add a closed-generation pending
    # notification. There is no active provider to dispatch or acknowledge it.
    inbox_type = importlib.import_module(p.__module__ + ".inbox").MirrorInbox
    inbox = inbox_type(vault)
    try:
        inbox.append(["add", "user", PENDING, {}])
        pending_rows = _rows(inbox.path)
        assert len(pending_rows) == (2 if interrupted else 1)
        watermark = inbox.high_watermark()
    finally:
        inbox.close()
    assert inbox._anchor is None
    assert not (vault / ".mirror-inbox.sqlite3-wal").exists()
    assert not (vault / ".mirror-inbox.sqlite3-shm").exists()
    journal_bytes = (vault / ".mirror-map.json").read_bytes()
    record_property("snapshot_pending_rows", json.dumps(pending_rows))
    record_property("snapshot_journal_sha256", hashlib.sha256(journal_bytes).hexdigest())
    record_property("shutdown", "host flushed; both workers drained/closed/dead; inbox closed; vault lock released")
    frozen = _archive_restore(archive_host, vault, record_property, restore_case)
    assert source.read_bytes() == source_bytes
    assert (vault / ".mirror-map.json").read_bytes() == journal_bytes
    assert _rows(vault / ".mirror-inbox.sqlite3") == pending_rows
    if restore_case != "077-fresh":
        return  # fail closed: negative controls never activate restored state

    _verify_documented_private_roots(host.home, vault, monkeypatch, record_property, "preactivation")
    recovered = runtime_factory()
    q = recovered.provider
    assert q is not p
    expected = {NEW if interrupted else OLD, PENDING}
    state = q._mirror_state()
    assert {r["content"] for r in state["records"].values()} == expected
    assert len(state["records"]) == 2
    assert state["last_notification"] == watermark
    assert q._mirror_inbox.high_watermark() == watermark
    assert q._mirror_inbox.first() is None
    assert state["pending_creates"] == state["pending_deletes"] == []
    assert state["refresh_required"] is False
    assert q._recall_token() is not None
    assert source.read_bytes() == source_bytes
    assert len(list((vault / "facts").glob("*.md"))) == 3
    for record in state["records"].values():
        assert (vault / record["path"]).read_text().endswith("\n\n" + record["content"] + "\n")
    if interrupted:
        assert not old_path.exists()
        assert create["staged"] in frozen
    assert list((vault / ".mirror-staging").iterdir()) == []
    index_count = len([call for call in recovered.calls if call[0] == "index"])
    # Startup replay itself can submit an index before initialize joins refresh
    # work, producing one bounded continuation. Exactly-once here means durable
    # mirror effects, not exactly one engine dispatch under that scheduling.
    assert 1 <= index_count <= 2
    record_property("recovery_native_index_calls", index_count)
    stable = _files(vault)
    # Shared runtime recovery helpers must be idempotent, including replay of
    # the journal-committed but unacknowledged replacement notification.
    q._recover_mirrors()
    q._drain_mirror_inbox()
    _drain(q)
    assert _files(vault) == stable
    assert q._mirror_state() == state
    assert len([call for call in recovered.calls if call[0] == "index"]) == index_count
    _stop(recovered)
    record_property("recovered_vault_modes", json.dumps(_assert_private_state(vault), sort_keys=True))
    record_property("recovered_profile_modes", json.dumps(_permission_manifest(host.home), sort_keys=True))
    _assert_private_tree(host.home)
    _verify_documented_private_roots(host.home, vault, monkeypatch, record_property, "first_cold")
    stable = _files(vault)
    second = runtime_factory()
    assert second.provider is not q
    assert second.provider._mirror_state() == state
    assert second.provider._mirror_inbox.first() is None
    assert not any(call[0] == "index" for call in second.calls)
    _stop(second)
    # Compare equally closed generations: opening a real WAL database creates
    # transient WAL/SHM files even when no durable transaction is replayed.
    assert _files(vault) == stable
    record_property("second_cold_vault_modes", json.dumps(_assert_private_state(vault), sort_keys=True))
    record_property("second_cold_profile_modes", json.dumps(_permission_manifest(host.home), sort_keys=True))
    _assert_private_tree(host.home)
    _verify_documented_private_roots(host.home, vault, monkeypatch, record_property, "second_cold")


def test_actual_host_archive_excludes_outside_user_home(archive_host, record_property, capsys):
    host, backup = archive_host.host, archive_host.backup
    # The path is still wholly test-owned, but genuinely outside synthetic HOME.
    outside = host.tmp.parent / (host.tmp.name + "-outside-user-home")
    outside.mkdir()
    try:
        note = outside / "excluded.md"
        note.write_bytes(b"F9 outside-home exclusion control\n")
        write_native(host, {"vault": str(outside)})
        (host.home / "config.yaml").write_text("memory:\n  provider: zvec-memory\n")
        assert backup._collect_memory_provider_external_paths() == [outside]
        assert load(host)._vault is None
        output = host.tmp / "outside-control.zip"
        assert backup.run_backup(SimpleNamespace(output=str(output), keep=0)) is True
        text = capsys.readouterr().out
        assert "outside your home directory" in text and str(outside) in text
        with zipfile.ZipFile(output) as archive:
            assert not any(n.startswith("_external/") for n in archive.namelist())
            assert not any(n.endswith("excluded.md") for n in archive.namelist())
        record_property("outside_home_support", "declared by provider; excluded by actual host archive")
        note.unlink()
        assert backup.run_import(SimpleNamespace(zipfile=str(output), force=True)) is None
        assert not note.exists(), "host import must not manufacture excluded state"
    finally:
        shutil.rmtree(outside)


def test_actual_import_command_parser_help(archive_host, record_property, capsys):
    # The same selected-host registrar is wired by main._build_cli_parser.
    # Inspect its real help without loading the rest of the CLI or services.
    import argparse
    from hermes_cli.subcommands import import_cmd
    assert Path(import_cmd.__file__).resolve() == HOST_ROOT / "hermes_cli/subcommands/import_cmd.py"
    parser = argparse.ArgumentParser(prog="hermes")
    subparsers = parser.add_subparsers(dest="command")
    import_cmd.build_import_cmd_parser(subparsers, cmd_import=archive_host.backup.run_import)
    with pytest.raises(SystemExit) as exited:
        parser.parse_args(["import", "--help"])
    assert exited.value.code == 0
    help_text = capsys.readouterr().out
    assert "zipfile" in help_text and "--force" in help_text
    args = parser.parse_args(["import", "/synthetic/quiescent.zip", "--force"])
    assert args.zipfile == "/synthetic/quiescent.zip" and args.force is True
    assert args.func is archive_host.backup.run_import
    record_property("real_import_parser_help", help_text)
    record_property("help_scope", "selected host import registrar, not full CLI startup")


@pytest.mark.parametrize("control", ["private", "broad-file", "symlink", "missing-journal"])
def test_documented_preactivation_permission_verifier(tmp_path, record_property, control):
    # This standalone permission fixture does not claim archive/data validity.
    # Run the exact published snippet in the same contained test environment,
    # independently of the archive_host fixture's no-subprocess service guard.
    docs = Path(__file__).resolve().parents[1] / "docs/maintenance.md"
    snippet = docs.read_text().split("<!-- f9-permission-verifier -->", 1)[1]
    snippet = snippet.split("```python\n", 1)[1].split("\n```", 1)[0] + "\n"
    script = tmp_path / "verify-private.py"
    script.write_text(snippet)
    home = tmp_path / "private-profile"
    vault = home / "zvec-memory"
    _prepare_fresh_restore(home, vault)
    for name in (".mirror-inbox.sqlite3", ".mirror-map.json", "facts/private.md"):
        fd = os.open(vault / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as target:
            target.write(b"synthetic permission-only fixture\n")
    if control == "broad-file":
        (vault / ".mirror-inbox.sqlite3").chmod(0o644)
    elif control == "symlink":
        (vault / "linked-fact").symlink_to(vault / "facts/private.md")
    elif control == "missing-journal":
        (vault / ".mirror-map.json").unlink()
    result = subprocess.run([sys.executable, "-I", "-B", str(script), str(home), str(vault)],
                            capture_output=True, text=True, timeout=5, cwd=tmp_path)
    assert result.returncode == (0 if control == "private" else 1)
    assert ("Preactivation POSIX permissions verified" in result.stdout
            if control == "private" else "STOP:" in result.stderr)
    record_property("docs_verifier_sha256", hashlib.sha256(script.read_bytes()).hexdigest())
    record_property("docs_verifier_exit", result.returncode)
    record_property("docs_verifier_output", result.stdout + result.stderr)
