#!/usr/bin/env python3
"""Aggregate stress receipts without importing or running the provider.

Percentiles use nearest rank on pooled raw samples, never pooled percentiles.
Throughput uses total successful callbacks / summed elapsed time (not wall time
for concurrent campaigns). Every input occurrence is an attempt, including retries.
Errors count occurrences in each receipt section; mirrored errors may count twice.
Unknown text is discarded, not scrubbed heuristically. Worker IDs are ordinals.

CLI (output directory must not exist)::

    .venv/bin/python scripts/report_stress.py --manifest manifest.json \
        --machine machine.json --output-dir share-output --zip

Manifest is a list of entries (path strings also work), or {"reports": [...]}::

    {"reports": [
      {"path": "../stress-runs/run/report.json",
       "tags": {"scenario": "same-workload", "variant": "baseline",
                "environment": "same-machine-cache-and-limits"}},
      {"path": "../stress-runs/fixed/report.json",
       "tags": {"scenario": "same-workload", "variant": "fix",
                "environment": "same-machine-cache-and-limits"}},
      {"passed": false, "failure_category": "oom",
       "metadata": {"lane": "load", "mode": "native", "source_sha": "..."}}
    ]}

Paths are relative to the LOCAL manifest, never exported. No receipt is required
for an explicit failed attempt. Receipt values override metadata; manifest false
status/failure category cannot be overridden by a green receipt. Scenario labels
become ordinal IDs; only baseline/fix variant values are exported. Comparison
matching uses all other tags privately, including unknown environment conditions.
No hostname, CPU model string, port, exception text, or arbitrary metadata survives.

Standard raw samples: workers[].samples[{phase, seconds}], recovery.queries[]
[{hit, seconds, chars}], recovery.convergence_seconds. Already-computed latency
percentiles are ignored. Optional metrics maps names in CUSTOM_METRICS to raw
numeric sample lists (e.g. {"daemon_rss_kib": [100, 120]}). Unknown names are
ignored; extend the explicit registry with tests for future schemas, not passthrough.
The explicit soak adapter supports soak_memory.py receipt v1 (long_lived_soak,
native_server_only transport). It exports worker callbacks/queries, convergence,
prefetch, restart timings, resources and per-command/phase native summaries at
runs[].soak. Native quantiles are NOT raw samples and are never pooled. Nonzero
native commands remain diagnostic counts, not automatic gate failures. Soak has
no planned callback count. Query hit rates include intentional absence probes;
query_gate_failures separately checks required hits and required absences.
Only known complete metric schemas may claim success; unsupported/incomplete
successful receipts raise ValueError (CLI exit 2), never pass with empty metrics.
Partial failed receipts retain failure/status metadata without inferred metrics.
Parent service/cgroup unit envelopes are NOT supported: point manifest paths at
inner receipts and propagate outer failures with passed=false/failure_category.
Cgroup cleanup verification and cgroup peak memory are not exported by this
adapter; concurrent sampled RSS is a distinct measurement, not cgroup peak.
No resource JSONL, production snapshot, native command log or private file is read.
Errors are lists of arbitrary objects (counted as other) or {category: known_enum};
error_counts maps categories to nonnegative integer occurrence counts. Supply one
representation per occurrence to avoid double counting. All totals cover observed
values only; metric_reporting_attempts exposes omissions and incomplete throughput
is null. No missing samples are synthesized from summary statistics.

Output: aggregate.json (complete sanitized metrics), aggregate.csv (one row per
attempt), aggregate.md, and optional share.zip containing exactly those three files.
The archive is built from sanitized in-memory strings, never by walking directories.
Exit 0 means export succeeded, NOT that the campaign passed; inspect passed and
failed_attempts. Exit 2 means invalid input or unavailable output (no raw traceback).
"""
from collections import Counter, defaultdict
import argparse
import csv
import io
import json
import math
from pathlib import Path
import re
import zipfile

