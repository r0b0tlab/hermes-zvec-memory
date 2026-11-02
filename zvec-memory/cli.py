"""`hermes zvec-memory doctor|status|reindex`.

Imported by the host during argparse setup, so the module level stays cheap:
stdlib only, no provider import, no engine start, no host internals. The health
checks live here too, which keeps this the only file the host has to import.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import sqlite3
import stat
import subprocess
import sys
from pathlib import Path
from typing import Callable, Dict, List

COMMANDS = (("doctor", "Check config, vault, engine, index, inbox and mirror state"),
            ("status", "Show the same state without failing on warnings"),
            ("reindex", "Request a full index rebuild on the next provider session"),
            ("engine", "Install the pinned local engine runtime (the one network step)"),
            ("migrate-config", "Unsupported in the reduced core release"))

REQUIRED_CHECKS = ("config", "vault", "engine", "index", "inbox", "mirror", "identity", "service", "tasks")
MIN_TASK_CEILING = 512
# The load-bearing ceiling belongs to the memory service, not to the shell that
# happens to run doctor: the native engine aborts (never degrades) under a small
# task limit. Keep this equal to engine.UNIT_NAME (equivalence-tested).
UNIT_NAME = "hermes-zvec-memory.service"
# Durable half of the deferred rebuild: admitted by the CLI and acknowledged
# only after the provider publishes a successful managed index generation.
REINDEX_REQUEST = ".reindex-request.json"
# Diagnostics refuse larger maps rather than allocating unbounded memory.
MIRROR_MAP_MAX_BYTES = 16 * 1024 * 1024


def unmirrored_builtin_count(entries, mirrored_contents) -> int:
    mirrored = set(mirrored_contents)
    return sum(1 for entry in entries if entry not in mirrored)


def builtin_mirror_check(entries, mirrored_contents) -> dict:
    missing = unmirrored_builtin_count(entries, mirrored_contents)
    return {"name": "builtin_mirror", "ok": missing == 0,
            "detail": f"unmirrored={missing}"}


def read_builtin_entries() -> list:
    """Read optional regular built-ins; unsafe or inaccessible is not absent.

    The configured home and its ancestors must be trusted and stationary.
    Reject a symlinked memories directory. No-follow/nonblocking leaf opens
    also reject a leaf replaced between observation and open.
    """
    home = _hermes_home() / "memories"
    entries = []
    try:
        directory = home.lstat()
    except FileNotFoundError:
        return entries
    if not stat.S_ISDIR(directory.st_mode):
        raise OSError("Unsafe built-in memory directory")
    if not hasattr(os, "O_NOFOLLOW") or not hasattr(os, "O_NONBLOCK"):
        raise OSError("Safe built-in memory open unavailable")
    for name in ("MEMORY.md", "USER.md"):
        path = home / name
        try:
            observed = path.lstat()
        except FileNotFoundError:
            continue
        if not stat.S_ISREG(observed.st_mode):
            raise OSError("Unsafe built-in memory file")
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        try:
            opened = os.fstat(fd)
            if (not stat.S_ISREG(opened.st_mode) or
                    (opened.st_dev, opened.st_ino) != (observed.st_dev, observed.st_ino)):
                raise OSError("Built-in memory changed during admission")
            with os.fdopen(fd, encoding="utf-8") as stream:
                fd = -1
                text = stream.read()
        finally:
            if fd != -1:
                os.close(fd)
        parts = [part.strip() for part in text.split("\n§\n") if part.strip()]
        if parts and parts[0].startswith("#"):
            parts = parts[1:]
        entries.extend(parts)
    return entries


def _read_mirror_map(vault: Path) -> str | None:
    """Read one stable regular leaf; only a genuinely absent entry is optional.

    Vault ancestors must be trusted and stationary. Metadata checks detect
    observable drift, not an atomic snapshot against a same-user attacker.
    """
    path = vault / ".mirror-map.json"
    try:
        observed = path.lstat()
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(observed.st_mode):
        raise ValueError("Mirror map is not a regular file")
    if not hasattr(os, "O_NOFOLLOW") or not hasattr(os, "O_NONBLOCK"):
        raise OSError("Safe mirror map open unavailable")

    def signature(info):
        return (info.st_dev, info.st_ino, info.st_mode, info.st_size,
                info.st_mtime_ns, info.st_ctime_ns)

    if observed.st_size > MIRROR_MAP_MAX_BYTES:
        raise ValueError("Mirror map exceeds diagnostic limit")
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError as exc:
        raise ValueError("Mirror map disappeared") from exc
    try:
        opened = os.fstat(fd)
        if not stat.S_ISREG(opened.st_mode) or signature(opened) != signature(observed):
            raise ValueError("Mirror map changed during admission")
        chunks = []
        size = 0
        while size <= MIRROR_MAP_MAX_BYTES:
            chunk = os.read(fd, min(65536, MIRROR_MAP_MAX_BYTES + 1 - size))
            if not chunk:
                break
            chunks.append(chunk)
            size += len(chunk)
        if size > MIRROR_MAP_MAX_BYTES or size != opened.st_size:
            raise ValueError("Mirror map size changed or exceeds diagnostic limit")
        try:
            current = path.lstat()
        except FileNotFoundError as exc:
            raise ValueError("Mirror map disappeared") from exc
        if signature(os.fstat(fd)) != signature(opened) or signature(current) != signature(opened):
            raise ValueError("Mirror map changed during read")
        return b"".join(chunks).decode("utf-8")
    finally:
        os.close(fd)


def read_mirrored_contents(vault: Path) -> set:
    text = _read_mirror_map(vault)
    if text is None:
        return set()
    value = json.loads(text)
    records = value.get("records", {}) if isinstance(value, dict) else {}
    return {record.get("content") for record in records.values() if isinstance(record, dict)}


def _unit_tasks_max(run: Callable, unit: str = UNIT_NAME) -> tuple:
    """Only a loaded managed target establishes its ceiling; never substitute caller.

    systemctl reports manager defaults for nonexistent units. Unknown, missing,
    unreadable and explicitly unlimited must remain separate observations.
    """
    try:
        rc, out, _err = run(["systemctl", "--user", "show", unit,
                             "--property=LoadState", "--property=TasksMax"])
    except Exception:
        rc, out = 1, ""
    if rc == 0 and "LoadState=loaded" in out.splitlines():
        for line in out.splitlines():
            if line.startswith("TasksMax="):
                raw = line.split("=", 1)[1].strip()
                if raw in ("infinity", "max"):
                    return "unlimited", unit
                try:
                    return int(raw), unit
                except ValueError:
                    break
    return None, unit


def _default_runner(args, timeout: int = 15):
    try:
        proc = subprocess.run([str(a) for a in args], capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 127, "", type(exc).__name__
    return proc.returncode, proc.stdout or "", proc.stderr or ""


def _read_engine_state(path: Path) -> Dict | None:
    """Share the cold read contract without importing the provider initializer.

    Only a missing directory entry is legacy absence. A failed read after
    observing that entry, an alias, or malformed identity is not adoption
    authority; callers must retain the evidence and keep recall closed.
    """
    if path.parent.resolve() != path.parent:
        raise ValueError("Aliased index state directory")
    try:
        mode = path.lstat().st_mode
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(mode):
        raise ValueError("Index state is not a regular file")
    value = json.loads(path.read_text(encoding="utf-8"))
    if (not isinstance(value, dict) or type(value.get("plugin_schema")) is not int
            or value["plugin_schema"] != 1
            or not isinstance(value.get("embedding"), str)
            or not isinstance(value.get("zg_bin"), str) or not value["zg_bin"]):
        raise ValueError("Invalid index identity")
    version = value.get("zg_version")
    if version is not None and (not isinstance(version, str) or not version):
        raise ValueError("Invalid index engine version")
    generation = value.get("index_generation")
    if "index_generation" in value and (not isinstance(generation, str) or not generation):
        raise ValueError("Invalid index generation")
    return value


def _stat_or_none(path: Path, *, follow_symlinks: bool = True):
    """Only a missing entry is absence; other filesystem errors are evidence."""
    try:
        return path.stat() if follow_symlinks else path.lstat()
    except FileNotFoundError:
        return None


def _is_symlink(path: Path) -> bool:
    observed = _stat_or_none(path, follow_symlinks=False)
    return observed is not None and stat.S_ISLNK(observed.st_mode)


def _markdown_count(path: Path):
    observed = _stat_or_none(path)
    if observed is None or not stat.S_ISDIR(observed.st_mode):
        return None
    # Path.glob suppresses directory scan errors, including EACCES on 3.14.
    # Scandir both enumerates and propagates failures, including iteration errors.
    with os.scandir(path) as entries:
        return sum(1 for entry in entries if entry.name.endswith('.md'))


def _validate_mirror_paths(vault: Path, value: Dict) -> None:
    """Read-only equivalent of provider live containment, including staging."""
    roots = {}
    for directory in ("facts", ".mirror-staging"):
        root = vault / directory
        if _is_symlink(root) or root.resolve() != root:
            raise ValueError("Aliased mirror root")
        roots[directory] = root

    def owned(relative, directory):
        rel = Path(relative)
        path = vault / rel
        if (rel.is_absolute() or ".." in rel.parts or _is_symlink(path)
                or path.resolve() != path or not path.is_relative_to(roots[directory])
                or path.suffix != ".md"):
            raise ValueError("Invalid scoped mirror path")

    for record in value["records"].values():
        owned(record["path"], "facts")
    for relative in value["pending_deletes"]:
        owned(relative, "facts")
    for item in value["pending_creates"]:
        owned(item["path"], "facts")
        owned(item["staged"], ".mirror-staging")


def vault_state(vault: Path) -> Dict[str, object]:
    """Durable state that does not need the engine."""
    facts = vault / "facts"
    sessions = vault / "sessions"
    state: Dict[str, object] = {
        "facts": _markdown_count(facts),
        "sessions": _markdown_count(sessions),
        "inbox_journal": None, "inbox_pending": None,
        "inbox_high_watermark": 0,
        "mirror_records": None, "mirror_pending": None,
        "delivery_failed": _stat_or_none(vault / ".mirror-delivery-failed.json", follow_symlinks=False) is not None,
        "rebuild_pending": _stat_or_none(vault / REINDEX_REQUEST, follow_symlinks=False) is not None,
        "refresh_pending": _stat_or_none(vault / ".index-refresh.json", follow_symlinks=False) is not None,
        "index_manifest": _stat_or_none(vault / ".zvec-grep" / "manifest.json") is not None,
        "engine_state": None,
    }
    inbox = vault / ".mirror-inbox.sqlite3"
    try:
        inbox.stat()
        db = sqlite3.connect(inbox.resolve().as_uri() + "?mode=ro", uri=True, timeout=1)
        try:
            db.execute("BEGIN")  # Count and allocated history share one read snapshot.
            columns = {row[1]: row for row in db.execute("PRAGMA table_info(notifications)")}
            if (set(columns) != {"id", "payload"} or
                    columns["id"][2].upper() != "INTEGER" or columns["id"][5] != 1 or
                    columns["payload"][2].upper() != "TEXT" or columns["payload"][3] != 1):
                raise sqlite3.DatabaseError("Invalid notification schema")
            if db.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                raise sqlite3.DatabaseError("Invalid inbox")
            state["inbox_journal"] = db.execute("PRAGMA journal_mode").fetchone()[0]
            state["inbox_pending"] = db.execute("SELECT COUNT(*) FROM notifications").fetchone()[0]
            row = db.execute("SELECT seq FROM sqlite_sequence WHERE name = 'notifications'").fetchone()
            high = row[0] if row else 0
            if type(high) is not int or high < 0:
                raise sqlite3.DatabaseError("Invalid inbox allocated history")
            state["inbox_high_watermark"] = high
        finally:
            db.close()
    except FileNotFoundError:
        pass
    except (OSError, sqlite3.Error) as exc:
        state["inbox_journal"] = f"unreadable: {type(exc).__name__}"
        state["inbox_high_watermark"] = None
    try:
        text = _read_mirror_map(vault)
        if text is not None:
            value = _plugin_module("journal").validate_journal(json.loads(text))
            _validate_mirror_paths(vault, value)
            high = state["inbox_high_watermark"]
            if high is None or value.get("last_notification", 0) > high:
                raise ValueError("Mirror watermark exceeds or cannot verify inbox history")
            state["mirror_records"] = len(value["records"])
            state["mirror_pending"] = sum(len(value[key]) for key in
                                           ("pending_creates", "pending_deletes"))
            state["mirror_refresh_required"] = bool(value.get("refresh_required"))
    except (OSError, ValueError, TypeError, RecursionError) as exc:
        state["mirror_error"] = type(exc).__name__
    sidecar = vault / ".zvec-grep" / ".zvec-memory-state.json"
    try:
        state["engine_state"] = _read_engine_state(sidecar)
    except (OSError, ValueError, TypeError) as exc:
        state["engine_state_error"] = type(exc).__name__
    return state


def _service_diagnostics(config: Dict, run: Callable) -> tuple:
    """Read only the fixed unit selected by this resolved default profile.

    Report artifact/loaded-definition health, not native recall readiness. Do
    not repair, reload, restart, or echo operator-controlled unit/manifest data.
    """
    try:
        home = _hermes_home().resolve()
        if home != (Path.home() / ".hermes").resolve():
            return True, "not_managed (named/custom profile; default unit not inspected)", False
        engine = _plugin_module("engine")
        selected = str(config.get("zg_bin") or "")
        if not selected or Path(selected).expanduser().resolve() != engine.launcher_path(home, config).resolve():
            return True, "not_managed (custom/direct executable; no managed daemon selected)", False
    except (OSError, ValueError, TypeError, RuntimeError) as exc:
        # None means selection is unknown, not an established direct target.
        return False, "unreadable service selection: " + type(exc).__name__, None
    issues = []
    try:
        unit, launcher = engine.unit_path(), engine.launcher_path(home, config)
        manifest_path = engine.runtime_root(home, config) / engine.MANIFEST_NAME
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if not isinstance(manifest, dict):
            raise ValueError("Invalid managed manifest")
        # The installed manifest retains the admitted interpreter even if PATH
        # changes later. Inspection must neither reselect Node nor lay out files.
        raw_node = manifest.get("node")
        if not isinstance(raw_node, str) or not Path(raw_node).is_absolute():
            raise ValueError("Invalid admitted interpreter")
        node = Path(raw_node)
        node_info = _stat_or_none(node)
        if node_info is None or not stat.S_ISREG(node_info.st_mode) or not os.access(node, os.X_OK):
            raise ValueError("Admitted interpreter unavailable")
        if config.get("node_bin"):
            configured = Path(config["node_bin"]).expanduser()
            if not configured.is_absolute() or configured.resolve() != node.resolve():
                issues.append("configured Node drift")
        wanted = engine.unit_template(home, config)
        artifacts = engine._artifact_set(home, config, node=node, version=engine.PINNED_VERSION)
        for name, path, text, mode in artifacts:
            conflict = path.with_name(path.name + ".new")
            if _stat_or_none(conflict, follow_symlinks=False) is not None:
                issues.append(name + " .new conflict")
            observed = _stat_or_none(path, follow_symlinks=False)
            if observed is None or not stat.S_ISREG(observed.st_mode):
                issues.append(name + " missing/unsafe file kind")
                continue
            if observed.st_mode & 0o7777 != mode:
                issues.append(name + " mode drift")
            current = path.read_text(encoding="utf-8")
            if name == "manifest":
                data = json.loads(current)
                # Same explicit legacy timestamp policy as artifact admission;
                # doctor only observes it, never rewrites an old generation.
                if isinstance(data, dict) and isinstance(data.get("updated"), str):
                    data = {key: value for key, value in data.items() if key != "updated"}
                matches = data == json.loads(text)
            else:
                matches = current == text
            if not matches:
                issues.append(name + " drift")
        if not os.access(launcher, os.X_OK):
            issues.append("launcher not executable")
        rc, out, _ = run(["systemctl", "--user", "show", UNIT_NAME,
                          "--property=LoadState", "--property=ExecStart",
                          "--property=NeedDaemonReload"])
        props = dict(line.split("=", 1) for line in out.splitlines() if "=" in line)
        if rc != 0 or props.get("LoadState") != "loaded":
            issues.append("managed unit not loaded")
        if props.get("NeedDaemonReload") != "no":
            issues.append("NeedDaemonReload=" + ("yes" if props.get("NeedDaemonReload") == "yes" else "unknown"))
        start = props.get("ExecStart", "")
        match = re.fullmatch(r"\{ path=(.*?) ; argv\[\]=(.*?) ; .*\}", start)
        expected_start = next(line.split("=", 1)[1] for line in wanted.splitlines()
                              if line.startswith("ExecStart="))
        if (not match or match.group(1) != str(launcher) or
                shlex.split(match.group(2)) != shlex.split(expected_start)):
            issues.append("loaded ExecStart drift/unknown")
    except (OSError, ValueError, TypeError, RuntimeError) as exc:
        issues.append("unreadable artifacts: " + type(exc).__name__)
    return not issues, "; ".join(issues) if issues else "managed artifacts and loaded ExecStart current", True


def collect_checks(vault: Path, config: Dict, runner: Callable | None = None, *,
                   builtin_entries: list | None = None,
                   mirrored_contents: set | None = None) -> List[Dict]:
    raw_run = runner or _default_runner

    def run(argv):
        try:
            return raw_run(argv)
        except Exception as exc:
            return 127, "", type(exc).__name__

    checks: List[Dict] = []

    def add(name: str, ok: bool, detail: str) -> None:
        checks.append({"name": name, "ok": bool(ok), "detail": detail})

    zg = str(config.get("zg_bin") or "")
    state = vault_state(vault)
    add("config", bool(config) and not config.get("_config_error"),
        "configuration unavailable or invalid" if config.get("_config_error") else
        f"embedding={config.get('embedding')} zg_bin={'set' if zg else 'MISSING'}")
    add("vault", state["facts"] is not None and state["sessions"] is not None,
        f"{vault} facts={state['facts']} sessions={state['sessions']}")
    engine_rc, engine_out, _ = run([zg, "--version"]) if zg else (127, "", "")
    settings = _plugin_module("settings")
    version = engine_out.strip() if engine_rc == 0 else None
    engine_ok = engine_rc == 0 and version == settings.PINNED_ENGINE_VERSION
    # Subprocess output is untrusted, even on success. Only emit fixed text
    # (or the pinned version, after exact comparison), never stdout or stderr.
    add("engine", engine_ok, settings.PINNED_ENGINE_VERSION if engine_ok else
        ("version mismatch" if engine_rc == 0 else "unavailable"))
    index_rc, _, _ = run([zg, "status", str(vault), "--check-ready"]) if zg else (127, "", "")
    ready = index_rc == 0
    add("index", ready, "ready" if ready else "not ready")
    if state["inbox_journal"] is None:
        add("inbox", True, "absent (no durable notifications yet)")
    else:
        add("inbox", state["inbox_journal"] == "wal" and state["inbox_pending"] == 0,
            f"journal={state['inbox_journal']} pending={state['inbox_pending']}")
    pending = state["mirror_pending"]
    add("mirror", not state.get("mirror_error") and
        not state.get("mirror_refresh_required") and (pending in (0, None)),
        f"records={state['mirror_records']} pending={pending} error={state.get('mirror_error')}")
    if builtin_entries is not None and mirrored_contents is not None:
        gap = builtin_mirror_check(builtin_entries, mirrored_contents)
        add(gap["name"], gap["ok"], gap["detail"])
    identity_issues = []
    if state["delivery_failed"]:
        identity_issues.append("delivery FAILED marker present")
    if state["rebuild_pending"]:
        identity_issues.append("pending rebuild marker")
    if state["refresh_pending"]:
        identity_issues.append("pending refresh marker")
    if state.get("engine_state_error"):
        identity_issues.append(f"unreadable engine state: {state['engine_state_error']}")
    recorded = state["engine_state"]
    if recorded is not None:
        if recorded["zg_bin"] != zg:
            identity_issues.append("configured engine identity mismatch")
        if recorded["embedding"] != config.get("embedding", settings.DEFAULT_EMBEDDING):
            identity_issues.append("configured model identity mismatch")
        if recorded.get("zg_version") and version and recorded["zg_version"] != version:
            identity_issues.append("executable version identity mismatch")
    add("identity", not identity_issues, "; ".join(identity_issues) if identity_issues else
        ("legacy index metadata absent (identity unverified)" if state["engine_state"] is None
         else "recorded index identity; no pending publication markers"))
    service_ok, service_detail, managed = _service_diagnostics(config, run)
    add("service", service_ok, service_detail)
    if managed is None:
        add("tasks", False, "unknown task target (service selection unreadable)")
        return checks
    if not managed:
        add("tasks", True, "not_managed; caller cgroup not consulted")
        return checks
    ceiling, source = _unit_tasks_max(run)
    observed = "unknown" if ceiling is None else ceiling
    detail = (f"{source} TasksMax={observed}" if source != "caller cgroup"
              else f"caller cgroup pids.max={observed}")
    add("tasks", ceiling == "unlimited" or
        (type(ceiling) is int and ceiling >= MIN_TASK_CEILING), detail)
    return checks


def report(checks: List[Dict]) -> Dict:
    required = [c["name"] for c in checks if c["name"] in REQUIRED_CHECKS]
    optional = [c["name"] for c in checks if c["name"] not in REQUIRED_CHECKS]
    checked = (sorted(required) == sorted(REQUIRED_CHECKS) and
               len(required) == len(REQUIRED_CHECKS) and
               (not optional or optional == ["builtin_mirror"]))
    failures = [c for c in checks if c["ok"] is not True]
    return {"ok": checked and not failures, "checks": checks,
            "failures": [c["name"] for c in failures], "checked": checked}


def resolve_vault(raw: str | None, home: Path) -> Path:
    """Expand the configured vault exactly as the provider does."""
    text = str(raw or "").strip()
    if not text:
        return (Path(home) / "zvec-memory").resolve()
    text = text.replace("$HERMES_HOME", str(home)).replace("${HERMES_HOME}", str(home))
    path = Path(text).expanduser()
    return (path if path.is_absolute() else Path(home) / path).resolve()


class _UnreadableSettings(dict):
    """Distinguish a failed read from operator-supplied diagnostic fields."""


def _configured(override: str | None = None) -> tuple:
    """Cold shared settings; an explicit vault bypasses configured path expansion."""
    home = _hermes_home()
    if override:
        # An explicit vault cannot bypass an unknown configuration home.
        home = home.resolve()
    try:
        config = _plugin_module("settings").load_settings(home)
    except Exception as exc:
        config = _UnreadableSettings(_config_error=type(exc).__name__)
    if override:
        vault = Path(override).expanduser()
        # Validate selection before health/admission; keep relative overrides
        # relative to cwd, not to the configured Hermes home.
        vault.resolve()
    elif isinstance(config, _UnreadableSettings):
        # No destination is established by a failed settings read.
        vault = None
    else:
        vault = resolve_vault(config.get("vault"), home)
    return vault, config


def _engine_module():
    """The checkout-local engine helper, never the provider initializer."""
    return _plugin_module("engine")


def _engine_command(args) -> int:
    """Check an existing engine; fresh installation is unavailable."""
    action = getattr(args, "engine_action", None)
    if action != "install":
        print("usage: hermes zvec-memory engine install [--version 0.2.2]")
        return 2
    try:
        _, config = _configured()
        if config.get("_config_error"):
            raise ValueError("Invalid provider settings; install refused")
        engine = _engine_module()
        version = getattr(args, "version", None)
        if version is None:
            version = engine.PINNED_VERSION
        home = _hermes_home()
        root = engine.runtime_root(home, config)
        if not getattr(args, "json", False):
            print(f"  Checking existing {engine.ENGINE_PACKAGE}@{version} under {root}")
        result = engine.install_engine(home, config, version=version)
    except Exception as exc:
        result = {"status": "failed", "error": type(exc).__name__}
    success = result.get("status") in {"present", "updated"} and result.get("unit_status") != "conflict"
    result = {**result, "action": {"name": "engine install",
                                   "status": result["status"] if success else "failed"},
              "health": {"status": "not_checked"}}
    print(json.dumps(result, indent=2))
    return 0 if success else 1


def _hermes_home() -> Path:
    from hermes_constants import get_hermes_home  # documented canonical helper

    return Path(get_hermes_home())


def _plugin_module(module: str):
    """Load a cold helper without executing the provider package initializer."""
    import hashlib
    import importlib
    from importlib.machinery import ModuleSpec
    from types import ModuleType

    directory = Path(__file__).resolve().parent
    name = "_zvec_cli_" + hashlib.sha256(str(directory).encode()).hexdigest()[:16]
    if name not in sys.modules:
        package = ModuleType(name)
        package.__path__ = [str(directory)]
        package.__package__ = name
        package.__spec__ = ModuleSpec(name, loader=None, is_package=True)
        sys.modules[name] = package
    return importlib.import_module(name + "." + module)


def _request_reindex(vault: Path) -> Path:
    """Admit durable generic intent through the existing maintenance protocol."""
    return _plugin_module("maintenance").request_rebuild(vault)


def _migrate_config_command(args) -> int:
    """Retain the old command spelling solely as a pre-I/O refusal."""
    result = {"status": "unsupported",
              "action": {"name": "migrate-config", "status": "unsupported"},
              "health": {"status": "not_checked"}}
    if getattr(args, "json", False):
        print(json.dumps(result, indent=2))
    else:
        print("Reduced core does not support configuration migration; no files changed.")
    return 2


def zvec_memory_command(args) -> int:
    sub = getattr(args, "zvec_command", None)
    if sub is None:
        print("usage: hermes zvec-memory {doctor,status,reindex,engine,migrate-config}")
        return 2
    if sub == "engine":
        return _engine_command(args)
    if sub == "migrate-config":
        return _migrate_config_command(args)
    try:
        override = getattr(args, "vault", None)
        vault, config = _configured(override) if override else _configured()
        if not isinstance(config, _UnreadableSettings):
            # Do not admit a rebuild for a selection the core provider refuses.
            _plugin_module("settings").validate_core_config(config)
        if vault is None:
            if sub == "reindex":
                raise ValueError("Unknown configured vault; rebuild refused")
            # Preserve existing informational default-vault diagnostics only.
            vault = resolve_vault(None, _hermes_home())
    except (OSError, ValueError, TypeError, RuntimeError) as exc:
        action = {"name": sub, "status": "failed", "error": type(exc).__name__}
        if sub == "reindex":
            action["completed"] = False
        result = {**report([]), "action": action}
        if getattr(args, "json", False):
            print(json.dumps(result, indent=2))
        else:
            print(f"  Could not select the vault: {action['error']}")
        return 1
    try:
        try:
            entries = read_builtin_entries()
        except (OSError, UnicodeError):
            # A failed read is not an empty memory. Retain the selected vault
            # and complete the other checks without echoing exception content.
            entries = None
        mirror_error = None
        try:
            mirrored = read_mirrored_contents(vault)
        except (OSError, ValueError, TypeError, AttributeError, RecursionError) as exc:
            # Still run the durable mirror check, but never claim the optional
            # builtin comparison succeeded when its inputs were unreadable.
            mirrored = None
            mirror_error = type(exc).__name__
        checks = collect_checks(vault, config, builtin_entries=entries,
                                mirrored_contents=mirrored)
        if entries is None:
            checks.append({"name": "builtin_mirror", "ok": False,
                           "detail": "unreadable built-in memory"})
        elif mirror_error:
            checks.append({"name": "builtin_mirror", "ok": False,
                           "detail": "unreadable: " + mirror_error})
    except OSError as exc:
        # Selection already succeeded. Incomplete health is neither absence nor
        # an unknown target: retain this vault for explicit durable admission.
        checks = [{"name": "health", "ok": False,
                   "detail": "unreadable: " + type(exc).__name__}]
    result = report(checks)
    action = {"name": sub, "status": "checked"}
    if sub == "reindex":
        try:
            marker = _request_reindex(vault)
            action = {"name": sub, "status": "requested", "completed": False,
                      "request": str(marker)}
        except (OSError, ValueError, TypeError) as exc:
            action = {"name": sub, "status": "failed", "completed": False,
                      "error": type(exc).__name__}
    result["action"] = action
    if getattr(args, "json", False):
        print(json.dumps(result, indent=2))
    else:
        for check in result["checks"]:
            print(f"  {'ok  ' if check['ok'] else 'FAIL'} {check['name']:<9} {check['detail']}")
        verdict = "healthy" if result["ok"] else "unhealthy: " + ", ".join(result["failures"])
        print(f"\n  {verdict}\n")
        if sub == "reindex":
            if action["status"] == "requested":
                print(f"  Rebuild requested ({action['request']}): pending the next provider session; not complete.\n")
            else:
                print(f"  Could not write the rebuild request: {action['error']}\n")
    if sub == "reindex":
        return 0 if action["status"] == "requested" else 1
    if sub == "status":
        return 0
    return 0 if result["ok"] else 1


def register_cli(subparser: argparse.ArgumentParser) -> None:
    subs = subparser.add_subparsers(dest="zvec_command")
    for name, help_text in COMMANDS:
        parser = subs.add_parser(name, help=help_text)
        parser.add_argument("--json", action="store_true")
        parser.add_argument("--vault", help="override the configured vault path")
        if name == "engine":
            actions = parser.add_subparsers(dest="engine_action")
            install = actions.add_parser("install", help="fetch the pinned engine package")
            install.add_argument("--version", help="package version (default: the pinned one)")
            install.add_argument("--json", action="store_true", default=argparse.SUPPRESS)
    subparser.set_defaults(func=zvec_memory_command)


# The host resolves getattr(cli_module, f"{provider}_command") and this provider's
# name contains a dash, so export the literal attribute name it looks for.
globals()["zvec-memory_command"] = zvec_memory_command
