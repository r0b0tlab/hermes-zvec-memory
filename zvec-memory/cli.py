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
import subprocess
import sys
from pathlib import Path
from typing import Callable, Dict, List

COMMANDS = (("doctor", "Check config, vault, engine, index, inbox and mirror state"),
            ("status", "Show the same state without failing on warnings"),
            ("reindex", "Request a full index rebuild on the next provider session"),
            ("engine", "Install the pinned local engine runtime (the one network step)"),
            ("migrate-config", "Preserve legacy settings in native JSON (quiescent maintenance)"))

REQUIRED_CHECKS = ("config", "vault", "engine", "index", "inbox", "mirror", "identity", "service", "tasks")
MIN_TASK_CEILING = 512
# The load-bearing ceiling belongs to the memory service, not to the shell that
# happens to run doctor: the native engine aborts (never degrades) under a small
# task limit. Keep this equal to engine.UNIT_NAME (equivalence-tested).
UNIT_NAME = "hermes-zvec-memory.service"
# Durable half of the deferred rebuild: admitted by the CLI and acknowledged
# only after the provider publishes a successful managed index generation.
REINDEX_REQUEST = ".reindex-request.json"


def _task_ceiling() -> int | str | None:
    """Direct target cgroup limit: explicit unlimited is distinct from unknown."""
    try:
        for line in Path("/proc/self/cgroup").read_text().splitlines():
            relative = line.partition("::")[2].strip()
            value = (Path("/sys/fs/cgroup") / relative.lstrip("/") / "pids.max").read_text().strip()
            return "unlimited" if value == "max" else int(value)
    except (OSError, ValueError):
        return None


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


def vault_state(vault: Path) -> Dict[str, object]:
    """Durable state that does not need the engine."""
    facts = vault / "facts"
    sessions = vault / "sessions"
    state: Dict[str, object] = {
        "facts": len(list(facts.glob("*.md"))) if facts.is_dir() else None,
        "sessions": len(list(sessions.glob("*.md"))) if sessions.is_dir() else None,
        "inbox_journal": None, "inbox_pending": None,
        "mirror_records": None, "mirror_pending": None,
        "delivery_failed": (vault / ".mirror-delivery-failed.json").exists(),
        "index_manifest": (vault / ".zvec-grep" / "manifest.json").exists(),
        "engine_state": None,
    }
    inbox = vault / ".mirror-inbox.sqlite3"
    try:
        inbox.stat()
        db = sqlite3.connect(inbox.resolve().as_uri() + "?mode=ro", uri=True, timeout=1)
        try:
            columns = {row[1]: row for row in db.execute("PRAGMA table_info(notifications)")}
            if (set(columns) != {"id", "payload"} or
                    columns["id"][2].upper() != "INTEGER" or columns["id"][5] != 1 or
                    columns["payload"][2].upper() != "TEXT" or columns["payload"][3] != 1):
                raise sqlite3.DatabaseError("Invalid notification schema")
            if db.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                raise sqlite3.DatabaseError("Invalid inbox")
            state["inbox_journal"] = db.execute("PRAGMA journal_mode").fetchone()[0]
            state["inbox_pending"] = db.execute("SELECT COUNT(*) FROM notifications").fetchone()[0]
        finally:
            db.close()
    except FileNotFoundError:
        pass
    except (OSError, sqlite3.Error) as exc:
        state["inbox_journal"] = f"unreadable: {type(exc).__name__}"
    mapping = vault / ".mirror-map.json"
    try:
        mapping.stat()
        value = _plugin_module("journal").validate_journal(
            json.loads(mapping.read_text(encoding="utf-8")))
        state["mirror_records"] = len(value["records"])
        state["mirror_pending"] = sum(len(value[key]) for key in
                                       ("pending_creates", "pending_deletes"))
        state["mirror_refresh_required"] = bool(value.get("refresh_required"))
    except FileNotFoundError:
        pass
    except (OSError, ValueError, TypeError) as exc:
        state["mirror_error"] = type(exc).__name__
    sidecar = vault / ".zvec-grep" / ".zvec-memory-state.json"
    try:
        state["engine_state"] = json.loads(sidecar.read_text(encoding="utf-8"))
    except FileNotFoundError:
        pass
    except (OSError, ValueError) as exc:
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
        if not node.is_file() or not os.access(node, os.X_OK):
            raise ValueError("Admitted interpreter unavailable")
        if config.get("node_bin"):
            configured = Path(config["node_bin"]).expanduser()
            if not configured.is_absolute() or configured.resolve() != node.resolve():
                issues.append("configured Node drift")
        wanted = engine.unit_template(home, config)
        for name, path, text in (("unit", unit, wanted),
                                 ("launcher", launcher, engine.launcher_script(home, config, node=node))):
            if path.read_text(encoding="utf-8") != text:
                issues.append(name + " drift")
            if path.with_name(path.name + ".new").exists():
                issues.append(name + " .new conflict")
        if not os.access(launcher, os.X_OK):
            issues.append("launcher not executable")
        expected = {"package": engine.ENGINE_PACKAGE, "version": engine.PINNED_VERSION,
                    "node": raw_node, "entry": str(engine.entry_path(home, config)),
                    "engine_home": str(engine.engine_home(home, config)),
                    "server_url": engine.server_url(config), "launcher": str(launcher),
                    "unit": str(unit), "unit_status": "generated", "tasks_max": engine.TASKS_MAX}
        if not isinstance(manifest, dict) or any(manifest.get(k) != v for k, v in expected.items()):
            issues.append("manifest drift")
        if manifest_path.with_name(manifest_path.name + ".new").exists():
            issues.append("manifest .new conflict")
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


