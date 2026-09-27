"""Stdlib campaign read authority; no production guards, snapshots or services.

Writers take controller then manifest locks. Readers use those same existing
inodes without creating files, and hold both locks through inner-receipt reads.
"""
from contextlib import contextmanager
import fcntl
import json
import os
import re
from pathlib import Path
import stat


MIN_TASKS = 64
MAX_TASKS = 4096


@contextmanager
def file_lease(path, *, read_only=False):
    flags = os.O_RDONLY if read_only else os.O_RDWR | os.O_CREAT
    fd = os.open(path, flags | os.O_NOFOLLOW, 0o600)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise ValueError("invalid campaign lease")
        try:
            fcntl.flock(fd, (fcntl.LOCK_SH if read_only else fcntl.LOCK_EX) | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError("another campaign controller owns this lease") from exc
        yield
    finally:
        os.close(fd)


def require_confirmed(root):
    if any(root.glob("*.finalizing.json")):
        raise RuntimeError("uncertain publication requires explicit reconciliation")


@contextmanager
def manifest_read(path):
    """Generic manifests remain standalone; owned campaigns cannot opt out."""
    path = Path(path).absolute()
    root = path.parent
    owned = any((candidate / "OWNER.json").exists() or (candidate / "OWNER.json").is_symlink()
                or (candidate / ".controller.lock").exists()
                or any(candidate.glob("hermes-zvec-stress-*.json"))
                for candidate in {root, path.resolve().parent})
    if not owned:
        yield json.loads(path.read_text(encoding="utf-8"))
        return
    if root.resolve() != root or path.name != "manifest.json" or path.is_symlink():
        raise ValueError("aliased campaign manifest")
    info = root.stat()
    if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
        raise ValueError("campaign output must be private and owned")
    owner_path = root / "OWNER.json"
    if owner_path.is_symlink():
        raise ValueError("aliased campaign owner")
    owner = json.loads(owner_path.read_text())
    if (not isinstance(owner, dict) or owner.get("kind") != "zvec-campaign"
            or type(owner.get("version")) is not int or owner["version"] != 1
            or not isinstance(owner.get("repo"), str) or not Path(owner["repo"]).is_absolute()):
        raise ValueError("invalid campaign owner")
    with file_lease(root / ".controller.lock", read_only=True), file_lease(root / ".manifest.lock", read_only=True):
        manifest = json.loads(path.read_text(encoding="utf-8"))
        validate_inventory(root, manifest, read_durable_attempts(root))
        yield manifest


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


def read_durable_attempts(root):
    """Read the complete owned inventory, independently of the manifest cache."""
    for path in root.glob("hermes-zvec-stress-*.json"):
        match = re.fullmatch(r"(hermes-zvec-stress-[a-f0-9]{32})(?:\.(?:launch|finalizing))?\.json", path.name)
        if match and not (root / (match[1] + ".started.json")).is_file():
            raise ValueError("attempt inventory has a record without its started identity")
    records = []
    for path in sorted(root.glob("*.started.json")):
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
            record_path = root / (stem + suffix)
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


def validate_inventory(root, manifest, records):
    require_confirmed(root)
    for key, expected in (("runs", records), ("reports", [attempt_entry(row) for row in records])):
        rows = manifest[key]
        if (not isinstance(rows, list) or len(rows) != len(expected) or
                {row["attempt_id"]: row for row in rows} != {row["attempt_id"]: row for row in expected}):
            raise RuntimeError("incomplete attempt inventory requires explicit reconciliation")
