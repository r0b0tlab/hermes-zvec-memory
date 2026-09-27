#!/usr/bin/env python3
"""Serial campaign controller for the synthetic zvec-memory stress lanes.

Every case runs as its own transient user unit inside app.slice with a hard
ResourceMax profile (MemoryMax, CPUQuota, TasksMax) and KillMode=control-group,
so an escaped native grandchild is still owned by the unit. Receipts are
written to .test-tools/campaign/, appended to manifest.json, and never deleted:
a failed attempt is evidence, not an error to retry away.

The production service, its vault, and its runtime wrapper are treated as
read-only. Each case first verifies the production baseline recorded in
.test-tools/campaign/production-before.json and refuses to run if anything
about it changed.
"""
import argparse
from contextlib import contextmanager
import fcntl
import hashlib
import json
from pathlib import Path
import os
import re
import stat
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
from capture_limits import limits_match
from harness_support import provider_source, product_identity, harness_identity, host_provenance
import subprocess
import time
import tempfile
import uuid

import yaml

ROOT = Path(__file__).resolve().parents[1]
HERE = ROOT / ".test-tools/campaign"
MAX_TASKS = 4096
MIN_TASKS = 64
# Measured native peak is 138-150 concurrent tasks; 128 provably fails the native
# lanes (see docs/stress-results.md), so the default is the measured profile.
DEFAULT_TASKS = 256
CGROUP_ROOT = Path("/sys/fs/cgroup")
SCRIPTS = {"stress_memory.py", "soak_memory.py"}
BASELINE = HERE / "production-before.json"
# The Hermes CLI rewrites unrelated keys (onboarding, _config_version) in this
# file on its own; memory-relevant state is compared section-wise instead.
CONFIG_PATH = Path.home() / ".hermes/config.yaml"
WATCHED_CONFIG_SECTIONS = ("memory", "plugins")


def output(argv):
    return subprocess.check_output(argv, text=True, timeout=30).strip()


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def watched_files():
    """Production files whose bytes must not change during a campaign."""
    plugin_dir = Path.home() / ".hermes/plugins/zvec-memory"
    paths = [CONFIG_PATH,
             Path.home() / ".hermes/zvec-memory/config.json",
             Path.home() / ".local/share/hermes-zvec-memory/zg-default",
             Path.home() / ".config/systemd/user/hermes-zvec-memory.service"]
    if plugin_dir.is_dir():
        paths.extend(sorted(plugin_dir.glob("*.py")))
    return paths


def production_snapshot(revision=None, reason=None):
    parsed = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8")) or {}
    snapshot = {
        "files": {str(path): sha256(path) for path in watched_files() if Path(path).exists()},
        "service": output(["systemctl", "--user", "show", "hermes-zvec-memory.service",
                           "-p", "MainPID", "-p", "ActiveState"]),
        "provider": parsed.get("memory", {}).get("provider"),
        "memory_config": {key: parsed.get(key) for key in WATCHED_CONFIG_SECTIONS},
        "config_sha256": sha256(CONFIG_PATH),
    }
    if revision is not None:
        snapshot["baseline_revision"] = revision
    if reason:
        snapshot["revision_reason"] = reason
    return snapshot


def production_differences(baseline, current):
    """Return the memory-relevant differences between two production snapshots."""
    differences = []
    if baseline.get("provider") != current.get("provider"):
        differences.append(f"memory.provider {baseline.get('provider')!r} -> "
                           f"{current.get('provider')!r}")
    if baseline.get("service") != current.get("service"):
        differences.append(f"service state {baseline.get('service')!r} -> "
                           f"{current.get('service')!r}")
    if baseline.get("memory_config") != current.get("memory_config"):
        differences.append("watched config sections (memory/plugins) changed")
    old, new = baseline.get("files", {}), current.get("files", {})
    for path in sorted(set(old) | set(new)):
        if path == str(CONFIG_PATH):
            continue  # gated section-wise above; Hermes rewrites unrelated keys itself
        if old.get(path) != new.get(path):
            differences.append(f"watched file changed: {path}")
    return differences


def unchanged():
    baseline = json.loads(BASELINE.read_text())
    return production_differences(baseline, production_snapshot())