def collect_checks(vault: Path, config: Dict, runner: Callable | None = None) -> List[Dict]:
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
        "unreadable: " + (config["_config_error"] if isinstance(config["_config_error"], str)
                          else type(config["_config_error"]).__name__)
        if config.get("_config_error") else
        f"embedding={config.get('embedding')} zg_bin={'set' if zg else 'MISSING'}")
    add("vault", state["facts"] is not None and state["sessions"] is not None,
        f"{vault} facts={state['facts']} sessions={state['sessions']}")
    engine_rc, engine_out, engine_err = run([zg, "--version"]) if zg else (127, "", "no zg_bin")
    add("engine", engine_rc == 0, (engine_out.strip() or engine_err.strip() or "unavailable").splitlines()[0][:60])
    index_rc, index_out, index_err = run([zg, "status", str(vault), "--check-ready"]) if zg else (127, "", "no zg_bin")
    ready = index_rc == 0
    add("index", ready, (index_err.strip().splitlines()[0] if index_err.strip() else
                         ("ready" if ready else index_out.strip()[:60] or "not ready")))
    if state["inbox_journal"] is None:
        add("inbox", True, "absent (no durable notifications yet)")
    else:
        add("inbox", state["inbox_journal"] == "wal" and state["inbox_pending"] == 0,
            f"journal={state['inbox_journal']} pending={state['inbox_pending']}")
    pending = state["mirror_pending"]
    add("mirror", not state.get("mirror_error") and
        not state.get("mirror_refresh_required") and (pending in (0, None)),
        f"records={state['mirror_records']} pending={pending} error={state.get('mirror_error')}")
    add("identity", not state["delivery_failed"] and not state.get("engine_state_error"),
        f"unreadable engine state: {state['engine_state_error']}" if state.get("engine_state_error") else
        ("no delivery-failure marker" if not state["delivery_failed"] else "delivery FAILED marker present"))
    service_ok, service_detail, managed = _service_diagnostics(config, run)
    add("service", service_ok, service_detail)
    if managed is None:
        add("tasks", False, "unknown task target (service selection unreadable)")
        return checks
    ceiling, source = _unit_tasks_max(run) if managed else (_task_ceiling(), "caller cgroup")
    observed = "unknown" if ceiling is None else ceiling
    detail = (f"{source} TasksMax={observed}" if source != "caller cgroup"
              else f"caller cgroup pids.max={observed}")
    add("tasks", ceiling == "unlimited" or
        (type(ceiling) is int and ceiling >= MIN_TASK_CEILING), detail)
    return checks


def report(checks: List[Dict]) -> Dict:
    checked = sorted(c["name"] for c in checks) == sorted(REQUIRED_CHECKS)
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
    """`hermes zvec-memory engine install [--version X]`: the explicit fetch step."""
    action = getattr(args, "engine_action", None)
    if action != "install":
        print("usage: hermes zvec-memory engine install [--version 0.2.2]")
        return 2
    try:
        _, config = _configured()
        if config.get("_config_error"):
            raise ValueError("Invalid provider settings; install refused")
        engine = _engine_module()
        version = getattr(args, "version", None) or engine.PINNED_VERSION
        home = _hermes_home()
        root = engine.runtime_root(home, config)
        if not getattr(args, "json", False):
            print(f"  Installing {engine.ENGINE_PACKAGE}@{version} under {root}")
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
    """Explicit, quiescent migration; use the runtime's authoritative settings reader."""
    # Legacy parsing is an invocation-only dependency, never CLI registration.
    # Marked YAML errors may contain source snippets; emit only their class.
    from yaml import YAMLError

    path: Path | None = None
    result: Dict
    try:
        home = _hermes_home().resolve()
        path = home / "zvec-memory" / "config.json"
        if path.is_symlink():
            raise ValueError("Refusing symlinked configuration")
        values = _plugin_module("settings").load_settings(home)
        status = "current" if path.exists() else "migrated"
        if status == "migrated":
            _plugin_module("hostio").atomic_json_write(path, values, mode=0o600)
            if _plugin_module("settings").load_settings(home) != values:
                raise ValueError("Configuration readback failed")
        result = {"status": status, "config": str(path)}
    except (OSError, ValueError, TypeError, RuntimeError, YAMLError) as exc:
        result = {"status": "failed", "config": str(path) if path is not None else None,
                  "error": type(exc).__name__}
    result.update(action={"name": "migrate-config", "status": result["status"]},
                  health={"status": "not_checked"})
    if getattr(args, "json", False):
        print(json.dumps(result, indent=2))
    else:
        print(f"  Configuration {result['status']}: {path}")
        if "error" in result:
            print(f"  {result['error']}")
    return 1 if result["status"] == "failed" else 0


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
        checks = collect_checks(vault, config)
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