# Extension contract: metrics is a mapping from these names to nonnegative raw
# numeric sample lists. Add names here with tests; never pass arbitrary keys through.
CUSTOM_METRICS = {"daemon_rss_kib", "daemon_threads", "daemon_fds",
                  "provider_rss_kib", "provider_threads", "provider_fds",
                  "query_seconds", "shutdown_seconds", "pending_inbox",
                  "pending_creates", "pending_deletes", "cgroup_peak_bytes",
                  "cgroup_swap_peak_bytes", "cgroup_cpu_seconds", "cgroup_throttled_seconds",
                  "cgroup_oom_kills", "cgroup_pids_peak"}
ARGUMENTS = {"workers", "records", "seed_facts", "timeout", "iterations",
             "duration_seconds", "sample_interval_seconds", "context_chars",
             "duration", "sample_interval", "convergence_timeout", "engine_restart_at"}

LANES = {"load", "regression", "native-regression", "soak", "daemon-soak", "corpus", "fault"}
MODES = {"offline", "native", "direct", "daemon"}
PHASES = {"add", "replace", "remove", "store", "search", "prefetch", "shutdown"}
ERRORS = {"timeout", "oom", "callback", "recovery", "child_exit", "oracle", "query", "other", "missing_report"}
NUMERIC = {"planned_callbacks", "successful_callbacks", "elapsed_seconds", "artifact_bytes",
           "peak_single_reaped_child_rss_kib"}


def number(value):
    if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
        raise ValueError("invalid numeric metric")
    return value


def count(value):
    number(value)
    if type(value) is not int:
        raise ValueError("count metric must be integer")
    return value


def enum(value, choices, default="unknown"):
    return value if isinstance(value, str) and value in choices else default


def stats(values):
    values = sorted(number(v) for v in values)
    return {"count": len(values), "mean": sum(values) / len(values) if values else None,
            **{name: values[math.ceil(len(values) * q) - 1] if values else None
               for name, q in (("p50", .5), ("p95", .95), ("p99", .99), ("max", 1))}}


def samples(report):
    return [s for w in report.get("workers", []) for s in w.get("samples", [])]


def error_counts(report):
    counts = Counter()
    for section in [report, *report.get("workers", []), report.get("recovery", {})]:
        for error in section.get("errors", []):
            category = error.get("category") if isinstance(error, dict) else None
            counts[enum(category, ERRORS, "other")] += 1
        for category, value in section.get("error_counts", {}).items():
            counts[enum(category, ERRORS, "other")] += count(value)
    return +counts


def phase_stats(samples):
    phases = defaultdict(list)
    for sample in samples:
        phases[enum(sample.get("phase"), PHASES, "other")].append(sample["seconds"])
    return {k: stats(v) for k, v in sorted(phases.items())}


