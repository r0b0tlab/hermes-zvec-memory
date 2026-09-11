"""The upgrade command must gate before it mutates, and never mutate on a dry run."""
import importlib.util
import json
import shutil
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("zvec_upgrade", ROOT / "scripts/upgrade.py")
assert SPEC is not None and SPEC.loader is not None
upgrade = importlib.util.module_from_spec(SPEC)
sys.modules["zvec_upgrade"] = upgrade
SPEC.loader.exec_module(upgrade)


class FakeRunner:
    """Records every command and answers by substring match."""

    def __init__(self, responses=None, default=(0, "", "")):
        self.calls = []
        self.responses = responses or {}
        self.default = default

    def __call__(self, argv, cwd=None, env=None, timeout=1800):
        argv = [str(a) for a in argv]
        self.calls.append({"argv": argv, "env": env})
        for token, result in self.responses.items():
            if token in " ".join(argv):
                return result
        return self.default

    @property
    def commands(self):
        return [" ".join(call["argv"]) for call in self.calls]

    def ran(self, token):
        return any(token in command for command in self.commands)


def make_ctx(tmp_path, runner, **overrides):
    ctx = upgrade.Context(
        repo=ROOT,
        python=sys.executable,
        plugin_dir=tmp_path / "plugins/zvec-memory",
        backups_root=tmp_path / "backups",
        launcher=tmp_path / "share/zg-default",
        model_cache=tmp_path / "cache/models",
        runner=runner,
        log=lambda *a, **k: None,
        **overrides)
    ctx.launcher.parent.mkdir(parents=True, exist_ok=True)
    ctx.launcher.write_text("#!/usr/bin/env bash\n")
    return ctx


def green_runner():
    return FakeRunner({
        "git status": (0, "", ""),
        "git rev-parse": (0, "abc1234\n", ""),
        "pytest tests/ -q -m not integration": (0, "386 passed, 10 deselected in 30s\n", ""),
        "pytest tests/ -q -m integration": (0, "9 passed, 1 skipped in 27s\n", ""),
        "deploy_plugin.py": (0, json.dumps({"backup": "/tmp/backups/zvec-upgrade-1"}) + "\n", ""),
        "run_campaign.py": (0, "production baseline re-recorded\n", ""),
        "doctor": (0, "  ok   engine    0.2.2\n\n  healthy\n", ""),
    })


@pytest.fixture(autouse=True)
def hermes_on_path(monkeypatch):
    monkeypatch.setattr(upgrade.shutil, "which", lambda name: f"/usr/bin/{name}")


def test_a_dirty_tree_stops_before_anything_mutating(tmp_path):
    runner = FakeRunner({"git status": (0, " M zvec-memory/__init__.py\n", "")})
    ctx = make_ctx(tmp_path, runner)
    result = upgrade.run_upgrade(ctx)
    assert result["ok"] is False and result["failed"] == "verify"
    assert "dirty" in result["steps"][-1]["detail"]
    assert not runner.ran("pytest") and not runner.ran("deploy_plugin.py")


def test_a_failing_offline_suite_stops_before_the_backup(tmp_path):
    runner = green_runner()
    runner.responses["pytest tests/ -q -m not integration"] = (1, "2 failed, 384 passed in 30s\n", "")
    ctx = make_ctx(tmp_path, runner)
    result = upgrade.run_upgrade(ctx)
    assert result["failed"] == "offline-suite"
    assert [s["step"] for s in result["steps"]] == ["verify", "offline-suite"]
    assert not runner.ran("deploy_plugin.py") and not runner.ran("run_campaign.py")
    assert not ctx.plugin_dir.exists() and not ctx.backups_root.exists()


def test_a_failing_native_lane_stops_before_the_backup(tmp_path):
    runner = green_runner()
    runner.responses["pytest tests/ -q -m integration"] = (1, "1 failed in 12s\n", "")
    ctx = make_ctx(tmp_path, runner)
    result = upgrade.run_upgrade(ctx)
    assert result["failed"] == "native-smoke"
    assert not runner.ran("deploy_plugin.py")


