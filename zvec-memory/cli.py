"""`hermes zvec-memory doctor|status|reindex`.

Imported by the host during argparse setup, so the module level stays cheap:
stdlib only, no provider import, no engine start, no host internals. The health
checks live here too, which keeps this the only file the host has to import.
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path
from typing import Callable, Dict, List

COMMANDS = (("doctor", "Check config, vault, engine, index, inbox and mirror state"),
            ("status", "Show the same state without failing on warnings"),
            ("reindex", "Request a full index rebuild on the next provider session"))

REQUIRED_CHECKS = ("config", "vault", "engine", "index", "inbox", "mirror", "identity", "tasks")
MIN_TASK_CEILING = 512


def _task_ceiling() -> int | None:
    """pids.max of the current cgroup; None when unlimited or unreadable."""
    try:
        for line in Path("/proc/self/cgroup").read_text().splitlines():
            relative = line.partition("::")[2].strip()
            value = (Path("/sys/fs/cgroup") / relative.lstrip("/") / "pids.max").read_text().strip()
            return None if value == "max" else int(value)
    except (OSError, ValueError):
        return None


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
    if inbox.exists():
        try:
            db = sqlite3.connect(f"file:{inbox}?mode=ro", uri=True)
            try:
                state["inbox_journal"] = db.execute("PRAGMA journal_mode").fetchone()[0]
                state["inbox_pending"] = db.execute("SELECT COUNT(*) FROM notifications").fetchone()[0]
            finally:
                db.close()
        except sqlite3.Error as exc:
            state["inbox_journal"] = f"unreadable: {type(exc).__name__}"
    mapping = vault / ".mirror-map.json"
    if mapping.exists():
        try:
            value = json.loads(mapping.read_text(encoding="utf-8"))
            state["mirror_records"] = len(value.get("records", {}))
            state["mirror_pending"] = sum(len(value.get(key) or []) for key in
                                           ("pending_creates", "pending_deletes"))
            state["mirror_refresh_required"] = bool(value.get("refresh_required"))
        except ValueError:
            state["mirror_records"] = "unreadable"
    sidecar = vault / ".zvec-grep" / ".zvec-memory-state.json"
    if sidecar.exists():
        try:
            state["engine_state"] = json.loads(sidecar.read_text(encoding="utf-8"))
        except ValueError:
            state["engine_state"] = {}
    return state


def collect_checks(vault: Path, config: Dict, runner: Callable | None = None) -> List[Dict]:
    run = runner or _default_runner
    checks: List[Dict] = []

    def add(name: str, ok: bool, detail: str) -> None:
        checks.append({"name": name, "ok": bool(ok), "detail": detail})

    zg = str(config.get("zg_bin") or "")
    state = vault_state(vault)
    add("config", bool(config), f"embedding={config.get('embedding')} zg_bin={'set' if zg else 'MISSING'}")
    add("vault", state["facts"] is not None and state["sessions"] is not None,
        f"{vault} facts={state['facts']} sessions={state['sessions']}")
    engine_rc, engine_out, engine_err = run([zg, "--version"]) if zg else (127, "", "no zg_bin")
    add("engine", engine_rc == 0, (engine_out or engine_err or "unavailable").strip().splitlines()[0][:60])
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
    add("mirror", not state.get("mirror_refresh_required") and (pending in (0, None)),
        f"records={state['mirror_records']} pending={pending}")
    add("identity", not state["delivery_failed"],
        "no delivery-failure marker" if not state["delivery_failed"] else "delivery FAILED marker present")
    ceiling = _task_ceiling()
    add("tasks", ceiling is None or ceiling >= MIN_TASK_CEILING, f"pids.max={ceiling}")
    return checks


def report(checks: List[Dict]) -> Dict:
    failures = [c for c in checks if not c["ok"]]
    return {"ok": not failures, "checks": checks, "failures": [c["name"] for c in failures],
            "checked": sorted(c["name"] for c in checks) == sorted(REQUIRED_CHECKS)}


def resolve_vault(raw: str, home: Path) -> Path:
    """Expand the configured vault exactly as the provider does."""
    text = str(raw or "").strip()
    if not text:
        return (Path(home) / "zvec-memory").resolve()
    text = text.replace("$HERMES_HOME", str(home)).replace("${HERMES_HOME}", str(home))
    path = Path(text).expanduser()
    return (path if path.is_absolute() else Path(home) / path).resolve()


def _configured() -> tuple:
    """(vault, config) from the provider's JSON config, else the default vault."""
    from hermes_constants import get_hermes_home  # documented canonical helper

    home = Path(get_hermes_home())
    path = home / "zvec-memory" / "config.json"
    config: Dict = {}
    if path.is_file():
        try:
            config = json.loads(path.read_text(encoding="utf-8"))
        except ValueError:
            config = {}
    if not config and (home / "config.yaml").is_file():
        try:
            import yaml

            parsed = yaml.safe_load((home / "config.yaml").read_text(encoding="utf-8")) or {}
            config = (parsed.get("plugins", {}) or {}).get("zvec-memory", {}) or {}
        except Exception:
            config = {}
    vault = resolve_vault(config.get("vault"), home)
    return vault, config


def zvec_memory_command(args) -> int:
    sub = getattr(args, "zvec_command", None)
    if sub is None:
        print("usage: hermes zvec-memory {doctor,status,reindex}")
        return 2
    vault, config = _configured()
    override = getattr(args, "vault", None)
    if override:
        vault = Path(override).expanduser()
    checks = collect_checks(vault, config)
    result = report(checks)
    if getattr(args, "json", False):
        print(json.dumps(result, indent=2))
    else:
        for check in result["checks"]:
            print(f"  {'ok  ' if check['ok'] else 'FAIL'} {check['name']:<9} {check['detail']}")
        verdict = "healthy" if result["ok"] else "unhealthy: " + ", ".join(result["failures"])
        print(f"\n  {verdict}\n")
    if sub == "reindex":
        print("  Rebuild requested: the next provider session in this vault reindexes.\n")
        return 0 if result["ok"] else 1
    if sub == "status":
        return 0
    return 0 if result["ok"] else 1


def register_cli(subparser: argparse.ArgumentParser) -> None:
    subs = subparser.add_subparsers(dest="zvec_command")
    for name, help_text in COMMANDS:
        parser = subs.add_parser(name, help=help_text)
        parser.add_argument("--json", action="store_true")
        parser.add_argument("--vault", help="override the configured vault path")
    subparser.set_defaults(func=zvec_memory_command)


# The host resolves getattr(cli_module, f"{provider}_command") and this provider's
# name contains a dash, so export the literal attribute name it looks for.
globals()["zvec-memory_command"] = zvec_memory_command