def summarize(report, index):
    errors = error_counts(report)
    row = {"attempt": index, "lane": enum(report.get("lane"), LANES),
           "mode": enum(report.get("mode"), MODES), "error_counts": dict(errors)}
    for key in NUMERIC:
        if key in report:
            row[key] = number(report[key]) if key == "elapsed_seconds" else count(report[key])
    for key in ("plugin_sha", "source_sha", "host_sha"):
        value = report.get(key)
        if isinstance(value, str) and re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", value):
            row[key] = value
    value = report.get("python_version", report.get("python"))
    if isinstance(value, str):
        match = re.match(r"^([0-9]+\.[0-9]+\.[0-9]+)(?: |$)", value)
        if match:
            row["python_version"] = match[1]
    planned = row.get("planned_callbacks")
    successful = row.get("successful_callbacks")
    row["unfulfilled_callbacks"] = max(0, planned - successful) if planned is not None and successful is not None else None
    nested_failure = any(section.get("passed", section.get("pass")) is False
                         for section in [*report.get("workers", []), report.get("recovery", {}),
                                         *report.get("iterations", [])])
    row["passed"] = (report.get("passed", report.get("pass")) is True and not errors
                     and not nested_failure and (planned is None or successful == planned))
    row["callback_seconds"] = stats([s["seconds"] for s in samples(report)])
    row["callback_by_phase"] = phase_stats(samples(report))
    row["callback_by_worker"] = [
        {"worker": i, "callback_seconds": stats([s["seconds"] for s in w.get("samples", [])]),
         "callback_by_phase": phase_stats(w.get("samples", []))}
        for i, w in enumerate(report.get("workers", []))]
    queries = report.get("recovery", {}).get("queries", [])
    row["query_seconds"] = stats([q["seconds"] for q in queries if "seconds" in q])
    row["query_chars"] = stats([q["chars"] for q in queries if "chars" in q])
    row["query_count"] = len(queries)
    row["query_hits"] = sum(q.get("hit") is True for q in queries)
    row["query_hit_rate"] = row["query_hits"] / len(queries) if queries else None
    if "convergence_seconds" in report.get("recovery", {}):
        row["convergence_seconds"] = number(report["recovery"]["convergence_seconds"])
    row["arguments"] = {k: number(v) for k, v in report.get("arguments", {}).items() if k in ARGUMENTS and v is not None}
    if "shutdown_policy" in report.get("arguments", {}):
        row["arguments"]["shutdown_policy"] = enum(report["arguments"]["shutdown_policy"], {"immediate", "drain"})
    if report.get("lane") == "long_lived_soak":
        row["lane"], row["mode"] = "long_lived_soak", "native_server_only"
        row["soak"] = report["soak"]
    return row


def custom_metrics(reports):
    pooled = defaultdict(list)
    for report in reports:
        for name, values in report.get("metrics", {}).items():
            if name in CUSTOM_METRICS:
                if not isinstance(values, list):
                    raise ValueError("custom metrics require raw sample lists")
                pooled[name].extend(values)
    return {name: stats(values) for name, values in sorted(pooled.items())}


def comparisons(reports, rows, tags):
    groups = defaultdict(lambda: {"baseline": [], "fix": []})
    for i, (raw, row, tag) in enumerate(zip(reports, rows, tags)):
        variant = enum(tag.get("variant"), {"baseline", "fix"})
        args = raw.get("arguments", {})
        if (variant == "unknown" or not tag.get("scenario") or "host_sha" not in row
                or row["lane"] == "unknown" or row["mode"] == "unknown"
                or not {"workers", "records", "seed_facts", "timeout"} <= args.keys()):
            continue
        # Compare even unknown conditions privately; don't silently ignore them.
        conditions = {k: v for k, v in args.items() if k not in {"run", "worker", "recover"}}
        context = {k: v for k, v in tag.items() if k not in {"variant", "failure_category"}}
        key = json.dumps([row["lane"], row["mode"], row["host_sha"], conditions, context], sort_keys=True)
        groups[key][variant].append(i)
    result = []
    for group in groups.values():
        if not group["baseline"] or not group["fix"]:
            continue
        item: dict = {"comparison": len(result) + 1}
        means = {}
        for variant, indices in group.items():
            item[variant + "_attempts"] = len(indices)
            item[variant + "_failed_attempts"] = sum(not rows[i]["passed"] for i in indices)
            item[variant + "_attempt_ids"] = [rows[i]["attempt"] for i in indices]
            means[variant] = stats([s["seconds"] for i in indices for s in samples(reports[i])])["mean"]
        item["callback_mean_delta_seconds"] = (means["fix"] - means["baseline"]
            if all(v is not None for v in means.values()) else None)
        result.append(item)
    return result


SOAK_PHASES = {"cold_start", "warm", "corpus_removal", "warm_after_removal"}


