"""Offline contract tests for the committed serial campaign controller."""
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("run_campaign", ROOT / "scripts/run_campaign.py")
assert spec is not None and spec.loader is not None
campaign = importlib.util.module_from_spec(spec)
spec.loader.exec_module(campaign)

CASE = {"label": "offline-1x20", "script": "stress_memory.py",
        "args": ["--lane", "load", "--mode", "offline"], "timeout_s": 360}


def test_default_command_uses_documented_resource_profile():
    command, limits = campaign.build_command(CASE, "hermes-zvec-stress-deadbeef0000.service", 128)
    assert command[0] == "systemd-run"
    assert "MemoryMax=2G" in command and "CPUQuota=200%" in command
    assert "TasksMax=128" in command and "KillMode=control-group" in command
    assert "--slice=app.slice" in command
    assert limits == {"memory_bytes": 2147483648, "cpu_quota_percent": 200,
                      "tasks": 128, "runtime_seconds": 360}


def test_task_budget_is_recorded_and_passed_through():
    command, limits = campaign.build_command(CASE, "hermes-zvec-stress-deadbeef0001.service", 256)
    assert "TasksMax=256" in command
    assert "TasksMax=128" not in command
    assert limits["tasks"] == 256


def test_command_runs_the_named_script_under_the_venv():
    command, _ = campaign.build_command(CASE, "hermes-zvec-stress-deadbeef0002.service", 128)
    assert command[0] == "systemd-run"
    assert command[1] == "--user"
    assert command[-len(CASE["args"]):] == CASE["args"]
    head = command[:-len(CASE["args"])]
    assert head[-2] == str(ROOT / ".venv/bin/python")
    assert head[-1] == str(ROOT / "scripts" / CASE["script"])
    assert "--unit=hermes-zvec-stress-deadbeef0002.service" in command
    assert any(part.startswith("ExecStopPost=") for part in command)
    assert "RuntimeMaxSec=360" in command


def test_rejects_out_of_range_task_budget_and_unknown_case():
    for bad in (0, 63, 4097):
        result = subprocess.run([sys.executable, str(ROOT / "scripts/run_campaign.py"),
                                 "--case", "offline-1x20", "--tasks", str(bad)],
                                capture_output=True, text=True, cwd=ROOT)
        assert result.returncode == 2, (bad, result.stdout, result.stderr)
    result = subprocess.run([sys.executable, str(ROOT / "scripts/run_campaign.py"),
                             "--case", "not-a-case"], capture_output=True, text=True, cwd=ROOT)
    assert result.returncode == 2


def test_planned_cases_are_exposed_by_list():
    result = subprocess.run([sys.executable, str(ROOT / "scripts/run_campaign.py"), "--list"],
                            capture_output=True, text=True, cwd=ROOT)
    assert result.returncode == 0, result.stderr
    labels = result.stdout.split()
    assert "offline-1x20" in labels and "daemon-soak-30m-restart" in labels


def test_manifest_entries_carry_the_task_budget():
    entry = campaign.manifest_entry(CASE, 256, {"pids_peak": 3, "pids_events": {"max": 0}})
    assert entry["tags"]["environment"] == "2G-200percent-256tasks"
    assert entry["tags"]["scenario"] == "offline-1x20"
    assert entry["passed"] is False
    assert entry["metadata"]["metrics"]["cgroup_pids_peak"] == [3]
    assert json.loads(json.dumps(entry)) == entry


def test_manifest_entry_marks_oom_kills():
    entry = campaign.manifest_entry(CASE, 128, {"memory_events": {"oom_kill": 1}})
    assert entry["failure_category"] == "oom"
    assert entry["passed"] is False
