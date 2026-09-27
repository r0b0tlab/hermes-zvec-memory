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
import hashlib
import json
from pathlib import Path
import os
import re
import stat
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
from capture_limits import limits_match
import subprocess
import time
import uuid

import yaml

ROOT = Path(__file__).resolve().parents[1]
HERE = ROOT / ".test-tools/campaign"
MAX_TASKS = 4096
MIN_TASKS = 64
# Measured native peak is 138-150 concurrent tasks; 128 provably fails the native
# lanes (see docs/stress-results.md), so the default is the measured profile.
DEFAULT_TASKS = 256
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
        "provider": output(["hermes", "config", "get", "memory.provider"]),
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
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    tmp.replace(path)


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


def build_command(case, unit, tasks):
    """Return (systemd-run argv, limits receipt) for one case."""
    assert case["script"] in SCRIPTS, case["script"]
    assert all(isinstance(x, str) for x in case["args"])
    assert unit.endswith(".service"), unit
    limit = int(case["timeout_s"])
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
               "-p", "KillMode=control-group",
               "-p", "MemoryAccounting=yes", "-p", "CPUAccounting=yes",
               "-p", "ExecStartPre=" + capture_command("start"),
               "-p", "ExecStopPost=" + capture_command("stop"),
               "-p", "RuntimeMaxSec=" + str(limit), "-p", "TimeoutStopSec=10",
               "--working-directory=" + str(ROOT), str(ROOT / ".venv/bin/python"),
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


def execute(case, variant, tasks):
    differences = unchanged()
    if differences:
        raise RuntimeError("Production baseline changed; refusing more load: "
                           + "; ".join(differences))
    unit = "hermes-zvec-stress-" + uuid.uuid4().hex[:12] + ".service"
    user_group = output(["systemctl", "--user", "show", "-p", "ControlGroup", "--value"])
    assert user_group.startswith("/")
    group = Path("/sys/fs/cgroup") / user_group.lstrip("/") / "app.slice" / unit
    stamp = unit.removesuffix(".service")
    logfile = HERE / (stamp + ".private.log")
    limitsfile = HERE / (stamp + ".limits.json")
    command, limits = build_command(case, unit, tasks)
    receipt = {"label": case["label"], "unit": unit, "limits": limits,
               "source_sha": output(["git", "-C", str(ROOT), "rev-parse", "HEAD"])}
    print("START " + case["label"] + " " + unit + " tasks=" + str(tasks), flush=True)
    start = time.monotonic()
    try:
        with logfile.open("w") as stream:
            result = subprocess.run(command, stdout=stream, stderr=subprocess.STDOUT,
                                    timeout=limits["runtime_seconds"] + 45, text=True)
            receipt["exit_code"] = result.returncode
    except subprocess.TimeoutExpired:
        receipt["exit_code"] = 124
    finally:
        # The exact UUID-named unit is ours; systemd owns all descendants even
        # if native grandchildren fork/reparent between /proc samples.
        if not group_empty(group):
            subprocess.run(["systemctl", "--user", "stop", unit], timeout=25,
                           capture_output=True, text=True)
        receipt["elapsed_seconds"] = time.monotonic() - start
        receipt["cleanup_verified"] = group_empty(group)
        try:
            after = unchanged()
            receipt["production_unchanged"] = not after
            if after:
                receipt["production_differences"] = after
        except Exception as exc:
            receipt["production_unchanged"] = False
            receipt["production_check_error"] = type(exc).__name__
    for line in logfile.read_text().splitlines():
        try:
            value = json.loads(line)
        except ValueError:
            continue
        if isinstance(value, dict) and "report" in value:
            path = Path(value["report"]).resolve()
            if not path.is_relative_to(ROOT / ".test-tools"):
                raise RuntimeError("Report escaped test artifacts")
            receipt["report"] = str(path)
            receipt["harness_passed"] = value.get("passed") is True
    receipt["cgroup"] = json.loads(limitsfile.read_text()) if limitsfile.exists() else {}
    receipt["passed"] = bool(receipt.get("exit_code") == 0 and receipt.get("harness_passed")
                             and receipt["cleanup_verified"] and receipt["production_unchanged"]
                             and receipt["cgroup"]
                             and not receipt["cgroup"].get("memory_events", {}).get("oom_kill", 0))
    atomic(HERE / (stamp + ".json"), receipt)
    manifest = json.loads((HERE / "manifest.json").read_text())
    manifest["runs"].append(receipt)
    entry = manifest_entry(case, tasks, receipt["cgroup"], receipt.get("report"),
                           receipt.get("exit_code"), receipt["source_sha"])
    entry["tags"]["variant"] = variant
    entry["passed"] = bool(receipt["passed"])
    manifest.setdefault("reports", []).append(entry)
    manifest["status"] = "running" if receipt["passed"] else "stopped_on_failure"
    atomic(HERE / "manifest.json", manifest)
    print(json.dumps({"case": case["label"], "passed": bool(receipt["passed"]),
                      "tasks": int(tasks),
                      "elapsed_seconds": receipt["elapsed_seconds"],
                      "cleanup_verified": receipt["cleanup_verified"],
                      "report": receipt.get("report")}), flush=True)
    return bool(receipt["passed"])


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


def select_output(path, initialize=False):
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
    manifest = json.loads((candidate / "manifest.json").read_text())
    if not isinstance(manifest, dict) or any(not isinstance(manifest.get(key), list) for key in ("runs", "reports")):
        raise ValueError("invalid campaign manifest")
    return candidate


def rebaseline(reason):
    """Write a new production baseline, preserving the previous revision."""
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
    parser.add_argument("--tasks", type=int, choices=range(MIN_TASKS, MAX_TASKS + 1),
                        default=DEFAULT_TASKS, help="cgroup task ceiling (64..4096)")
    parser.add_argument("--list", action="store_true")
    parser.add_argument("--init", action="store_true")
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--snapshot-production", action="store_true",
                        help="re-record the production baseline (requires --reason)")
    parser.add_argument("--reason", help="why the production baseline is being re-recorded")
    args = parser.parse_args(argv)
    try:
        cases = load_cases()
    except (OSError, ValueError, TypeError):
        parser.error("invalid or missing tracked campaign definitions")
    if sum((args.list, args.init, args.snapshot_production, bool(args.case))) != 1:
        parser.error("select exactly one operation")
    if args.list:
        print("\n".join(cases))
        return 0
    if any(x not in cases for x in args.case):
        parser.error("select known --case labels (see --list)")
    if args.output_dir is None:
        parser.error("--output-dir is required")
    try:
        selected = select_output(args.output_dir, initialize=args.init)
    except (OSError, ValueError, TypeError):
        parser.error("missing, invalid, or already existing campaign output")
    HERE, BASELINE = selected, selected / "production-before.json"
    if args.init:
        return 0
    if args.snapshot_production:
        if not args.reason:
            parser.error("--snapshot-production requires --reason")
        return rebaseline(args.reason)
    if not BASELINE.is_file() or BASELINE.is_symlink():
        parser.error("record an explicit production baseline before launch")
    for label in args.case:
        if not execute(cases[label], args.variant, args.tasks):
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