def soak_metrics(raw):
    """Receipt-v1 summaries stay per attempt; never pool their quantiles."""
    worker, resources = raw["worker"], raw["resources"]
    native = {}
    for key, value in raw["native_latency"].items():
        if key not in {f"{kind}:{phase}" for kind in ("index", "query", "status", "probe")
                       for phase in SOAK_PHASES}:
            continue
        native[key] = {k: (count(value[k]) if k in {"count", "over_prefetch_2s_budget"}
                           else number(value[k])) for k in (
            "count", "over_prefetch_2s_budget", "p50_seconds", "p95_seconds", "p99_seconds", "max_seconds")}
    trends = {}
    for key, value in resources["trends"].items():
        match = re.fullmatch(r"(cold_start|warm|corpus_removal|warm_after_removal):generation([0-9]+)", key)
        if not match:
            continue
        slope = value["rss_bytes_per_second"]
        if slope is not None:
            if type(slope) not in (int, float) or not math.isfinite(slope):
                raise ValueError("invalid numeric metric")
        trends[key] = {"samples": count(value["samples"]), "rss_bytes_per_second": slope,
                       "classification": "diagnostic_not_leak_proof"}
    prefetch = {}
    for kind in ("repeated_query", "distinct_query"):
        values = [v for v in worker.get("prefetch", []) if v.get("kind") == kind]
        prefetch[kind] = {"seconds": stats([v["seconds"] for v in values]),
                          "chars": stats([v["chars"] for v in values]),
                          "hits": sum(v.get("hit") is True for v in values)}
    result = {"native_latency": native,
        "native_nonzero_commands": count(raw["native_nonzero_commands"]),
        "prefetch_budget_seconds": number(raw["prefetch_budget_seconds"]),
        "convergence_seconds": stats([v["seconds"] for v in worker["convergence"]]),
        "prefetch_by_kind": prefetch,
        "restart_seconds": stats([v["restart_seconds"] for v in worker["restarts"]]),
        "resources": {"sample_count": count(resources["sample_count"]),
            "peak_sampled_rss_sum_bytes": count(resources["peak_sampled_rss_sum_bytes"]),
            "rss_semantics": "concurrent_tree_sum_shared_pages_overcounted",
            "cpu_semantics": "observed_lifetime_sum_short_lived_children_may_be_missed",
            "trends": trends}}
    for key in ("cycles", "final_mirror_records", "active_seconds"):
        if key in worker:
            result[key] = number(worker[key]) if key == "active_seconds" else count(worker[key])
    result["corpus_removed"] = worker.get("corpus_removed") is True
    result["query_gate_failures"] = sum(
        (q.get("required") is True and q.get("hit") is not True)
        or (q.get("absent") is True and q.get("hit") is True)
        for q in worker["queries"])
    return result


def adapt_report(raw):
    """Only known schemas can claim success; incomplete failures stay attempts."""
    soak = raw.get("lane") == "long_lived_soak"
    version = raw.get("schema_version", 1)
    supported = type(version) is int and version == 1
    if soak:
        worker = raw.get("worker", {})
        resources = raw.get("resources", {})
        supported = (supported and raw.get("transport") == "native_server_only"
            and {"elapsed_seconds", "worker_exit", "leftover_owned_pids", "native_latency",
                 "native_nonzero_commands", "prefetch_budget_seconds"} <= raw.keys()
            and {"passed", "callbacks", "queries", "convergence", "prefetch", "restarts"} <= worker.keys()
            and {"sample_count", "peak_sampled_rss_sum_bytes", "trends", "rss_semantics"} <= resources.keys()
            and (raw.get("passed") is not True or all(worker[k] for k in ("callbacks", "queries", "convergence"))))
    else:
        supported = (supported and enum(raw.get("lane"), LANES) != "unknown"
            and (enum(raw.get("mode"), MODES) != "unknown" or bool(error_counts(raw)))
            and {"planned_callbacks", "successful_callbacks", "elapsed_seconds", "workers"} <= raw.keys()
            and isinstance(raw["workers"], list)
            and all(isinstance(w, dict) and isinstance(w.get("samples"), list) for w in raw["workers"]))
    if not supported:
        if raw.get("passed", raw.get("pass")) is True:
            raise ValueError("unsupported or incomplete receipt schema")
        # Failure-only envelope: do not interpret unknown metrics or parent units.
        result = {k: raw[k] for k in ("lane", "mode", "source_sha", "plugin_sha", "host_sha",
                                      "errors", "error_counts") if k in raw}
        result["passed"] = False
        if soak:
            result["lane"] = "soak"
        return result
    if not soak:
        return raw
    worker = raw["worker"]
    result = dict(raw)
    result["passed"] = (raw.get("passed") is True and worker.get("passed") is True
                        and type(raw["worker_exit"]) is int and raw["worker_exit"] == 0
                        and raw["leftover_owned_pids"] == [])
    result["mode"] = "native_server_only"
    result["workers"] = [{**worker, "samples": [
        {"phase": v["action"], "seconds": v["seconds"]} for v in worker["callbacks"]]}]
    result["successful_callbacks"] = len(worker["callbacks"])
    # Duration-driven soak has no planned callback count; do not invent one.
    result["recovery"] = {"queries": worker["queries"]}
    result["soak"] = soak_metrics(raw)
    result["passed"] = result["passed"] and not result["soak"]["query_gate_failures"]
    return result


