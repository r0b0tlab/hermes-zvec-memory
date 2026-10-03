#!/usr/bin/env python3
"""Capture actual cgroup-v2 limits in an explicitly owned campaign directory."""
import argparse
import json
import os
from pathlib import Path
import re
import stat
import subprocess

ROOT = Path(__file__).resolve().parents[1]


def unit_identity(unit):
    if not re.fullmatch(r"hermes-zvec-stress-[a-zA-Z0-9-]+\.service", unit):
        raise ValueError("invalid owned unit name")
    result = subprocess.run(["systemctl", "--user", "show", unit,
                             "-p", "ControlGroup", "-p", "InvocationID"],
                            capture_output=True, text=True, check=True, timeout=10)
    return dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)


def capture(unit, phase, *, proc=Path("/proc/self/cgroup"), mount=Path("/sys/fs/cgroup"), identity=None):
    if phase not in {"start", "stop"}:
        raise ValueError("invalid capture phase")
    lines = [line[3:] for line in proc.read_text().splitlines() if line.startswith("0::")]
    if len(lines) != 1 or not lines[0].startswith("/"):
        raise ValueError("missing unified cgroup membership")
    relative = lines[0]
    group = mount / relative.lstrip("/")
    if group.name != unit or not group.resolve().is_relative_to(mount.resolve()):
        raise ValueError("capture is not in requested unit")
    identity = unit_identity(unit) if identity is None else identity
    if identity.get("ControlGroup") != relative or not re.fullmatch(r"[0-9a-f]{32}", identity.get("InvocationID", "")):
        raise ValueError("unit membership/identity mismatch")
    result = {"schema_version": 1, "phase": phase, "unit": unit,
              "invocation_id": identity["InvocationID"], "control_group": relative,
              "measurement_semantics": "unit_including_capture_helpers",
              "effective": {name: (group / name).read_text().strip() for name in
                            ("memory.max", "memory.swap.max", "pids.max", "cpu.max")}}
    for name, key in (("memory.events", "memory_events"), ("pids.events", "pids_events"), ("cpu.stat", "cpu")):
        result[key] = {k: int(v) for k, v in (line.split() for line in (group / name).read_text().splitlines())}
    for name, key in (("memory.peak", "memory_peak_bytes"), ("memory.swap.peak", "swap_peak_bytes"), ("pids.peak", "pids_peak")):
        path = group / name
        result[key] = int(path.read_text().strip()) if path.exists() else None
    return result


def limits_match(effective, requested):
    try:
        quota, period = effective["cpu.max"].split()
        return (effective["memory.max"] == str(requested["memory_bytes"])
                and effective["memory.swap.max"] == str(requested["swap_bytes"])
                and effective["pids.max"] == str(requested["tasks"])
                and int(period) == 100000
                and int(quota) * 100 == requested["cpu_quota_percent"] * int(period))
    except (KeyError, TypeError, ValueError):
        return False


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    parser.add_argument("--unit", required=True)
    parser.add_argument("--phase", choices=("start", "stop"), required=True)
    parser.add_argument("--tasks", type=int, default=256)
    args = parser.parse_args(argv)
    path = args.output.absolute()
    directory = path.parent
    info = directory.stat()
    if (path != path.resolve() or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) != 0o700 or (directory / "OWNER.json").is_symlink()):
        raise ValueError("capture output is not private/owned")
    if json.loads((directory / "OWNER.json").read_text()) != {"kind": "zvec-campaign", "version": 1, "repo": str(ROOT)}:
        raise ValueError("capture owner marker mismatch")
    if path.name != args.unit.removesuffix(".service") + f".{args.phase}.limits.json":
        raise ValueError("capture filename does not match unit/phase")
    result = capture(args.unit, args.phase)
    requested = {"memory_bytes": 2147483648, "swap_bytes": 0, "tasks": args.tasks, "cpu_quota_percent": 200}
    result["limits_match"] = limits_match(result["effective"], requested)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w") as stream:
        json.dump(result, stream, indent=2, allow_nan=False)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    return 0 if result["limits_match"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