def atomic(path, data):
    path = Path(path)
    tmp = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                         prefix="." + path.name + ".", suffix=".tmp", delete=False) as stream:
            tmp = Path(stream.name)
            json.dump(data, stream, indent=2, allow_nan=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if tmp is not None:
            tmp.unlink(missing_ok=True)


def group_empty(group):
    group = Path(group)
    try:
        values = dict(
            line.split()
            for line in (group / "cgroup.events").read_text().splitlines()
        )
    except FileNotFoundError:
        if not group.exists():
            return True
        raise RuntimeError("cgroup exists but population evidence is unavailable")
    if values.get("populated") not in {"0", "1"}:
        raise RuntimeError("invalid recursive cgroup population evidence")
    return values["populated"] == "0"


def unit_state(unit):
    text = output(["systemctl", "--user", "show", unit, "-p", "LoadState", "-p", "ActiveState",
                   "-p", "ControlGroup", "-p", "InvocationID", "-p", "Job"])
    result = dict(line.split("=", 1) for line in text.splitlines() if "=" in line)
    if result.get("LoadState") not in {"loaded", "not-found"}:
        raise RuntimeError("unit state unavailable")
    return result


def read_capture(unit, phase):
    path = HERE / (unit.removesuffix(".service") + f".{phase}.limits.json")
    if path.is_symlink():
        raise ValueError("aliased resource capture")
    data = json.loads(path.read_text())
    if (not isinstance(data, dict) or type(data.get("schema_version")) is not int
            or data["schema_version"] != 1 or data.get("phase") != phase or data.get("unit") != unit
            or not isinstance(data.get("invocation_id"), str)
            or not re.fullmatch(r"[a-f0-9]{32}", data["invocation_id"])):
        raise ValueError("invalid resource capture identity")
    group = data.get("control_group")
    if (not isinstance(group, str) or not group.startswith("/") or ".." in group.split("/")
            or str(Path(group)) != group or Path(group).name != unit):
        raise ValueError("invalid captured cgroup")
    return data


def verify_resources(start, stop, requested):
    try:
        if any(start[key] != stop[key] for key in ("unit", "invocation_id", "control_group")):
            return False
        for data, phase in ((start, "start"), (stop, "stop")):
            if data["phase"] != phase:
                return False
            effective = data["effective"]
            if any(not isinstance(effective[key], str) for key in
                   ("memory.max", "memory.swap.max", "pids.max", "cpu.max")):
                return False
            if not limits_match(effective, requested):
                return False
            for section, key in (("memory_events", "oom"), ("memory_events", "oom_kill"), ("pids_events", "max")):
                value = data[section][key]
                if type(value) is not int or value != 0:
                    return False
        return True
    except (KeyError, TypeError, ValueError):
        return False


def cleanup_unit(unit, capture):
    group = CGROUP_ROOT / capture["control_group"].lstrip("/")
    def same_identity():
        current = unit_state(unit)
        if current["LoadState"] == "not-found":
            return current
        if (current.get("InvocationID") != capture["invocation_id"]
                or current.get("ControlGroup") not in {capture["control_group"], ""}):
            raise RuntimeError("owned unit identity changed; refusing stop")
        return current
    current = same_identity()
    if current["LoadState"] == "loaded":
        # Empty during activation is not completion: cancel pending ExecStart
        # even when the collector is the last process to have left the group.
        result = subprocess.run(["systemctl", "--user", "stop", unit], timeout=25,
                                capture_output=True, text=True)
        if result.returncode:
            raise RuntimeError("owned unit stop failed")
        current = same_identity()
    if current["LoadState"] != "not-found" and (current.get("ActiveState") not in {"inactive", "failed"}
                                                or current.get("Job") not in {"", "0"}):
        raise RuntimeError("owned unit is not quiescent")
    return group_empty(group)


def workload_environment(unit, selected_source=None):
    root = HERE / (unit.removesuffix(".service") + ".work")
    home = root / "home"
    return {"ZVEC_CAMPAIGN_ATTEMPT_ID":unit.removesuffix(".service"),
            "PATH":"/usr/bin:/bin", "HOME":str(home), "HERMES_HOME":str(home / "hermes"),
            "HERMES_AGENT_DIR":os.environ.get("HERMES_AGENT_DIR", str(Path.home()/".hermes/hermes-agent")),
            "XDG_CONFIG_HOME":str(home / "config"), "XDG_DATA_HOME":str(home / "data"),
            "XDG_CACHE_HOME":str(home / "cache"), "XDG_STATE_HOME":str(home / "state"),
            "TMPDIR":str(root / "tmp"), "ZVEC_TEST_ROOT":str(root),
            "HERMES_ZVEC_RUNTIME_DIR":str(root / "runtime"), "HERMES_ZVEC_MODEL_CACHE":str(root / "models"),
            "ZVEC_GREP_HOME":str(root / "zg-state"), "ZVEC_GREP_MODEL_CACHE":str(ROOT / ".test-tools/models"),
            "ZVEC_TEST_MODEL_CACHE":str(ROOT / ".test-tools/models"),
            "ZVEC_TEST_NODE_MODULES":str(ROOT / ".test-tools/node_modules"),
            "ZVEC_TEST_PROVIDER_ROOT":str(selected_source if selected_source is not None else os.environ.get("ZVEC_TEST_PROVIDER_ROOT", ROOT)),
            "HF_HUB_OFFLINE":"1", "TRANSFORMERS_OFFLINE":"1", "PYTHONDONTWRITEBYTECODE":"1",
            "PYTEST_DISABLE_PLUGIN_AUTOLOAD":"1", "LANG":"C.UTF-8", "TZ":"UTC"}


def build_command(case, unit, tasks):
    """Return (systemd-run argv, limits receipt) for one case."""
    assert case["script"] in SCRIPTS, case["script"]
    assert all(isinstance(x, str) for x in case["args"])
    assert unit.endswith(".service"), unit
    limit = int(case["timeout_s"])
    env = workload_environment(unit, case.get("provider_source"))
    def capture_command(phase):
        path = HERE / (unit.removesuffix(".service") + f".{phase}.limits.json")
        # systemd Exec*= uses its own quoting and specifiers, not a shell.
        def quoted(value):
            return json.dumps(str(value).replace("%", "%%").replace("$", "$$"))
        return " ".join([quoted(ROOT / ".venv/bin/python"), "-I", "-B",
                         quoted(ROOT / "scripts/capture_limits.py"), quoted(path),
                         "--unit", unit, "--phase", phase, "--tasks", str(tasks)])
    command = ["systemd-run", "--user", "--unit=" + unit, "--slice=app.slice",
               "--service-type=exec", "--wait", "--pipe", "-p", "MemoryMax=2G",
               "-p", "MemorySwapMax=0", "-p", "CPUQuota=200%",
               "-p", "CPUQuotaPeriodSec=100ms", "-p", "TasksMax=" + str(tasks),
               "-p", "KillMode=control-group", "-p", "NoNewPrivileges=yes", "-p", "Restart=no",
               "-p", "UnsetEnvironment=LD_PRELOAD LD_LIBRARY_PATH LD_AUDIT LD_DEBUG LD_DEBUG_OUTPUT LD_PROFILE GCONV_PATH LOCPATH PYTHONPATH PYTHONHOME PYTHONSTARTUP NODE_OPTIONS BASH_ENV ENV",
               "-p", "MemoryAccounting=yes", "-p", "CPUAccounting=yes",
               "-p", "ExecStartPre=" + capture_command("start"),
               "-p", "ExecStopPost=" + capture_command("stop"),
               "-p", "RuntimeMaxSec=" + str(limit), "-p", "TimeoutStartSec=20", "-p", "TimeoutStopSec=10",
               "--working-directory=" + str(ROOT), "/usr/bin/env", "-i",
               *[f"{key}={value}" for key,value in sorted(env.items())],
               "/usr/bin/unshare", "--user", "--map-root-user", "--net", "--",
               "/bin/sh", "-ec", '/usr/bin/ip link set lo up; exec /usr/bin/env -u PWD -u SHLVL -u _ "$@"', "zvec-isolated",
               str(ROOT / ".venv/bin/python"), "-I", "-B",
               str(ROOT / "scripts" / case["script"]), *case["args"]]
    limits = {"memory_bytes": 2147483648, "swap_bytes": 0, "cpu_quota_percent": 200,
              "tasks": int(tasks), "runtime_seconds": limit}
    return command, limits


def manifest_entry(case, tasks, cgroup, report=None, exit_code=None, source_sha=""):
    """Return the privacy-allowlisted manifest entry for one attempt."""
    entry = {"tags": {"scenario": case["label"], "variant": case.get("variant", "fix"),
                      "environment": f"2G-200percent-{int(tasks)}tasks"},
             "passed": False}
    if report:
        entry["path"] = str(report)
    else:
        entry["failure_category"] = "timeout" if exit_code == 124 else "child_exit"
        entry["metadata"] = {"source_sha": source_sha}
    metrics = {}
    for source, target in (("memory_peak_bytes", "cgroup_peak_bytes"),
                           ("swap_peak_bytes", "cgroup_swap_peak_bytes"),
                           ("pids_peak", "cgroup_pids_peak")):
        value = cgroup.get(source)
        if isinstance(value, (int, float)):
            metrics[target] = [value]
    for source, target in (("usage_usec", "cgroup_cpu_seconds"),
                           ("throttled_usec", "cgroup_throttled_seconds")):
        value = cgroup.get("cpu", {}).get(source)
        if isinstance(value, (int, float)):
            metrics[target] = [value / 1000000]
    oom = cgroup.get("memory_events", {}).get("oom_kill")
    if isinstance(oom, int):
        metrics["cgroup_oom_kills"] = [oom]
        if oom:
            entry["failure_category"] = "oom"
    entry.setdefault("metadata", {})["metrics"] = metrics
    return entry


@contextmanager
def locked_file(name, require_manifest=True):
    select_output(HERE, require_manifest=require_manifest)
    fd = os.open(HERE / name, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError("another campaign controller owns this lease") from exc
        yield
    finally:
        os.close(fd)


@contextmanager
def campaign_lease(require_manifest=True):
    with locked_file(".controller.lock", require_manifest=require_manifest):
        yield


def attempt_entry(receipt):
    entry = manifest_entry(receipt["case"], receipt["tasks"], receipt.get("cgroup", {}),
                           receipt.get("report") if receipt.get("report_validated") else None, receipt.get("exit_code"), receipt.get("source_sha", ""))
    for key in ("product_identity", "harness_identity", "host_provenance"):
        if key in receipt:
            entry["metadata"][key] = receipt[key]
    entry["attempt_id"] = receipt["attempt_id"]
    entry["tags"]["variant"] = receipt["variant"]
    entry["passed"] = receipt["passed"] is True
    if not entry["passed"]:
        entry["failure_category"] = receipt.get("failure_category", entry.get("failure_category", "other"))
    return entry


def publish_attempt(receipt):
    entry = attempt_entry(receipt)
    with locked_file(".manifest.lock"):
        path = HERE / "manifest.json"
        manifest = json.loads(path.read_text())
        for key, value in (("runs", receipt), ("reports", entry)):
            manifest[key] = [row for row in manifest[key] if row.get("attempt_id") != receipt["attempt_id"]]
            manifest[key].append(value)
        manifest["status"] = "running" if receipt["passed"] or receipt["status"] == "started" else "stopped_on_failure"
        publish_manifest(manifest)


def invalidate_manifest():
    # Export reads this file directly, not the final receipts. A failed fsync
    # can follow a successful replace, so leaving the old pathname is unsafe.
    (HERE / "manifest.json").unlink(missing_ok=True)
    directory = os.open(HERE, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def publish_manifest(manifest):
    try:
        atomic(HERE / "manifest.json", manifest)
    except BaseException as exc:
        try:
            invalidate_manifest()
        except BaseException as invalidation:
            exc.add_note("manifest invalidation failed: " + type(invalidation).__name__)
        raise


@contextmanager
def attempt_record(case, variant, tasks, unit):
    select_output(HERE)
    if not re.fullmatch(r"hermes-zvec-stress-[a-f0-9]{32}\.service", unit):
        raise ValueError("invalid owned attempt unit")
    stamp = unit.removesuffix(".service")
    receipt = {"schema_version": 2, "attempt_id": stamp, "unit": unit,
               "label": case["label"], "case": dict(case), "variant": variant,
               "tasks": tasks, "passed": False, "status": "started", "errors": [],
               "cleanup_verified": False, "production_unchanged": False,
               "resources_verified": False, "started_unix_ns": time.time_ns(),
               "failure_category": "unfinished"}
    with (HERE / (stamp + ".started.json")).open("x", encoding="utf-8") as stream:
        json.dump(receipt, stream, indent=2, allow_nan=False)
        stream.flush()
        os.fsync(stream.fileno())
    directory = os.open(HERE, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)
    try:
        publish_attempt(receipt)
        yield receipt
    except BaseException as exc:
        receipt["passed"] = False
        receipt["failure_category"] = "interrupted" if isinstance(exc, (KeyboardInterrupt, SystemExit)) else "controller_exception"
        receipt["errors"].append("controller:" + type(exc).__name__)
        raise
    finally:
        receipt["status"] = "final"
        receipt["finished_unix_ns"] = time.time_ns()
        receipt["passed"] = receipt["passed"] is True and not receipt["errors"]
        pending = HERE / (stamp + ".finalizing.json")
        try:
            # Persist a conservative fallback before exposing any success. If
            # failed-receipt repair also fails, reconciliation must not revive
            # an unconfirmed green final receipt.
            atomic(pending, {**receipt, "passed": False,
                             "errors": [*receipt["errors"], "manifest_finalization:unconfirmed"]})
            atomic(HERE / (stamp + ".json"), receipt)
            publish_attempt(receipt)
            # An unsynced removal can only resurrect the failed fallback after
            # a crash; it cannot resurrect success. No green data follows it.
            pending.unlink()
        except BaseException as exc:
            receipt["passed"] = False
            receipt["errors"].append("manifest_finalization:" + type(exc).__name__)
            try:
                invalidate_manifest()
            except BaseException as invalidation:
                exc.add_note("manifest invalidation failed: " + type(invalidation).__name__)
            try:
                atomic(HERE / (stamp + ".json"), receipt)
            except BaseException as repair:
                exc.add_note("failed receipt repair: " + type(repair).__name__)
            raise


def execute(case, variant, tasks):
    with campaign_lease():
        unit = "hermes-zvec-stress-" + uuid.uuid4().hex + ".service"
        with attempt_record(case, variant, tasks, unit) as receipt:
            start = time.monotonic()
            try:
                execute_body(case, variant, tasks, receipt)
            except (KeyboardInterrupt, SystemExit):
                raise
            except Exception as exc:
                receipt["errors"].append("execute:" + type(exc).__name__)
                receipt["failure_category"] = "controller_exception"
            finally:
                receipt["elapsed_seconds"] = time.monotonic() - start
                try:
                    differences = unchanged()
                    if receipt.get("baseline_sha256") is not None and sha256(BASELINE) != receipt["baseline_sha256"]:
                        differences = [*differences, "production baseline changed during attempt"]
                    receipt["production_unchanged"] = not differences
                    if differences:
                        receipt["production_differences"] = differences
                        receipt["errors"].append("production_changed")
                except BaseException as exc:
                    receipt["production_unchanged"] = False
                    receipt["errors"].append("production_check:" + type(exc).__name__)
            receipt["passed"] = bool(receipt.get("workload_passed") and receipt["cleanup_verified"]
                                     and receipt["production_unchanged"] and not receipt["errors"])
        print(json.dumps({"case": case["label"], "passed": receipt["passed"],
                          "cleanup_verified": receipt["cleanup_verified"],
                          "elapsed_seconds": receipt["elapsed_seconds"]}), flush=True)
        return receipt["passed"]


def validate_workload_binding(case, receipt, path, raw):
    """Bind evidence to the owned attempt and the same parser as its producer."""
    if case["script"] == "stress_memory.py":
        import stress_memory as workload
        prefix = "stress"
    else:
        import soak_memory as workload
        prefix = "soak"
    expected_path = ROOT / ".test-tools" / (prefix + "-runs") / (prefix + "-" + receipt["attempt_id"]) / "report.json"
    if path != expected_path.absolute() or path.resolve() != expected_path.absolute():
        raise ValueError("workload report is not owned by this attempt")
    if raw.get("attempt_id") != receipt["attempt_id"] or raw.get("run") != str(path.parent):
        raise ValueError("workload attempt identity mismatch")
    parser = workload.argument_parser()
    parser.set_defaults(provider_source=str(provider_source(ROOT, case.get("provider_source"))))
    expected = vars(parser.parse_args(case["args"]))
    if prefix == "stress":
        if expected["lane"] == "native-regression": expected["mode"] = "native"
        expected["corpus_seed"] = workload.CORPUS_SEED
        if raw.get("lane") != expected["lane"] or raw.get("mode") != expected["mode"]:
            raise ValueError("workload lane or mode mismatch")
    else:
        expected.pop("worker_run")
        if raw.get("lane") != "long_lived_soak" or raw.get("transport") != "native_server_only":
            raise ValueError("workload lane or transport mismatch")
    expected = {key: str(value) if isinstance(value, Path) else value for key, value in expected.items()}
    if raw.get("arguments") != expected:
        raise ValueError("reported workload differs from requested arguments")


def execute_body(case, variant, tasks, receipt):
    receipt["launch_started"] = False
    receipt["cleanup_verified"] = True  # no process exists until launch admission
    baseline_bytes = BASELINE.read_bytes()
    receipt["baseline_revision"] = json.loads(baseline_bytes).get("baseline_revision")
    receipt["baseline_sha256"] = hashlib.sha256(baseline_bytes).hexdigest()
    differences = unchanged()
    if differences:
        raise RuntimeError("production baseline changed")
    manifest = json.loads((HERE / "manifest.json").read_text())
    previous = durable_attempts()
    if any(HERE.glob("*.finalizing.json")):
        raise RuntimeError("uncertain publication requires explicit reconciliation")
    for key, expected in (("runs", previous), ("reports", [attempt_entry(row) for row in previous])):
        rows = manifest[key]
        if (len(rows) != len(expected) or
                {row["attempt_id"]: row for row in rows} != {row["attempt_id"]: row for row in expected}):
            raise RuntimeError("incomplete attempt inventory requires explicit reconciliation")
    if any(row.get("attempt_id") != receipt["attempt_id"] and
           (row.get("status") == "started" or
            (row.get("launch_started") and row.get("cleanup_verified") is not True)) for row in previous):
        raise RuntimeError("unfinished ownership requires explicit reconciliation")
    unit = receipt["unit"]
    if unit_state(unit)["LoadState"] != "not-found":
        raise RuntimeError("refusing an existing attempt unit")
    logfile = HERE / (receipt["attempt_id"] + ".private.log")
    env = workload_environment(unit, case.get("provider_source"))
    private = Path(env["ZVEC_TEST_ROOT"])
    private.mkdir(mode=0o700)
    for key in ("HOME", "HERMES_HOME", "XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_CACHE_HOME", "XDG_STATE_HOME",
                "TMPDIR", "HERMES_ZVEC_RUNTIME_DIR", "HERMES_ZVEC_MODEL_CACHE", "ZVEC_GREP_HOME"):
        Path(env[key]).mkdir(parents=True, exist_ok=True)
    command, limits = build_command(case, unit, tasks)
    receipt["limits"] = limits
    receipt["product_identity"] = product_identity(provider_source(ROOT, case.get("provider_source")))
    receipt["harness_identity"] = harness_identity(ROOT)
    receipt["host_provenance"] = host_provenance(Path(env["HERMES_AGENT_DIR"]))
    receipt["source_sha"] = receipt["harness_identity"].get("git_sha")
    print("START " + case["label"] + " " + unit + " tasks=" + str(tasks), flush=True)
    cancelled = None
    try:
        with logfile.open("x") as stream:
            atomic(HERE / (receipt["attempt_id"] + ".launch.json"), {**receipt, "launch_started": True})
            receipt["launch_started"] = True
            receipt["cleanup_verified"] = False
            result = subprocess.run(command, stdout=stream, stderr=subprocess.STDOUT,
                                    timeout=limits["runtime_seconds"] + 45, text=True)
            receipt["exit_code"] = result.returncode
            if result.returncode:
                receipt["failure_category"] = "child_exit"
                receipt["errors"].append("child_nonzero_exit")
    except subprocess.TimeoutExpired:
        receipt["exit_code"] = 124
        receipt["failure_category"] = "timeout"
        receipt["errors"].append("whole_command_timeout")
    finally:
        first = None
        if receipt["launch_started"]:
            try:
                first = read_capture(unit, "start")
                receipt["start_capture"] = first
                receipt["cleanup_verified"] = cleanup_unit(unit, first)
                if not receipt["cleanup_verified"]:
                    receipt["errors"].append("owned_cgroup_not_empty")
            except BaseException as exc:
                receipt["cleanup_verified"] = False
                receipt["errors"].append("cleanup:" + type(exc).__name__)
                if isinstance(exc, (KeyboardInterrupt, SystemExit)): cancelled = exc
            try:
                last = read_capture(unit, "stop")
                receipt["cgroup"] = last
                receipt["resources_verified"] = first is not None and verify_resources(first, last, limits)
                if not receipt["resources_verified"]:
                    receipt["errors"].append("effective_resources_unverified")
            except BaseException as exc:
                receipt["resources_verified"] = False
                receipt["errors"].append("resource_capture:" + type(exc).__name__)
                if isinstance(exc, (KeyboardInterrupt, SystemExit)): cancelled = exc
    if cancelled is not None:
        raise cancelled
    pointer = None
    for line in logfile.read_text().splitlines():
        try:
            value = json.loads(line)
        except ValueError:
            continue
        if isinstance(value, dict) and "report" in value:
            if pointer is not None:
                raise ValueError("ambiguous workload report")
            pointer = value
    if pointer is None:
        raise ValueError("missing workload report")
    path = Path(pointer["report"]).resolve()
    if path.name != "report.json" or not path.is_relative_to((ROOT / ".test-tools").resolve()):
        raise ValueError("report escaped test artifacts")
    receipt["report"] = str(path)
    from report_stress import adapt_report, summarize
    raw = json.loads(path.read_text())
    validate_workload_binding(case, receipt, path, raw)
    valid = summarize(adapt_report(raw), 1)
    for key in ("product_identity", "harness_identity", "host_provenance"):
        if valid.get(key, {}).get("content_sha256") != receipt[key]["content_sha256"]:
            raise ValueError("workload source fingerprint mismatch")
    receipt["report_validated"] = True
    receipt["harness_passed"] = pointer.get("passed") is True and valid["passed"] is True
    if not receipt["harness_passed"]:
        receipt["errors"].append("workload_coverage_failed")
    receipt["workload_passed"] = (receipt.get("exit_code") == 0 and receipt["harness_passed"]
                                   and receipt["resources_verified"])




def load_cases(path=None):
    data = json.loads(Path(path or ROOT / "scripts/campaign_cases.json").read_text())
    if not isinstance(data, dict) or type(data.get("schema_version")) is not int or data["schema_version"] != 1:
        raise ValueError("invalid campaign definitions")
    if not isinstance(data.get("cases"), list) or not data["cases"]:
        raise ValueError("empty campaign definitions")
    result = {}
    for case in data["cases"]:
        if not isinstance(case, dict):
            raise ValueError("invalid case")
        label = case.get("label")
        if not isinstance(label, str) or not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,79}", label):
            raise ValueError("invalid case label")
        if label in result:
            raise ValueError("duplicate case label")
        if case.get("script") not in SCRIPTS:
            raise ValueError("unsupported script")
        args = case.get("args")
        if not isinstance(args, list) or not all(isinstance(arg, str) and "\0" not in arg for arg in args):
            raise ValueError("invalid arguments")
        timeout = case.get("timeout_s")
        if type(timeout) is not int or not 1 <= timeout <= 2400:
            raise ValueError("invalid timeout")
        required = case.get("prerequisites")
        if (not isinstance(required, list) or not required
                or not all(isinstance(key, str) and key in {"python", "host", "engine", "models"} for key in required)
                or len(required) != len(set(required))):
            raise ValueError("invalid prerequisites")
        result[label] = case
    return result


def select_output(path, initialize=False, require_manifest=True):
    candidate = Path(path).expanduser().absolute()
    if candidate.is_symlink() or candidate.resolve() != candidate:
        raise ValueError("aliased campaign output")
    owner = {"kind": "zvec-campaign", "version": 1, "repo": str(ROOT)}
    if initialize:
        candidate.mkdir(mode=0o700, parents=True, exist_ok=False)
        atomic(candidate / "OWNER.json", owner)
        atomic(candidate / "manifest.json", {"schema_version": 1, "runs": [], "reports": [], "status": "initialized"})
    info = candidate.stat()
    if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
        raise ValueError("campaign output must be private and owned")
    for name in ("OWNER.json", "manifest.json"):
        if (candidate / name).is_symlink():
            raise ValueError("aliased campaign metadata")
    if json.loads((candidate / "OWNER.json").read_text()) != owner:
        raise ValueError("campaign owner mismatch")
    if not require_manifest:
        return candidate
    manifest = json.loads((candidate / "manifest.json").read_text())
    if not isinstance(manifest, dict) or any(not isinstance(manifest.get(key), list) for key in ("runs", "reports")):
        raise ValueError("invalid campaign manifest")
    return candidate


def durable_attempts():
    """Read the complete owned inventory, independently of the manifest cache."""
    for path in HERE.glob("hermes-zvec-stress-*.json"):
        match = re.fullmatch(r"(hermes-zvec-stress-[a-f0-9]{32})(?:\.(?:launch|finalizing))?\.json", path.name)
        if match and not (HERE / (match[1] + ".started.json")).is_file():
            raise ValueError("attempt inventory has a record without its started identity")
    records = []
    for path in sorted(HERE.glob("*.started.json")):
        stem = path.name.removesuffix(".started.json")
        unit = stem + ".service"
        if not re.fullmatch(r"hermes-zvec-stress-[a-f0-9]{32}\.service", unit) or path.is_symlink():
            raise ValueError("unattributable started record")
        initial = json.loads(path.read_text())
        if (not isinstance(initial, dict) or initial.get("attempt_id") != stem
                or initial.get("unit") != unit or initial.get("status") != "started"
                or type(initial.get("schema_version")) is not int
                or initial["schema_version"] not in {1, 2}
                or initial.get("variant") not in {"baseline", "fix"}
                or type(initial.get("tasks")) is not int or not MIN_TASKS <= initial["tasks"] <= MAX_TASKS
                or not isinstance(initial.get("case"), dict)
                or initial["case"].get("label") != initial.get("label")):
            raise ValueError("invalid started record")
        receipt = dict(initial)
        for suffix, phase in ((".launch.json", "launch"), (".json", "final"), (".finalizing.json", "pending")):
            record_path = HERE / (stem + suffix)
            if record_path.is_symlink():
                raise ValueError("aliased attempt record")
            if not record_path.exists():
                continue
            record = json.loads(record_path.read_text())
            if (not isinstance(record, dict)
                    or any(record.get(k) != initial[k] for k in ("attempt_id", "unit", "case", "variant", "tasks"))
                    or (phase == "launch" and record.get("launch_started") is not True)
                    or (phase in {"final", "pending"} and (record.get("status") != "final" or type(record.get("passed")) is not bool))):
                raise ValueError("unattributable " + phase + " record")
            if phase == "pending":
                if receipt.get("status") != "final":
                    receipt.update(record)
                receipt.update(status="final", passed=False,
                               errors=[*receipt.get("errors", []),
                                       *[error for error in record["errors"] if error not in receipt.get("errors", [])]])
            else:
                receipt.update(record)
        records.append(receipt)
    return records


def reconcile():
    """Recover accounting from owned records; never rerun or green an orphan."""
    with campaign_lease(require_manifest=False):
        records = durable_attempts()
        for receipt in records:
            launched = receipt.get("launch_started") is True
            needs_write = receipt["status"] != "final" or (HERE / (receipt["attempt_id"] + ".finalizing.json")).exists()
            if receipt["status"] != "final":
                receipt.update(status="final", passed=False, failure_category="interrupted",
                               errors=[*receipt.get("errors", []), "unfinished_controller_attempt"], production_unchanged=False,
                               resources_verified=False, finished_unix_ns=time.time_ns(),
                               cleanup_verified=receipt["schema_version"] == 2 and not launched)
            if launched and receipt.get("cleanup_verified") is not True:
                # Cleanup recovery discharges ownership, not the failed attempt.
                receipt["passed"] = False
                receipt["cleanup_verified"] = False
                needs_write = True
                try:
                    first = read_capture(receipt["unit"], "start")
                    receipt["start_capture"] = first
                    receipt["cleanup_verified"] = cleanup_unit(receipt["unit"], first)
                except BaseException as exc:
                    receipt["errors"].append("reconcile_cleanup:" + type(exc).__name__)
                    if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                        atomic(HERE / (receipt["attempt_id"] + ".json"), receipt)
                        raise
            if needs_write:
                atomic(HERE / (receipt["attempt_id"] + ".json"), receipt)
        # Never expose an empty or partially reconstructed inventory, even if
        # interrupted immediately after replacement. Durable records win over
        # stale cached rows, including previously failed attempts.
        with locked_file(".manifest.lock", require_manifest=False):
            path = HERE / "manifest.json"
            try:
                current = json.loads(path.read_text())
                if not isinstance(current, dict) or any(not isinstance(current.get(k), list) for k in ("runs", "reports")):
                    raise ValueError("invalid manifest")
            except (FileNotFoundError, ValueError):
                if path.exists():
                    backup = HERE / ("manifest.corrupt-" + uuid.uuid4().hex + ".json")
                    with backup.open("xb") as stream:
                        stream.write(path.read_bytes())
                        stream.flush()
                        os.fsync(stream.fileno())
                current = {"schema_version": 1, "runs": [], "reports": []}
            owned = {row["attempt_id"] for row in records}
            if any(not isinstance(row, dict) or row.get("attempt_id") not in owned
                   for key in ("runs", "reports") for row in current[key]):
                raise ValueError("manifest inventory has unattributable attempts; refusing to discard evidence")
            current.update(runs=records, reports=[attempt_entry(row) for row in records],
                           status="stopped_on_failure" if any(row["passed"] is not True for row in records) else "reconciled")
            publish_manifest(current)
        for receipt in records:
            (HERE / (receipt["attempt_id"] + ".finalizing.json")).unlink(missing_ok=True)
        return int(any(row["passed"] is not True for row in records))


def rebaseline(reason):
    """Write a new production baseline, preserving the previous revision."""
    with campaign_lease():
        previous = json.loads(BASELINE.read_text()) if BASELINE.exists() else {}
        revision = int(previous.get("baseline_revision", 0)) + 1
        if previous:
            (HERE / f"production-before-rev{previous.get('baseline_revision', 0)}.json").write_text(
                json.dumps(previous, indent=2), encoding="utf-8")
        snapshot = production_snapshot(revision=revision, reason=reason)
        atomic(BASELINE, snapshot)
        differences = production_differences(previous, snapshot) if previous else []
        print(json.dumps({"baseline_revision": revision, "reason": reason,
                          "differences_from_previous": differences,
                          "config_sha256": snapshot["config_sha256"],
                          "watched_files": len(snapshot["files"])}, indent=2))
    return 0


def main(argv=None):
    global HERE, BASELINE
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--case", action="append", default=[])
    parser.add_argument("--variant", choices=["baseline", "fix"], default="fix")
    parser.add_argument("--tasks", type=int, choices=range(MIN_TASKS, MAX_TASKS + 1), metavar="64..4096",
                        default=DEFAULT_TASKS, help="cgroup task ceiling (64..4096)")
    parser.add_argument("--list", action="store_true")
    parser.add_argument("--init", action="store_true")
    parser.add_argument("--reconcile", action="store_true")
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--snapshot-production", action="store_true",
                        help="re-record the production baseline (requires --reason)")
    parser.add_argument("--reason", help="why the production baseline is being re-recorded")
    parser.add_argument("--provider-source", type=Path, help="explicit measured product checkout or snapshot")
    args = parser.parse_args(argv)
    try:
        cases = load_cases()
    except (OSError, ValueError, TypeError):
        parser.error("invalid or missing tracked campaign definitions")
    if sum((args.list, args.init, args.reconcile, args.snapshot_production, bool(args.case))) != 1:
        parser.error("select exactly one operation")
    if args.list:
        print("\n".join(cases))
        return 0
    if any(x not in cases for x in args.case):
        parser.error("select known --case labels (see --list)")
    if args.output_dir is None:
        parser.error("--output-dir is required")
    try:
        selected = select_output(args.output_dir, initialize=args.init, require_manifest=not args.reconcile)
    except (OSError, ValueError, TypeError):
        parser.error("missing, invalid, or already existing campaign output")
    HERE, BASELINE = selected, selected / "production-before.json"
    if args.init:
        return 0
    if args.reconcile:
        return reconcile()
    if args.snapshot_production:
        if not args.reason:
            parser.error("--snapshot-production requires --reason")
        return rebaseline(args.reason)
    if not BASELINE.is_file() or BASELINE.is_symlink():
        parser.error("record an explicit production baseline before launch")
    for label in args.case:
        case = dict(cases[label])
        if args.provider_source is not None:
            case["provider_source"] = str(args.provider_source.expanduser().resolve())
        if not execute(case, args.variant, args.tasks):
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