def aggregate(reports, tags=None, machine=None):
    reports = [adapt_report(r) for r in reports]
    rows = [summarize(r, i) for i, r in enumerate(reports, 1)]
    tags = [{} for _ in reports] if tags is None else tags
    if len(tags) != len(reports):
        raise ValueError("tags must match attempts")
    scenarios = {}
    for row, tag, raw in zip(rows, tags, reports):
        scenario = json.dumps(tag.get("scenario"), sort_keys=True)
        if scenario not in scenarios:
            scenarios[scenario] = len(scenarios) + 1
        row["scenario"] = scenarios[scenario]
        row["variant"] = enum(tag.get("variant"), {"baseline", "fix"})
        row["custom_metrics"] = custom_metrics([raw])
    successful = sum(r.get("successful_callbacks", 0) for r in rows)
    elapsed = sum(r.get("elapsed_seconds", 0) for r in rows)
    failures = sum(not r["passed"] for r in rows)
    errors = Counter()
    for row in rows:
        errors.update(row["error_counts"])
    queries = [q for r in reports for q in r.get("recovery", {}).get("queries", [])]
    reporting = {k: sum(k in r for r in rows) for k in sorted(NUMERIC)}
    throughput_complete = all(reporting[k] == len(rows) for k in ("successful_callbacks", "elapsed_seconds"))
    return {"metric_reporting_attempts": reporting,
            "callback_by_phase": phase_stats([s for r in reports for s in samples(r)]),
            "query_seconds": stats([q["seconds"] for q in queries if "seconds" in q]),
            "query_hit_rate": sum(q.get("hit") is True for q in queries) / len(queries) if queries else None,
            "query_count": len(queries),
            "query_hits": sum(q.get("hit") is True for q in queries),
            "schema_version": 1, "attempts": len(rows), "failed_attempts": failures,
            "machine": sanitize_machine(machine or {}),
            "custom_metrics": custom_metrics(reports),
            "comparisons": comparisons(reports, rows, tags),
            "passed": bool(rows) and not failures, "runs": rows,
            "error_counts": dict(errors),
            "callback_seconds": stats([s["seconds"] for r in reports for s in samples(r)]),
            "successful_callbacks": successful,
            "planned_callbacks": sum(r.get("planned_callbacks", 0) for r in rows),
            "unfulfilled_callbacks": sum(r["unfulfilled_callbacks"] or 0 for r in rows),
            "elapsed_seconds": elapsed,
            "convergence_seconds": stats([r["convergence_seconds"] for r in rows if "convergence_seconds" in r]),
            "artifact_bytes": sum(r.get("artifact_bytes", 0) for r in rows),
            "peak_single_reaped_child_rss_kib": max((r["peak_single_reaped_child_rss_kib"] for r in rows if "peak_single_reaped_child_rss_kib" in r), default=None),
            "callbacks_per_second_including_recovery": successful / elapsed if elapsed and throughput_complete else None}


