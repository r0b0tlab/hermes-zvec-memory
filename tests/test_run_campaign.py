"""Offline contract tests for the committed serial campaign controller."""
import importlib.util
import json
import subprocess
import sys
import pytest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("run_campaign", ROOT / "scripts/run_campaign.py")
assert spec is not None and spec.loader is not None
campaign = importlib.util.module_from_spec(spec)
spec.loader.exec_module(campaign)

CASE = {"label": "offline-1x20", "script": "stress_memory.py",
        "args": ["--lane", "load", "--mode", "offline"], "timeout_s": 360}

BASE = {
    "files": {"/home/x/.hermes/zvec-memory/config.json": "vaulthash",
              "/home/x/.hermes/plugins/zvec-memory/__init__.py": "pluginhash",
              "/home/x/.local/share/hermes-zvec-memory/zg-default": "wrapperhash"},
    "service": "ActiveState=active\nMainPID=4242",
    "provider": "zvec-memory",
    "memory_config": {"memory": {"provider": "zvec-memory"},
                      "plugins": {"zvec-memory": {"recall_limit": 8}}},
    "config_sha256": "confighash-1",
    "baseline_revision": 1,
}


def snap(**overrides):
    value = json.loads(json.dumps(BASE))
    value.update(overrides)
    return value


def test_default_command_uses_documented_resource_profile():
    # 128 is proven non-viable for the native lanes (see docs/stress-results.md);
    # the measured native profile is the default so a case cannot silently run
    # under a ceiling that fails it.
    command, limits = campaign.build_command(CASE, "hermes-zvec-stress-deadbeef0000.service", 256)
    assert command[0] == "systemd-run"
    assert "MemoryMax=2G" in command and "CPUQuota=200%" in command
    assert "TasksMax=256" in command and "KillMode=control-group" in command
    assert "--slice=app.slice" in command
    assert limits == {"memory_bytes": 2147483648, "swap_bytes": 0, "cpu_quota_percent": 200,
                      "tasks": 256, "runtime_seconds": 360}
    assert campaign.DEFAULT_TASKS == 256


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


def test_identical_snapshots_have_no_differences():
    assert campaign.production_differences(BASE, snap()) == []
    assert campaign.production_differences(BASE, snap(baseline_revision=9)) == []


def test_hermes_rewriting_unrelated_config_keys_is_not_a_difference():
    # The CLI rewrites onboarding/_config_version itself; that must not block a campaign.
    assert campaign.production_differences(BASE, snap(config_sha256="confighash-2")) == []


def test_memory_provider_and_plugin_settings_changes_are_differences():
    changed = snap(memory_config={"memory": {"provider": "none"}, "plugins": {}})
    differences = campaign.production_differences(BASE, changed)
    assert any("memory/plugins" in item for item in differences)
    assert campaign.production_differences(BASE, snap(provider="none")), "provider change missed"


def test_watched_file_and_service_changes_are_differences():
    files = json.loads(json.dumps(BASE["files"]))
    files["/home/x/.hermes/plugins/zvec-memory/__init__.py"] = "tampered"
    differences = campaign.production_differences(BASE, snap(files=files))
    assert any("__init__.py" in item for item in differences)
    assert campaign.production_differences(BASE, snap(service="ActiveState=active\nMainPID=1"))


def test_new_unwatched_file_is_reported_as_added():
    files = json.loads(json.dumps(BASE["files"]))
    files["/home/x/.hermes/plugins/zvec-memory/extra.py"] = "newhash"
    differences = campaign.production_differences(BASE, snap(files=files))
    assert any("extra.py" in item for item in differences)


