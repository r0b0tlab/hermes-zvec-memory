"""Synthetic kernel fixtures; never read or change a production unit."""
import importlib.util
from pathlib import Path
import pytest

ROOT = Path(__file__).resolve().parents[1]


def collector():
    path = ROOT / "scripts/capture_limits.py"
    assert path.exists(), "tracked resource collector missing"
    spec = importlib.util.spec_from_file_location("capture_limits_test", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def fixture(tmp_path):
    unit = "hermes-zvec-stress-fixture.service"
    relative = "/user.slice/app.slice/" + unit
    root = tmp_path / "sys"
    group = root / relative.lstrip("/")
    group.mkdir(parents=True)
    files = {"memory.max": "2147483648", "memory.swap.max": "0",
             "pids.max": "256", "cpu.max": "200000 100000",
             "memory.events": "oom 0\noom_kill 0\n", "pids.events": "max 0\n",
             "cpu.stat": "usage_usec 123\nthrottled_usec 2\n",
             "memory.peak": "900", "memory.swap.peak": "0", "pids.peak": "3"}
    for name, value in files.items(): (group / name).write_text(value)
    proc = tmp_path / "proc-cgroup"
    proc.write_text("0::" + relative + "\n")
    identity = {"ControlGroup": relative, "InvocationID": "a" * 32}
    return unit, proc, root, group, identity


def test_capture_binds_actual_membership_and_effective_values(tmp_path):
    module = collector()
    unit, proc, root, group, identity = fixture(tmp_path)
    result = module.capture(unit, "start", proc=proc, mount=root, identity=identity)
    assert result["unit"] == unit and result["invocation_id"] == "a" * 32
    assert result["control_group"] == identity["ControlGroup"]
    assert result["phase"] == "start"
    assert result["effective"]["memory.swap.max"] == "0"
    assert result["effective"]["cpu.max"] == "200000 100000"
    assert result["memory_events"]["oom_kill"] == 0
    assert result["pids_events"]["max"] == 0
    assert result["memory_peak_bytes"] == 900


@pytest.mark.parametrize("damage", ["wrong_unit", "wrong_group", "bad_invocation", "missing_limit", "malformed_counter"])
def test_capture_refuses_unbound_or_missing_evidence(tmp_path, damage):
    module = collector()
    unit, proc, root, group, identity = fixture(tmp_path)
    if damage == "wrong_unit": unit = "hermes-zvec-stress-other.service"
    elif damage == "wrong_group": identity["ControlGroup"] = "/other"
    elif damage == "bad_invocation": identity["InvocationID"] = ""
    elif damage == "missing_limit": (group / "memory.swap.max").unlink()
    elif damage == "malformed_counter": (group / "pids.events").write_text("max unknown")
    with pytest.raises((ValueError, OSError)):
        module.capture(unit, "stop", proc=proc, mount=root, identity=identity)