def sanitize_machine(raw):
    """Discard identifying model/build strings; retain generic counts/versions."""
    result = {}
    for key, choices in (("os", {"Linux", "Darwin", "Windows"}),
                         ("architecture", {"x86_64", "aarch64", "arm64", "AMD64"})):
        result[key] = enum(raw.get(key), choices)
    for key in ("logical_cpus", "available_cpu_affinity"):
        if key in raw:
            result[key] = number(raw[key])
    for source, target in (("MemTotal", "memory_total_kib"), ("MemAvailable", "memory_available_kib")):
        value = raw.get("memory", {}).get(source)
        if isinstance(value, str) and re.fullmatch(r"[0-9]+ kB", value):
            result[target] = int(value.split()[0])
    limits = raw.get("campaign_limits", {})
    value = limits.get("MemoryMax")
    if isinstance(value, str) and re.fullmatch(r"[0-9]+[KMG]", value):
        result["memory_limit_bytes"] = int(value[:-1]) * 1024 ** ("KMG".index(value[-1]) + 1)
    value = limits.get("CPUQuota")
    if isinstance(value, str) and re.fullmatch(r"[0-9]+%", value):
        result["cpu_quota_percent"] = int(value[:-1])
    if "TasksMax" in limits:
        result["tasks_limit"] = number(limits["TasksMax"])
    # Only numeric release components: build suffixes can contain host identities.
    for key in ("kernel", "python_version", "zg_version", "zvec_version", "node_version"):
        value = raw.get(key)
        if isinstance(value, str):
            match = re.fullmatch(r"([0-9]+\.[0-9]+\.[0-9]+)(?:[-+][A-Za-z0-9.-]+)?", value)
            if match:
                result[key] = match[1]
    return result


def load_manifest(path):
    """Entries: path, tags, optional metadata and explicit passed/failure_category.

    Relative report paths resolve against the manifest directory. Missing/broken
    reports remain failed attempts. Metadata may supply report fields for failed
    attempts without receipts; all export still goes through the same allowlist.
    """
    manifest = json.loads(path.read_text(encoding="utf-8"))
    entries = manifest if isinstance(manifest, list) else manifest["reports"]
    if not isinstance(entries, list):
        raise ValueError("manifest reports must be a list")
    reports, tags = [], []
    for entry in entries:
        entry = {"path": entry} if isinstance(entry, str) else entry
        if not isinstance(entry, dict) or not isinstance(entry.get("metadata", {}), dict):
            raise ValueError("invalid manifest entry")
        raw: dict = dict(entry.get("metadata", {}))
        try:
            receipt = json.loads((path.parent / entry["path"]).read_text(encoding="utf-8"))
            if not isinstance(receipt, dict):
                raise ValueError("invalid receipt")
            raw.update(receipt)
        except (OSError, ValueError, KeyError):
            raw.update(passed=False, errors=[{"category": "missing_report"}])
        if entry.get("passed") is False or entry.get("failure_category"):
            raw["passed"] = False
            raw["errors"] = [*raw.get("errors", []),
                             {"category": enum(entry.get("failure_category"), ERRORS, "other")}]
        reports.append(raw)
        tags.append(entry.get("tags", {}))
    return reports, tags