def test_clean_export_lists_without_private_prerequisites(tmp_path):
    import shutil
    export = tmp_path / "export"
    tracked = subprocess.check_output(["git", "-C", str(ROOT), "ls-files", "-z"]).decode().split("\0")
    for name in filter(None, tracked):
        source, target = ROOT / name, export / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
    script = export / "scripts/run_campaign.py"
    result = subprocess.run([sys.executable, str(script), "--list"], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    assert {"offline-1x20", "native-repeat-2", "daemon-soak-30m-restart"} <= set(result.stdout.split())
    assert not (export / ".test-tools").exists()
    assert not (export / ".git").exists()
    probe = importlib.util.spec_from_file_location("export_campaign", script)
    assert probe is not None and probe.loader is not None
    module = importlib.util.module_from_spec(probe)
    probe.loader.exec_module(module)
    command, _ = module.build_command(CASE, "hermes-zvec-stress-fixture.service", 256)
    assert any(str(export / "scripts/capture_limits.py") in part for part in command)
    assert (export / "scripts/capture_limits.py").is_file()
    result = subprocess.run([sys.executable, str(script), "--case", "unknown"], capture_output=True, text=True, timeout=10)
    assert result.returncode == 2 and not (export / ".test-tools").exists()


def test_explicit_campaign_init_is_private_and_non_overwriting(tmp_path):
    import stat
    output = tmp_path / "campaign"
    args = [sys.executable, str(ROOT / "scripts/run_campaign.py"), "--init", "--output-dir", str(output)]
    result = subprocess.run(args, capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    assert stat.S_IMODE(output.stat().st_mode) == 0o700
    assert json.loads((output / "manifest.json").read_text())["runs"] == []
    assert json.loads((output / "OWNER.json").read_text())["kind"] == "zvec-campaign"
    assert not (output / "production-before.json").exists()
    before = {p.name: p.read_bytes() for p in output.iterdir()}
    assert subprocess.run(args, capture_output=True, timeout=10).returncode == 2
    assert {p.name: p.read_bytes() for p in output.iterdir()} == before


def test_missing_or_unowned_campaign_refuses_before_production(tmp_path, monkeypatch):
    import pytest
    def forbidden(): raise AssertionError("production must not be read")
    monkeypatch.setattr(campaign, "production_snapshot", forbidden)
    for directory in (tmp_path / "missing", tmp_path):
        with pytest.raises(SystemExit) as exc:
            campaign.main(["--case", "offline-1x20", "--output-dir", str(directory)])
        assert exc.value.code == 2


def test_case_definitions_reject_duplicate_labels(tmp_path):
    import pytest
    source = tmp_path / "cases.json"
    case = {**CASE, "prerequisites": ["python", "host"]}
    source.write_text(json.dumps({"schema_version": 1, "cases": [case, case]}))
    with pytest.raises(ValueError, match="duplicate"):
        campaign.load_cases(source)


def test_empty_parent_is_not_empty_recursive_cgroup(tmp_path):
    (tmp_path / "cgroup.procs").write_text("")
    (tmp_path / "cgroup.events").write_text("populated 1\nfrozen 0\n")
    assert campaign.group_empty(tmp_path) is False
    (tmp_path / "cgroup.events").write_text("populated 0\nfrozen 0\n")
    assert campaign.group_empty(tmp_path) is True


def test_missing_population_evidence_is_not_proof_of_cleanup(tmp_path):
    import pytest
    (tmp_path / "cgroup.procs").write_text("")
    with pytest.raises(RuntimeError): campaign.group_empty(tmp_path)
    assert campaign.group_empty(tmp_path / "gone") is True


def test_command_captures_both_effective_limit_phases():
    command, limits = campaign.build_command(CASE, "hermes-zvec-stress-fixture.service", 256)
    assert "MemorySwapMax=0" in command and "CPUQuotaPeriodSec=100ms" in command
    assert limits["swap_bytes"] == 0
    assert any(part.startswith("ExecStartPre=") and "--phase start" in part for part in command)
    assert any(part.startswith("ExecStopPost=") and "--phase stop" in part for part in command)


def initialize_private_campaign(tmp_path, monkeypatch):
    root = tmp_path / "campaign"
    campaign.select_output(root, initialize=True)
    monkeypatch.setattr(campaign, "HERE", root)
    monkeypatch.setattr(campaign, "BASELINE", root / "production-before.json")
    return root


@pytest.mark.parametrize("exception", [RuntimeError("preflight"), KeyboardInterrupt()])
def test_started_attempt_survives_exception_and_manifest_is_idempotent(tmp_path, monkeypatch, exception):
    root = initialize_private_campaign(tmp_path, monkeypatch)
    unit = "hermes-zvec-stress-" + "a" * 32 + ".service"
    with pytest.raises(type(exception)):
        with campaign.attempt_record(CASE, "fix", 256, unit) as receipt:
            assert (root / (unit[:-8] + ".started.json")).is_file()
            raise exception
    path = root / (unit[:-8] + ".json")
    final = json.loads(path.read_text())
    assert final["passed"] is False and final["errors"]
    assert final["failure_category"] in {"interrupted", "controller_exception"}
    assert final["cleanup_verified"] is False
    campaign.publish_attempt(final)
    manifest = json.loads((root / "manifest.json").read_text())
    assert len(manifest["runs"]) == len(manifest["reports"]) == 1
    assert manifest["runs"][0]["attempt_id"] == final["attempt_id"]
    assert manifest["reports"][0]["passed"] is False


def test_campaign_lease_refuses_concurrent_controller(tmp_path, monkeypatch):
    root = initialize_private_campaign(tmp_path, monkeypatch)
    with campaign.campaign_lease():
        with pytest.raises(RuntimeError, match="controller"):
            with campaign.campaign_lease(): pytest.fail("overlapping workload admitted")
    with campaign.campaign_lease(): pass
    assert json.loads((root / "manifest.json").read_text())["runs"] == []


def test_atomic_publication_syncs_file_directory_and_uses_unique_temp(tmp_path, monkeypatch):
    import stat
    path = tmp_path / "manifest.json"
    old_temp = tmp_path / "manifest.json.tmp"
    old_temp.write_text("unrelated retained scratch")
    before = set(tmp_path.iterdir())
    synced = []
    real = campaign.os.fsync
    def fsync(fd):
        synced.append(stat.S_ISDIR(campaign.os.fstat(fd).st_mode))
        return real(fd)
    monkeypatch.setattr(campaign.os, "fsync", fsync)
    campaign.atomic(path, {"runs": []})
    assert json.loads(path.read_text()) == {"runs": []}
    assert synced == [False, True]
    assert old_temp.read_text() == "unrelated retained scratch"
    assert set(tmp_path.iterdir()) == before | {path}


@pytest.mark.parametrize("fault", ["preflight", "build", "spawn", "interrupt"])
def test_execute_retains_failure_and_runs_post_guard(tmp_path, monkeypatch, fault):
    root = initialize_private_campaign(tmp_path, monkeypatch)
    checks = []
    def unchanged():
        checks.append(1)
        return ["fixture drift"] if fault == "preflight" else []
    monkeypatch.setattr(campaign, "unchanged", unchanged)
    monkeypatch.setattr(campaign, "output", lambda command: "/fixture" if command[0] == "systemctl" else "a" * 40)
    monkeypatch.setattr(campaign, "unit_state", lambda unit: {"LoadState": "not-found"}, raising=False)
    def build(*args):
        if fault == "build": raise ValueError("invalid construction")
        return ["fixture-never-executed"], {"runtime_seconds": 1, "tasks":256, "memory_bytes":2147483648,
                                            "swap_bytes":0, "cpu_quota_percent":200}
    monkeypatch.setattr(campaign, "build_command", build)
    def spawn(*args, **kwargs):
        if fault == "interrupt": raise KeyboardInterrupt()
        raise OSError("injected pre-spawn failure")
    monkeypatch.setattr(campaign.subprocess, "run", spawn)
    try:
        result = campaign.execute(CASE, "fix", 256)
        assert result is False
    except BaseException as exc:
        assert fault == "interrupt" and isinstance(exc, KeyboardInterrupt)
    starts = list(root.glob("*.started.json"))
    assert len(starts) == 1
    final = json.loads(starts[0].with_name(starts[0].name.replace(".started", "")).read_text())
    manifest = json.loads((root / "manifest.json").read_text())
    assert final["passed"] is False and final["errors"]
    assert manifest["runs"] == [final]
    assert len(manifest["reports"]) == 1 and manifest["reports"][0]["passed"] is False
    assert len(checks) == 2, "post-check must run even after an early preflight failure"


def capture_fixture(unit, phase):
    return {"schema_version":1, "phase":phase, "unit":unit,
            "invocation_id":"b" * 32, "control_group":"/fixture/app.slice/" + unit,
            "effective":{"memory.max":"2147483648","memory.swap.max":"0","pids.max":"256",
                         "cpu.max":"200000 100000"},
            "memory_events":{"oom":0,"oom_kill":0},"pids_events":{"max":0},
            "memory_peak_bytes":12345,"pids_peak":12,"cpu":{"usage_usec":1000}}


@pytest.mark.parametrize("fault", [None,"missing", "identity", "phase", "swap", "boolean_swap", "denial", "oom", "missing_counter", "traversal"])
def test_effective_resources_require_matching_complete_captures(tmp_path, monkeypatch, fault):
    root = initialize_private_campaign(tmp_path, monkeypatch)
    unit = "hermes-zvec-stress-" + "a" * 32 + ".service"
    start, stop = capture_fixture(unit, "start"), capture_fixture(unit, "stop")
    if fault == "identity": stop["invocation_id"] = "c" * 32
    if fault == "phase": stop["phase"] = "start"
    if fault == "swap": stop["effective"]["memory.swap.max"] = "max"
    if fault == "boolean_swap": stop["effective"]["memory.swap.max"] = False
    if fault == "denial": stop["pids_events"]["max"] = 1
    if fault == "oom": stop["memory_events"]["oom_kill"] = 1
    if fault == "missing_counter": del stop["pids_events"]["max"]
    if fault == "traversal": start["control_group"] = "/fixture/../app.slice/" + unit
    for phase, record in (("start", start), ("stop", stop)):
        if phase == "stop" and fault == "missing": continue
        (root / (unit[:-8] + f".{phase}.limits.json")).write_text(json.dumps(record))
    limits = {"memory_bytes":2147483648,"swap_bytes":0,"cpu_quota_percent":200,"tasks":256,"runtime_seconds":30}
    if fault is None:
        first, last = campaign.read_capture(unit, "start"), campaign.read_capture(unit, "stop")
        assert campaign.verify_resources(first, last, limits) is True
    else:
        try:
            first, last = campaign.read_capture(unit, "start"), campaign.read_capture(unit, "stop")
        except (ValueError, OSError): pass
        else: assert campaign.verify_resources(first, last, limits) is False


@pytest.mark.parametrize("state", ["empty", "populated", "reused_identity", "missing_population", "stop_failure"])
def test_cleanup_uses_recorded_group_and_rechecks_unit_identity(tmp_path, monkeypatch, state):
    root = initialize_private_campaign(tmp_path, monkeypatch)
    mount = tmp_path / "cgroup"
    monkeypatch.setattr(campaign, "CGROUP_ROOT", mount, raising=False)
    unit = "hermes-zvec-stress-" + "a" * 32 + ".service"
    start = capture_fixture(unit, "start")
    group = mount / start["control_group"].lstrip("/")
    group.mkdir(parents=True)
    if state != "missing_population":
        (group / "cgroup.events").write_text("populated " + ("0" if state == "empty" else "1") + "\n")
    current = {"LoadState":"loaded", "ActiveState":"active", "ControlGroup":start["control_group"],
               "InvocationID": "c"*32 if state == "reused_identity" else start["invocation_id"]}
    monkeypatch.setattr(campaign, "unit_state", lambda name: current, raising=False)
    stopped = []
    def stop(command, **kwargs):
        stopped.append(command)
        if state == "stop_failure": raise TimeoutError("stop failed")
        (group / "cgroup.events").write_text("populated 0\n")
        return subprocess.CompletedProcess(command, 0)
    monkeypatch.setattr(campaign.subprocess, "run", stop)
    if state in {"empty", "populated"}:
        assert campaign.cleanup_unit(unit, start) is True
        assert len(stopped) == (1 if state == "populated" else 0)
    else:
        with pytest.raises((RuntimeError, OSError, TimeoutError)):
            campaign.cleanup_unit(unit, start)
        if state in {"reused_identity", "missing_population"}: assert not stopped


@pytest.mark.parametrize("fault", [None, "missing_start", "bad_stop", "swap", "unit_reuse", "cleanup", "report_json", "report_coverage", "timeout", "log_read", "post_guard"])
def test_execute_integrates_real_receipt_validation(tmp_path, monkeypatch, fault):
    """Synthetic kernel/workload files test the real controller, not capacity."""
    source = tmp_path / "source"
    source.mkdir()
    monkeypatch.setattr(campaign, "ROOT", source)
    root = initialize_private_campaign(tmp_path, monkeypatch)
    mount = tmp_path / "cgroup"
    monkeypatch.setattr(campaign, "CGROUP_ROOT", mount)
    calls = []
    checks = []
    state = {"LoadState":"not-found"}
    def unchanged():
        checks.append(1)
        if fault == "post_guard" and len(checks) == 2: raise OSError("post guard unavailable")
        return []
    monkeypatch.setattr(campaign, "unchanged", unchanged)
    monkeypatch.setattr(campaign, "unit_state", lambda unit: dict(state))
    monkeypatch.setattr(campaign, "output", lambda command: "/fixture" if command[0] == "systemctl" else "a"*40)
    report_path = source / ".test-tools/stress-runs/stress-fixture/report.json"
    report_path.parent.mkdir(parents=True)
    raw = {"schema_version":1, "lane":"regression", "mode":"offline", "passed":True,
           "arguments":{"iterations":1}, "planned_iterations":1, "completed_iterations":1,
           "planned_callbacks":0,"successful_callbacks":0,"elapsed_seconds":1,"workers":[],
           "iterations":[{"number":0,"exit":0,"tests":3,"skipped":0,"failures":0,"errors":0,
                          "diagnostic_errors":[],"elapsed_seconds":.5}]}
    if fault == "report_coverage": raw["iterations"][0]["tests"] = 0
    report_path.write_text("broken" if fault == "report_json" else json.dumps(raw))
    def launch(command, **kwargs):
        calls.append(command)
        assert command[0] == "systemd-run", "no unrelated process may be controlled"
        unit = next(x.removeprefix("--unit=") for x in command if x.startswith("--unit="))
        start, stop = capture_fixture(unit, "start"), capture_fixture(unit, "stop")
        group = mount / start["control_group"].lstrip("/")
        group.mkdir(parents=True)
        if fault != "cleanup": (group / "cgroup.events").write_text("populated 0\n")
        if fault == "unit_reuse": state.update(LoadState="loaded", ControlGroup=start["control_group"], InvocationID="c"*32)
        if fault == "swap": stop["effective"]["memory.swap.max"] = "max"
        for phase, value in (("start",start),("stop",stop)):
            if phase == "start" and fault == "missing_start": continue
            (root / (unit[:-8] + f".{phase}.limits.json")).write_text("broken" if phase == "stop" and fault == "bad_stop" else json.dumps(value))
        # A historical unbound capture must never rescue missing/new invalid proof.
        (root / (unit[:-8] + ".limits.json")).write_text(json.dumps(stop))
        kwargs["stdout"].write(json.dumps({"report":str(report_path),"passed":True})+"\n")
        kwargs["stdout"].flush()
        if fault == "timeout": raise subprocess.TimeoutExpired(command, 1)
        return subprocess.CompletedProcess(command, 0)
    monkeypatch.setattr(campaign.subprocess, "run", launch)
    real_open = Path.open
    def open_path(path, mode="r", *args, **kwargs):
        if fault == "log_read" and str(path).endswith(".private.log") and mode.startswith("r"):
            raise OSError("unreadable owned log")
        return real_open(path, mode, *args, **kwargs)
    monkeypatch.setattr(Path, "open", open_path)
    case = {"label":"regression-fixture","script":"stress_memory.py","args":["--lane","regression","--iterations","1"],"timeout_s":30}
    assert campaign.execute(case, "fix", 256) is (fault is None)
    manifest = json.loads((root / "manifest.json").read_text())
    final = manifest["runs"][0]
    assert len(manifest["runs"]) == len(manifest["reports"]) == len(calls) == 1
    assert len(checks) == 2
    if fault is None:
        assert final["cleanup_verified"] and final["resources_verified"] and final["production_unchanged"]
    else:
        assert final["errors"] and not manifest["reports"][0]["passed"]
    if fault == "timeout": assert final["failure_category"] == "timeout"
    if fault in {"report_json", "report_coverage"}:
        assert "path" not in manifest["reports"][0]
        assert final["report"] == str(report_path), "private failed source must remain attributable"



@pytest.mark.parametrize("fault", ["none", "manifest_final", "receipt_final"])
def test_attempt_is_published_before_work_and_finalization_cannot_hide_failure(tmp_path, monkeypatch, fault):
    root = initialize_private_campaign(tmp_path, monkeypatch)
    unit = "hermes-zvec-stress-" + "a"*32 + ".service"
    real_atomic = campaign.atomic
    publication, entered = [], []
    def atomic(path, data):
        if path.name == "manifest.json":
            publication.append(1)
            if fault == "manifest_final" and len(publication) == 2:
                raise OSError("manifest write failed")
        if fault == "receipt_final" and path.name == unit[:-8] + ".json":
            raise OSError("final receipt write failed")
        return real_atomic(path, data)
    monkeypatch.setattr(campaign, "atomic", atomic)
    def attempt():
        with campaign.attempt_record(CASE, "fix", 256, unit) as receipt:
            manifest = json.loads((root / "manifest.json").read_text())
            assert len(manifest["runs"]) == 1
            assert manifest["runs"][0]["status"] == "started"
            assert manifest["runs"][0]["passed"] is False
            entered.append(True)
            receipt["passed"] = True  # fixture models a fully checked inner result
    if fault == "none": attempt()
    else:
        with pytest.raises(OSError): attempt()
        assert entered == [True]
        assert (root / (unit[:-8] + ".started.json")).exists()
        final = root / (unit[:-8] + ".json")
        if fault == "manifest_final":
            data = json.loads(final.read_text())
            assert data["passed"] is False
            assert any("manifest" in error for error in data["errors"])
        else: assert not final.exists()


@pytest.mark.parametrize("manifest_damage", ["missing", "corrupt", "valid"])
def test_reconcile_sigkilled_controller_preserves_interrupted_attempt(tmp_path, monkeypatch, manifest_damage):
    import time
    root = initialize_private_campaign(tmp_path, monkeypatch)
    unit = "hermes-zvec-stress-" + "a"*32 + ".service"
    code = """import importlib.util,json,pathlib,signal,sys
spec=importlib.util.spec_from_file_location('campaign_crash',sys.argv[1])
m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
m.HERE=pathlib.Path(sys.argv[2])
case=json.loads(sys.argv[4])
with m.campaign_lease(), m.attempt_record(case,'fix',256,sys.argv[3]):
 (m.HERE/'entered').write_text('ready')
 signal.pause()
"""
    child = subprocess.Popen([sys.executable,"-I","-B","-c",code,str(ROOT/"scripts/run_campaign.py"),str(root),unit,json.dumps(CASE)],
                             stdout=subprocess.DEVNULL,stderr=subprocess.PIPE)
    try:
        deadline = time.monotonic() + 3
        while not (root/"entered").exists() and child.poll() is None and time.monotonic() < deadline:
            time.sleep(.01)
        assert (root/"entered").exists(), "child must enter durable lifecycle before crash injection"
        child.kill()
        child.wait(timeout=2)
    finally:
        if child.poll() is None: child.kill()
        child.wait(timeout=2)
        child.stderr.close()
    manifest = root/"manifest.json"
    if manifest_damage == "missing": manifest.unlink()
    if manifest_damage == "corrupt": manifest.write_text("retained corrupt fixture")
    monkeypatch.setattr(campaign.subprocess,"run",lambda *a,**k: pytest.fail("metadata-only orphan must not launch anything"))
    assert campaign.reconcile() == 1
    assert campaign.reconcile() == 1
    data = json.loads(manifest.read_text())
    assert len(data["runs"]) == len(data["reports"]) == 1
    final = data["runs"][0]
    assert final["passed"] is False and final["failure_category"] == "interrupted"
    assert final["cleanup_verified"] is True  # durable launch boundary was never reached
    assert final["production_unchanged"] is False  # no invented production recheck
    assert (root/(unit[:-8]+".started.json")).exists()
    assert (root/(unit[:-8]+".json")).exists()
    if manifest_damage == "corrupt":
        assert any(path.read_text() == "retained corrupt fixture" for path in root.glob("manifest.corrupt-*.json"))


def test_launch_requires_durable_boundary(tmp_path, monkeypatch):
    initialize_private_campaign(tmp_path, monkeypatch)
    monkeypatch.setattr(campaign, "unchanged", lambda: [])
    monkeypatch.setattr(campaign, "unit_state", lambda unit: {"LoadState":"not-found"})
    monkeypatch.setattr(campaign, "output", lambda command: "a"*40)
    real_atomic = campaign.atomic
    def atomic(path, data):
        if path.name.endswith(".launch.json"): raise OSError("launch record write failed")
        return real_atomic(path, data)
    monkeypatch.setattr(campaign, "atomic", atomic)
    monkeypatch.setattr(campaign.subprocess, "run", lambda *a, **k: pytest.fail("launch before durable record"))
    assert campaign.execute(CASE, "fix", 256) is False
    final = json.loads((campaign.HERE/"manifest.json").read_text())["runs"][0]
    assert not final["launch_started"] and final["cleanup_verified"]


def test_reconcile_cli_repairs_missing_manifest_without_work(tmp_path, monkeypatch):
    root = initialize_private_campaign(tmp_path, monkeypatch)
    (root/"manifest.json").unlink()
    monkeypatch.setattr(campaign.subprocess,"run",lambda *a,**k: pytest.fail("unexpected launch"))
    assert campaign.main(["--reconcile","--output-dir",str(root)]) == 0
    assert json.loads((root/"manifest.json").read_text())["runs"] == []


def test_unfinished_or_unverified_cleanup_refuses_more_load(tmp_path, monkeypatch):
    root = initialize_private_campaign(tmp_path, monkeypatch)
    unit = "hermes-zvec-stress-"+"a"*32+".service"
    with campaign.attempt_record(CASE,"fix",256,unit) as receipt:
        receipt.update(launch_started=True, cleanup_verified=False)
    monkeypatch.setattr(campaign,"unchanged",lambda: [])
    monkeypatch.setattr(campaign,"unit_state",lambda unit: pytest.fail("unresolved ownership admitted new unit"))
    assert campaign.execute(CASE,"fix",256) is False
    manifest = json.loads((root/"manifest.json").read_text())
    assert len(manifest["runs"]) == 2
    assert not any(row["passed"] for row in manifest["runs"])
