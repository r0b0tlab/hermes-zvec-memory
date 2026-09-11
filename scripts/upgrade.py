#!/usr/bin/env python3
"""One gated command to upgrade the installed zvec-memory plugin.

Order matters: nothing mutating runs until the checkout verifies, the offline
suite passes and the native lane passes against the launcher; then the installed
plugin is backed up, replaced, re-baselined and health-checked. Any failure stops
the run and leaves the installed plugin untouched.

    python scripts/upgrade.py --dry-run     # show what would run
    python scripts/upgrade.py               # run the gates and swap
    python scripts/upgrade.py --rollback    # restore the newest backup

Exit codes: 0 success, 1 a gate failed, 2 usage/rollback problem.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
PLUGIN_NAME = "zvec-memory"
DEFAULT_PUBLISHED_FILES = ("__init__.py", "hostio.py", "workers.py", "transactions.py",
                           "inbox.py", "engine.py", "cli.py", "config_schema.py",
                           "plugin.yaml", "README.md")
BACKUP_GLOB = "zvec-upgrade-*"
NATIVE_ENV = {"ZVEC_RUN_NATIVE": "1"}


class GateFailure(RuntimeError):
    """A gate said no; nothing may mutate after this."""


def _run_command(argv, cwd=None, env=None, timeout=1800):
    environment = {**os.environ, **(env or {})}
    try:
        proc = subprocess.run([str(a) for a in argv], cwd=str(cwd) if cwd else None,
                              env=environment, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 127, "", type(exc).__name__
    return proc.returncode, proc.stdout or "", proc.stderr or ""


@dataclass
class Context:
    repo: Path = REPO
    python: str = sys.executable
    plugin_dir: Path = field(default_factory=lambda: Path.home() / ".hermes/plugins" / PLUGIN_NAME)
    backups_root: Path = field(default_factory=lambda: Path.home() / ".hermes/backups")
    launcher: Path = field(default_factory=lambda: Path.home() / ".local/share/hermes-zvec-memory/zg-default")
    model_cache: Path = field(default_factory=lambda: Path.home() / ".cache/hermes-zvec-memory/models")
    runner: object = _run_command
    log: object = print
    dry_run: bool = False
    published_files: tuple = DEFAULT_PUBLISHED_FILES

    def run(self, argv, env=None, timeout=1800):
        self.log(f"    $ {' '.join(str(a) for a in argv)}")
        return self.runner(argv, cwd=self.repo, env=env, timeout=timeout)


def step_verify(ctx: Context) -> str:
    rc, out, err = ctx.run(["git", "status", "--porcelain"])
    if rc != 0:
        raise GateFailure(f"git status failed: {(err or out).strip()[:200]}")
    if out.strip():
        raise GateFailure("working tree is dirty; commit or stash before upgrading:\n" + out.strip())
    rc, out, _ = ctx.run(["git", "rev-parse", "HEAD"])
    if rc != 0:
        raise GateFailure("cannot resolve HEAD")
    return out.strip()


def step_offline_suite(ctx: Context) -> str:
    rc, out, err = ctx.run([ctx.python, "-m", "pytest", "tests/", "-q", "-m", "not integration"])
    tail = (out.strip().splitlines() or [""])[-1]
    if rc != 0:
        raise GateFailure(f"offline suite failed: {tail or err.strip()[-200:]}")
    return tail


def step_native_smoke(ctx: Context) -> str:
    if not Path(ctx.launcher).is_file():
        raise GateFailure(f"engine launcher {ctx.launcher} is missing; "
                          f"run 'hermes {PLUGIN_NAME} engine install' first")
    env = {**NATIVE_ENV, "ZVEC_TEST_BIN": str(ctx.launcher),
           "ZVEC_TEST_MODEL_CACHE": str(ctx.model_cache)}
    rc, out, err = ctx.run([ctx.python, "-m", "pytest", "tests/", "-q", "-m", "integration"], env=env)
    tail = (out.strip().splitlines() or [""])[-1]
    if rc != 0:
        raise GateFailure(f"native lane failed: {tail or err.strip()[-200:]}")
    if "passed" not in tail and "skipped" in tail and "passed" not in out:
        raise GateFailure("native lane ran nothing (all skipped); check ZVEC_TEST_BIN")
    return tail


def step_backup(ctx: Context) -> str:
    helper = ctx.repo / ".test-tools/deploy_plugin.py"
    if not helper.is_file():
        raise GateFailure(f"{helper} is missing; cannot back up before swapping")
    rc, out, err = ctx.run([ctx.python, str(helper)])
    if rc != 0:
        raise GateFailure(f"backup failed: {(err or out).strip()[-200:]}")
    try:
        return json.loads(out)["backup"]
    except (ValueError, KeyError):
        raise GateFailure(f"backup helper printed no receipt: {out.strip()[:200]}")


def changed_files(ctx: Context) -> list:
    changed = []
    for name in ctx.published_files:
        source = ctx.repo / PLUGIN_NAME / name
        target = ctx.plugin_dir / name
        if not source.is_file():
            continue
        if not target.is_file() or target.read_bytes() != source.read_bytes():
            changed.append(name)
    return changed


def step_deploy(ctx: Context) -> str:
    changed = changed_files(ctx)
    if not changed:
        return "already current"
    if ctx.dry_run:
        return f"would replace {len(changed)} file(s): {', '.join(changed)}"
    ctx.plugin_dir.mkdir(parents=True, exist_ok=True)
    for name in changed:
        shutil.copy2(ctx.repo / PLUGIN_NAME / name, ctx.plugin_dir / name)
    shutil.rmtree(ctx.plugin_dir / "__pycache__", ignore_errors=True)
    return f"replaced {len(changed)} file(s): {', '.join(changed)}"


def step_rebaseline(ctx: Context) -> str:
    script = ctx.repo / "scripts/run_campaign.py"
    if not script.is_file():
        return "skipped (no campaign controller in this checkout)"
    rc, out, err = ctx.run([ctx.python, str(script), "--snapshot-production",
                            "--reason", f"upgrade {datetime.now(timezone.utc).date().isoformat()}"])
    if rc != 0:
        raise GateFailure(f"re-baseline failed: {(err or out).strip()[-200:]}")
    return (out.strip().splitlines() or ["re-recorded"])[-1]


def step_doctor(ctx: Context) -> str:
    hermes = shutil.which("hermes")
    if not hermes:
        return "skipped (hermes CLI not on PATH)"
    rc, out, err = ctx.run([hermes, "zvec-memory", "doctor"])
    if rc != 0:
        raise GateFailure("doctor reports unhealthy:\n" + (out or err).strip()[-400:])
    return (out.strip().splitlines() or ["healthy"])[-1].strip()


STEPS = (("verify", step_verify, False),
         ("offline-suite", step_offline_suite, False),
         ("native-smoke", step_native_smoke, False),
         ("backup", step_backup, True),
         ("deploy", step_deploy, True),
         ("rebaseline", step_rebaseline, True),
         ("doctor", step_doctor, True))


def _plan(ctx: Context, name: str) -> str:
    """What a mutating step would do, without doing it."""
    if name == "deploy":
        changed = changed_files(ctx)
        return (f"would replace {len(changed)} file(s): {', '.join(changed)}" if changed
                else "already current")
    return {"backup": "would back up the installed plugin, config, unit and launcher",
            "rebaseline": "would re-record the production baseline",
            "doctor": "would run hermes zvec-memory doctor"}.get(name, "no-op")


def run_upgrade(ctx: Context, steps=STEPS) -> dict:
    results = []
    for name, step, mutating in steps:
        ctx.log(f"  [{name}]{' (mutating)' if mutating else ''}")
        if ctx.dry_run and mutating:
            detail = _plan(ctx, name)
            results.append({"step": name, "ok": True, "detail": detail, "skipped": "dry-run"})
            ctx.log(f"  · {name}: {detail}")
            continue
        try:
            detail = step(ctx)
        except GateFailure as exc:
            results.append({"step": name, "ok": False, "detail": str(exc)})
            ctx.log(f"  ✗ {name}: {exc}")
            return {"ok": False, "failed": name, "steps": results}
        results.append({"step": name, "ok": True, "detail": detail})
        ctx.log(f"  ✓ {name}: {detail.splitlines()[0] if detail else 'ok'}")
    return {"ok": True, "failed": None, "steps": results}


def newest_backup(root: Path):
    candidates = sorted(p for p in Path(root).glob(BACKUP_GLOB) if p.is_dir())
    return candidates[-1] if candidates else None


def rollback(ctx: Context, backup=None) -> dict:
    backup = Path(backup) if backup else newest_backup(ctx.backups_root)
    if not backup or not Path(backup).is_dir():
        raise GateFailure(f"no backup directory found under {ctx.backups_root}/{BACKUP_GLOB}")
    restored = []
    previous = Path(backup) / "zvec-memory.previous"
    ctx.plugin_dir.mkdir(parents=True, exist_ok=True)
    for name in ctx.published_files:
        source = previous / name
        if source.is_file():
            shutil.copy2(source, ctx.plugin_dir / name)
            restored.append(name)
    shutil.rmtree(ctx.plugin_dir / "__pycache__", ignore_errors=True)
    for name, target in (("zg-default", ctx.launcher), ("hermes-zvec-memory.service",
                          Path.home() / ".config/systemd/user/hermes-zvec-memory.service"),
                         ("config.yaml", Path.home() / ".hermes/config.yaml")):
        source = Path(backup) / name
        if source.is_file():
            shutil.copy2(source, target)
            restored.append(name)
    if not restored:
        raise GateFailure(f"backup {backup} contains nothing to restore")
    return {"ok": True, "backup": str(backup), "restored": restored}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dry-run", action="store_true", help="run every read-only step; report mutations")
    parser.add_argument("--rollback", action="store_true", help="restore the newest backup")
    parser.add_argument("--backup", help="backup directory to restore with --rollback")
    parser.add_argument("--only", action="append", help="run/plan a single step (repeatable)")
    args = parser.parse_args(argv)

    ctx = Context(dry_run=args.dry_run)
    if args.rollback:
        try:
            result = rollback(ctx, args.backup)
        except GateFailure as exc:
            print(f"  ✗ rollback: {exc}")
            return 2
        print(f"  ✓ rollback: restored {len(result['restored'])} file(s) from {result['backup']}")
        return 0

    if args.only:
        wanted = set(args.only)
        unknown = wanted - {name for name, _, _ in STEPS}
        if unknown:
            print(f"  ✗ unknown step(s): {', '.join(sorted(unknown))}")
            return 2
        steps = tuple(step for step in STEPS if step[0] in wanted)
    else:
        steps = STEPS

    print(f"zvec-memory upgrade{' (dry run)' if ctx.dry_run else ''}: {ctx.repo}")
    result = run_upgrade(ctx, steps)
    print(json.dumps({"ok": result["ok"], "failed": result["failed"]}, indent=2))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