def render(data):
    """Only call with aggregate() output; no original data is rendered."""
    fields = ["attempt", "scenario", "variant", "lane", "mode", "passed", "workers",
              "records", "seed_facts", "planned_callbacks", "successful_callbacks",
              "elapsed_seconds", "callback_mean_seconds", "callback_p99_seconds",
              "convergence_seconds", "query_hit_rate", "artifact_bytes",
              "peak_single_reaped_child_rss_kib", "error_counts", "plugin_sha", "source_sha", "host_sha"]
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=fields)
    writer.writeheader()
    lines = ["# Stress campaign metrics", "",
             f"Attempts: {data['attempts']}; failed: {data['failed_attempts']}; passed: {data['passed']}.", "",
             "Every listed attempt is retained, including retries and missing receipts.",
             "Percentiles: nearest rank over pooled raw samples; means are sample-weighted.",
             "Throughput divides total callbacks by summed run elapsed time, including recovery.",
             "Legacy RSS is peak SINGLE reaped child, not aggregate process-tree memory.",
             "Soak RSS is concurrent sampled tree sum: shared pages are overcounted; not PSS.",
             "Soak CPU sampling may miss short-lived children; trends are diagnostic, not leak proof.",
             "Soak native latency quantiles remain per-attempt command/phase summaries, never pooled.",
             "Complete soak metrics are in aggregate.json runs[].soak; CSV is a compact overview.",
             "Error counts are receipt occurrences; coordinator/worker duplicates are not deduplicated.",
             "Scenario labels are anonymous IDs. Unknown fields and identifying text are omitted.",
             "Pooled campaign latency may mix conditions; use per-attempt rows for capacity claims.", "",
             "| Attempt | Scenario | Variant | Lane | Mode | Pass | Callbacks | p99 (s) | Errors |",
             "|---:|---:|---|---|---|---|---:|---:|---|"]
    for row in data["runs"]:
        flat = {k: row.get(k) for k in fields}
        flat.update({k: row["arguments"].get(k) for k in ("workers", "records", "seed_facts")})
        flat["callback_mean_seconds"] = row["callback_seconds"]["mean"]
        flat["callback_p99_seconds"] = row["callback_seconds"]["p99"]
        flat["error_counts"] = json.dumps(row["error_counts"], sort_keys=True)
        writer.writerow(flat)
        lines.append("| " + " | ".join(str(flat[k]) for k in (
            "attempt", "scenario", "variant", "lane", "mode", "passed",
            "successful_callbacks", "callback_p99_seconds", "error_counts")) + " |")
    lines.extend(["", "## Matched baseline/fix comparisons", "",
                  "Deltas are fix minus baseline; they are descriptive, not causal claims.",
                  "Matching requires scenario/context tags, host SHA, lane, mode, and full workload arguments.",
                  "Machine/cache/environment equality must be supplied in context tags when they vary.",
                  "Missing required conditions produce no comparison.", "",
                  "```json", json.dumps(data["comparisons"], indent=2, allow_nan=False), "```", ""])
    return {"aggregate.json": json.dumps(data, indent=2, sort_keys=True, allow_nan=False) + "\n",
            "aggregate.csv": output.getvalue(), "aggregate.md": "\n".join(lines)}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--machine", type=Path, help="optional local machine metadata JSON")
    parser.add_argument("--output-dir", required=True, type=Path, help="new directory; never overwrite")
    parser.add_argument("--zip", action="store_true", help="write only sanitized aggregate files to share.zip")
    args = parser.parse_args(argv)
    try:
        reports, tags = load_manifest(args.manifest)
        machine = json.loads(args.machine.read_text(encoding="utf-8")) if args.machine else {}
        data = aggregate(reports, tags=tags, machine=machine)
        artifacts = render(data)
        args.output_dir.mkdir(parents=True, exist_ok=False)
        for name, text in artifacts.items():
            (args.output_dir / name).write_text(text, encoding="utf-8")
        if args.zip:
            with zipfile.ZipFile(args.output_dir / "share.zip", "x", zipfile.ZIP_DEFLATED) as bundle:
                for name, text in artifacts.items():
                    bundle.writestr(name, text)
    except (OSError, ValueError, TypeError, KeyError, AttributeError, OverflowError):
        # Do not leak paths, values, or exception strings to captured share logs.
        parser.exit(2, "report export failed: invalid input or output unavailable\n")
    print(json.dumps({"attempts": data["attempts"], "failed_attempts": data["failed_attempts"],
                      "passed": data["passed"]}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