def test_a_green_run_gates_then_swaps_in_order(tmp_path):
    runner = green_runner()
    ctx = make_ctx(tmp_path, runner)
    result = upgrade.run_upgrade(ctx)
    assert result["ok"] is True and result["failed"] is None
    assert [s["step"] for s in result["steps"]] == [
        "verify", "offline-suite", "native-smoke", "backup", "deploy", "rebaseline", "doctor"]

    commands = runner.commands
    assert commands[0].startswith("git status") and commands[1].startswith("git rev-parse")
    assert commands.index(" ".join([sys.executable, "-m", "pytest", "tests/", "-q", "-m", "integration"])) \
        < commands.index(f"{sys.executable} {ROOT / '.test-tools/deploy_plugin.py'}")
    native = next(call for call in runner.calls
                  if "integration" in call["argv"] and "not" not in call["argv"])
    assert native["env"]["ZVEC_RUN_NATIVE"] == "1"
    assert native["env"]["ZVEC_TEST_BIN"] == str(ctx.launcher)
    assert (ctx.plugin_dir / "__init__.py").is_file(), "deploy must copy the published files"
    assert result["steps"][4]["detail"].startswith("replaced ")


def test_a_dry_run_runs_the_gates_and_mutates_nothing(tmp_path):
    runner = green_runner()
    ctx = make_ctx(tmp_path, runner, dry_run=True)
    result = upgrade.run_upgrade(ctx)
    assert result["ok"] is True
    assert [s["step"] for s in result["steps"]].count("deploy") == 1
    assert all(s.get("skipped") == "dry-run" for s in result["steps"] if s["step"] in
               {"backup", "deploy", "rebaseline", "doctor"})
    assert runner.ran("pytest")
    assert not runner.ran("deploy_plugin.py") and not runner.ran("run_campaign.py")
    assert not runner.ran("doctor")
    assert not ctx.plugin_dir.exists(), "a dry run must not touch the installed plugin"
    deploy = next(s for s in result["steps"] if s["step"] == "deploy")
    assert deploy["detail"].startswith("would replace ")


def test_deploy_is_a_no_op_when_the_installed_files_are_identical(tmp_path):
    ctx = make_ctx(tmp_path, FakeRunner())
    ctx.plugin_dir.mkdir(parents=True)
    for name in upgrade.DEFAULT_PUBLISHED_FILES:
        source = ROOT / "zvec-memory" / name
        if source.is_file():
            shutil.copy2(source, ctx.plugin_dir / name)
    stamps = {p.name: p.stat().st_mtime_ns for p in ctx.plugin_dir.iterdir()}
    steps = (("deploy", upgrade.step_deploy, True),)
    result = upgrade.run_upgrade(ctx, steps)
    assert result["steps"][0]["detail"] == "already current"
    assert {p.name: p.stat().st_mtime_ns for p in ctx.plugin_dir.iterdir()} == stamps


def test_rollback_restores_the_newest_backup(tmp_path):
    ctx = make_ctx(tmp_path, FakeRunner())
    old = tmp_path / "backups/zvec-upgrade-2026-01-01_000000"
    (old / "zvec-memory.previous").mkdir(parents=True)
    (old / "zvec-memory.previous/__init__.py").write_text("# previous plugin\n")
    (old / "zvec-memory.previous/cli.py").write_text("# previous cli\n")
    (old / "zg-default").write_text("# previous launcher\n")
    (old / "hermes-zvec-memory.service").write_text("[Service]\nExecStart=/usr/bin/true\n")
    (old / "config.yaml").write_text("memory:\n  provider: zvec-memory\n")
    newer = tmp_path / "backups/zvec-upgrade-2026-02-02_000000"
    newer.mkdir()
    (newer / "zvec-memory.previous").mkdir()

    backup = upgrade.newest_backup(ctx.backups_root)
    assert backup == newer, "the newest backup wins, even when incomplete"
    result = upgrade.rollback(ctx, old)
    assert result["ok"] is True and "zg-default" in result["restored"]
    assert (ctx.plugin_dir / "__init__.py").read_text() == "# previous plugin\n"
    assert ctx.launcher.read_text() == "# previous launcher\n"


def test_rollback_without_a_backup_exits_two(tmp_path, monkeypatch, capsys):
    ctx = make_ctx(tmp_path, FakeRunner())
    monkeypatch.setattr(upgrade, "Context", lambda **kwargs: ctx)
    assert upgrade.main(["--rollback"]) == 2
    assert "no backup directory" in capsys.readouterr().out


def test_only_selects_a_single_step_and_rejects_unknown_ones(tmp_path, monkeypatch):
    runner = green_runner()
    ctx = make_ctx(tmp_path, runner)
    monkeypatch.setattr(upgrade, "Context", lambda **kwargs: ctx)
    assert upgrade.main(["--only", "deploy"]) == 0
    assert len(runner.calls) == 0, "deploy copies files, it does not shell out"
    assert upgrade.main(["--only", "bogus"]) == 2
