"""Pure aggregation tests; never start a provider or stress workload."""
import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def tool():
    path = ROOT / "scripts/report_stress.py"
    assert path.exists(), "report tool must exist"
    spec = importlib.util.spec_from_file_location("report_stress", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def report(values, passed=True):
    return {"lane": "load", "mode": "offline", "passed": passed,
            "arguments": {"workers": 1, "records": 2, "seed_facts": 0, "timeout": 30},
            "planned_callbacks": len(values), "successful_callbacks": len(values),
            "elapsed_seconds": 2, "workers": [{"samples": [
                {"phase": "add", "seconds": v} for v in values]}]}


def soak_report():
    # Shape emitted by soak_memory.supervise + main, not the load schema.
    return {"schema_version": 1, "lane": "long_lived_soak",
        "transport": "native_server_only", "passed": True, "errors": [],
        "elapsed_seconds": 10, "worker_exit": 0, "leftover_owned_pids": [],
        "worker": {"passed": True, "errors": [], "callbacks": [
            {"action": "add", "phase": "warm", "seconds": .1},
            {"action": "remove", "phase": "warm", "seconds": .3}],
            "queries": [{"phase": "warm", "kind": "fts", "generation": 1,
                         "seconds": .5, "hit": True, "required": True, "absent": False, "chars": 20}],
            "convergence": [{"phase": "warm", "seconds": 2}],
            "prefetch": [{"kind": "repeated_query", "phase": "warm", "generation": 1,
                          "seconds": .2, "chars": 10, "hit": True}],
            "restarts": [{"generation": 2, "restart_seconds": 3}],
            "cycles": 1, "active_seconds": 5, "final_mirror_records": 0, "corpus_removed": True},
        "resources": {"sample_count": 2, "peak_sampled_rss_sum_bytes": 4096,
            "rss_semantics": "concurrent_tree_sum_shared_pages_overcounted",
            "trends": {"warm:generation1": {"samples": 2, "rss_bytes_per_second": -10,
                                           "classification": "diagnostic_not_leak_proof"}}},
        # Selected real smoke summary values (no identities or local paths).
        "native_latency": {"query:warm": {"count": 41, "over_prefetch_2s_budget": 0,
            "p50_seconds": .9643653579987586, "p95_seconds": 1.6928382410042104,
            "p99_seconds": 1.7835073890018975, "max_seconds": 1.7835073890018975}},
        "native_nonzero_commands": 1, "prefetch_budget_seconds": 2,
        "arguments": {"duration": 5, "seed_facts": 20, "sample_interval": 1,
                      "convergence_timeout": 120, "engine_restart_at": None}}


def test_actual_soak_schema_exports_metrics_without_pooling_native_percentiles():
    data = tool().aggregate([soak_report(), soak_report()])
    assert data["passed"] is True
    assert data["successful_callbacks"] == 4
    assert data["callback_seconds"]["mean"] == .2
    assert data["query_seconds"]["count"] == 2
    row = data["runs"][0]
    assert (row["lane"], row["mode"]) == ("long_lived_soak", "native_server_only")
    assert "planned_callbacks" not in row  # duration-driven; no invented plan
    assert row["callback_by_phase"]["add"]["count"] == 1
    assert row["soak"]["convergence_seconds"]["max"] == 2
    assert row["soak"]["prefetch_by_kind"]["repeated_query"]["seconds"]["mean"] == .2
    assert row["soak"]["restart_seconds"]["max"] == 3
    assert row["soak"]["native_latency"]["query:warm"]["count"] == 41
    assert row["soak"]["native_nonzero_commands"] == 1  # transient restart failure, still passes
    assert "native_latency" not in data  # summary quantiles cannot be pooled
    resources = row["soak"]["resources"]
    assert resources["peak_sampled_rss_sum_bytes"] == 4096
    assert resources["trends"]["warm:generation1"]["rss_bytes_per_second"] == -10
    assert resources["rss_semantics"] == "concurrent_tree_sum_shared_pages_overcounted"
    assert "short_lived_children_may_be_missed" in resources["cpu_semantics"]
    assert row["arguments"]["duration"] == 5


@pytest.mark.parametrize("raw", [
    {"passed": True}, {"passed": True, "lane": "future", "mode": "new"},
    {**report([1]), "schema_version": 99},
    {k: v for k, v in report([1]).items() if k != "workers"},
    {**soak_report(), "schema_version": 2},
    {**soak_report(), "transport": "private-token"},
    {k: v for k, v in soak_report().items() if k != "resources"},
    {**soak_report(), "worker": {"passed": True}},
])
def test_unsupported_or_incomplete_schema_rejected(raw):
    with pytest.raises(ValueError, match="unsupported or incomplete receipt schema"):
        tool().aggregate([raw])


@pytest.mark.parametrize("section,key,value", [
    (None, "worker_exit", 1), (None, "leftover_owned_pids", [123]),
    ("worker", "passed", False), ("worker", "errors", ["private exception"]),
])
def test_soak_nested_failure_cannot_become_green(section, key, value):
    raw = soak_report()
    (raw[section] if section else raw)[key] = value
    assert tool().aggregate([raw])["passed"] is False


def test_soak_partial_failed_receipt_remains_failed():
    data = tool().aggregate([{"schema_version": 1, "lane": "long_lived_soak",
        "transport": "native_server_only", "passed": False, "errors": ["private preflight"]}])
    assert data["failed_attempts"] == 1
    assert data["error_counts"] == {"other": 1}
    assert data["callbacks_per_second_including_recovery"] is None


def test_soak_share_zip_uses_only_allowlisted_metrics(tmp_path, capsys):
    import json
    import zipfile
    raw = soak_report()
    secret = "private-host-user-token /home/private/query-body"
    raw.update(hostname=secret, exception=secret, cgroup_cleanup={"verified": True, "path": secret})
    raw["worker"]["queries"][0].update(query=secret, body=secret)
    raw["worker"]["same_handle_ids"] = [123456789]
    raw["resources"].update(notes=secret, samples_file=secret, rss_semantics=secret)
    raw["resources"]["trends"][secret] = {"samples": 99}
    raw["native_latency"][secret] = {"count": 99}
    raw["native_latency"]["query:warm"]["exception"] = secret
    raw["worker"]["restarts"][0].update(pid=123456789, instanceToken=secret)
    receipt = tmp_path / "receipt.json"
    receipt.write_text(json.dumps(raw))
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps([str(receipt)]))
    out = tmp_path / "share"
    assert tool().main(["--manifest", str(manifest), "--output-dir", str(out), "--zip"]) == 0
    assert json.loads(capsys.readouterr().out)["passed"] is True
    with zipfile.ZipFile(out / "share.zip") as bundle:
        for name in bundle.namelist():
            text = bundle.read(name).decode()
            assert secret not in text and "123456789" not in text
            assert "cgroup_cleanup" not in text
        exported = json.loads(bundle.read("aggregate.json"))
        assert exported["runs"][0]["soak"]["native_latency"]["query:warm"]["count"] == 41
        assert "shared pages" in bundle.read("aggregate.md").decode()


def test_unsupported_cli_rejects_without_creating_share(tmp_path, capsys):
    import json
    receipt = tmp_path / "private-receipt.json"
    receipt.write_text(json.dumps({"passed": True, "lane": "private-future-schema"}))
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps([str(receipt)]))
    with pytest.raises(SystemExit) as exc:
        tool().main(["--manifest", str(manifest), "--output-dir", str(tmp_path / "out")])
    assert exc.value.code == 2
    assert "private" not in capsys.readouterr().err
    assert not (tmp_path / "out").exists()


@pytest.mark.parametrize("field", ["callbacks", "queries", "convergence"])
def test_successful_soak_requires_observed_metrics(field):
    raw = soak_report()
    raw["worker"][field] = []
    with pytest.raises(ValueError, match="unsupported or incomplete receipt schema"):
        tool().aggregate([raw])


def test_soak_query_counts_and_gate_failures():
    raw = soak_report()
    raw["worker"]["queries"].append({"phase": "warm", "kind": "fts", "generation": 1,
        "seconds": .7, "chars": 0, "hit": False, "required": False, "absent": True})
    data = tool().aggregate([raw, raw])
    assert data["query_count"] == 4
    assert data["query_hits"] == 2
    assert data["runs"][0]["soak"]["query_gate_failures"] == 0
    raw["worker"]["queries"][0]["hit"] = False
    data = tool().aggregate([raw])
    assert data["passed"] is False
    assert data["runs"][0]["soak"]["query_gate_failures"] == 1


def test_pool_raw_samples_and_keep_every_attempt():
    result = tool().aggregate([report([1], False), report([3, 3, 3])])
    assert result["attempts"] == 2
    assert result["failed_attempts"] == 1
    assert result["passed"] is False
    stats = result["callback_seconds"]
    assert stats == {"count": 4, "mean": 2.5, "p50": 3, "p95": 3, "p99": 3, "max": 3}
    assert result["successful_callbacks"] == 4
    assert result["callbacks_per_second_including_recovery"] == 1


def test_sanitization_is_typed_allowlist_and_errors_are_counts():
    import json
    secret = "private-host-user-token:17999 /home/private/facts"
    raw = report([.1])
    raw.update(run=secret, python=secret, plugin_sha=secret, host_sha="a" * 40,
               mode=secret, errors=[secret, {"category": "timeout", "message": secret}],
               artifact_bytes=20, peak_single_reaped_child_rss_kib=120,
               callback_latency_seconds={"p99": 9999}, passed=True)
    raw["workers"][0].update(control={"path": secret, "content": secret}, errors=[secret])
    raw["workers"][0]["samples"].append({"phase": secret, "seconds": .3, "text": secret})
    raw["recovery"] = {"queries": [{"key": secret, "hit": True, "seconds": .5, "chars": 42}],
                       "convergence_seconds": 2, "errors": [secret]}
    result = tool().aggregate([raw])
    assert secret not in json.dumps(result)
    assert "9999" not in json.dumps(result)
    assert result["failed_attempts"] == 1  # errors override an inconsistent passed flag
    assert result["error_counts"] == {"other": 3, "timeout": 1}
    row = result["runs"][0]
    assert row["mode"] == "unknown"
    assert row["host_sha"] == "a" * 40
    assert "plugin_sha" not in row
    assert row["query_seconds"]["count"] == 1
    assert row["query_hit_rate"] == 1
    assert row["query_chars"]["max"] == 42
    assert row["callback_by_phase"]["other"]["count"] == 1
    assert row["callback_by_worker"][0]["callback_seconds"]["count"] == 2
    assert result["convergence_seconds"]["mean"] == 2
    assert result["artifact_bytes"] == 20
    assert result["peak_single_reaped_child_rss_kib"] == 120


@pytest.mark.parametrize("value", [True, -1, float("nan"), float("inf"), "0.1"])
def test_invalid_numeric_samples_are_rejected_not_silently_dropped(value):
    with pytest.raises(ValueError, match="invalid numeric metric"):
        tool().aggregate([report([value])])


def test_empty_missing_and_callback_shortfall_fail_closed():
    assert tool().aggregate([])["passed"] is False
    raw = report([])
    raw.update(planned_callbacks=3, successful_callbacks=1)
    result = tool().aggregate([raw, {}])
    assert result["failed_attempts"] == 2
    assert result["unfulfilled_callbacks"] == 2
    assert result["callback_seconds"]["p99"] is None


def test_export_manifest_machine_missing_attempt_and_private_zip(tmp_path):
    import csv
    import json
    import subprocess
    import sys
    import zipfile
    private = "private-user-host-token:17999"
    raw = report([.1, .2])
    raw.update(run="/home/" + private, source_sha="b" * 40)
    receipt = tmp_path / "receipt.json"
    receipt.write_text(json.dumps(raw))
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({"reports": [
        {"path": "receipt.json", "tags": {"scenario": private, "variant": "baseline"}},
        {"path": "receipt.json", "tags": {"scenario": private, "variant": "fix"}},
        {"path": "missing.json", "passed": False, "failure_category": "oom"}],
        "production_snapshot": private}))
    machine = tmp_path / "machine.json"
    machine.write_text(json.dumps({"hostname": private, "cpu_model": private,
        "os": "Linux", "kernel": "7.2.3-arch1-3", "architecture": "x86_64",
        "logical_cpus": 16, "available_cpu_affinity": 16,
        "memory": {"MemTotal": "15622964 kB", "MemAvailable": "9700064 kB"},
        "campaign_limits": {"MemoryMax": "2G", "CPUQuota": "200%", "TasksMax": 128}}))
    out = tmp_path / "share"
    proc = subprocess.run([sys.executable, str(ROOT / "scripts/report_stress.py"),
        "--manifest", str(manifest), "--machine", str(machine), "--output-dir", str(out), "--zip"],
        capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr  # export success, not campaign success
    data = json.loads((out / "aggregate.json").read_text())
    assert data["attempts"] == 3 and data["failed_attempts"] == 1
    assert data["runs"][0]["source_sha"] == "b" * 40
    assert data["runs"][2]["error_counts"]["oom"] == 1
    assert data["machine"]["logical_cpus"] == 16
    assert data["machine"]["memory_total_kib"] == 15622964
    assert data["machine"]["memory_limit_bytes"] == 2147483648
    assert data["machine"]["cpu_quota_percent"] == 200
    assert len(list(csv.DictReader((out / "aggregate.csv").open()))) == 3
    assert "3" in (out / "aggregate.md").read_text()
    with zipfile.ZipFile(out / "share.zip") as bundle:
        assert set(bundle.namelist()) == {"aggregate.json", "aggregate.csv", "aggregate.md"}
        for name in bundle.namelist():
            text = bundle.read(name).decode()
            assert private not in text and str(tmp_path) not in text
            assert "receipt.json" not in text and "missing.json" not in text
    assert "receipt.json" in manifest.read_text()  # local provenance stays local


def test_comparisons_require_matched_conditions_and_pool_retries():
    baseline, retry, fixed = report([1], False), report([3, 3, 3]), report([1])
    for raw in (baseline, retry, fixed):
        raw["host_sha"] = "a" * 40
    tags = [{"scenario": "private scenario", "variant": "baseline"}] * 2 + [
        {"scenario": "private scenario", "variant": "fix"}]
    result = tool().aggregate([baseline, retry, fixed], tags=tags)
    match = result["comparisons"][0]
    assert match["baseline_attempts"] == 2 and match["baseline_failed_attempts"] == 1
    assert match["callback_mean_delta_seconds"] == -1.5
    fixed["arguments"]["workers"] = 2
    assert tool().aggregate([baseline, fixed], tags=[tags[0], tags[2]])["comparisons"] == []
    fixed["arguments"]["workers"] = 1
    fixed["arguments"]["unknown_condition"] = "different"
    assert tool().aggregate([baseline, fixed], tags=[tags[0], tags[2]])["comparisons"] == []


def test_shutdown_policy_is_visible_and_not_compared_across_policies():
    original, drained = report([0.1]), report([0.01])
    original["arguments"]["shutdown_policy"] = "immediate"
    drained["arguments"]["shutdown_policy"] = "drain"
    original["host_sha"] = drained["host_sha"] = "a" * 40
    data = tool().aggregate([original, drained], tags=[
        {"scenario": "same", "variant": "baseline"},
        {"scenario": "same", "variant": "fix"}])
    assert data["runs"][0]["arguments"]["shutdown_policy"] == "immediate"
    assert data["runs"][1]["arguments"]["shutdown_policy"] == "drain"
    assert data["comparisons"] == []


def test_cgroup_resource_metrics_are_preserved_without_private_fields():
    raw = report([0.1])
    raw["metrics"] = {"cgroup_peak_bytes": [123456], "cgroup_cpu_seconds": [2.5],
                      "cgroup_oom_kills": [0], "cgroup_pids_peak": [7],
                      "cgroup_path": "/private/user/cgroup"}
    result = tool().aggregate([raw])
    assert result["custom_metrics"]["cgroup_peak_bytes"]["max"] == 123456
    assert result["custom_metrics"]["cgroup_cpu_seconds"]["mean"] == 2.5
    assert result["custom_metrics"]["cgroup_oom_kills"]["max"] == 0
    assert "/private" not in str(result)


def test_custom_metrics_only_registered_numeric_fields():
    raw = report([])
    raw["metrics"] = {"daemon_rss_kib": [100, 120], "daemon_threads": [4, 5],
                      "daemon_fds": [8, 9], "private-token": "private text"}
    result = tool().aggregate([raw])
    assert result["custom_metrics"]["daemon_rss_kib"]["mean"] == 110
    assert "private" not in str(result)


def test_export_refuses_existing_directory_and_sanitizes_failure(tmp_path, capsys):
    import json
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps([]))
    out = tmp_path / "existing"
    out.mkdir()
    sentinel = out / "aggregate.json"
    sentinel.write_text("do not overwrite")
    with pytest.raises(SystemExit) as exc:
        tool().main(["--manifest", str(manifest), "--output-dir", str(out), "--zip"])
    assert exc.value.code == 2
    assert sentinel.read_text() == "do not overwrite"
    assert str(tmp_path) not in capsys.readouterr().err
    manifest.write_text("private-secret invalid JSON")
    with pytest.raises(SystemExit):
        tool().main(["--manifest", str(manifest), "--output-dir", str(tmp_path / "new")])
    assert "private-secret" not in capsys.readouterr().err
    assert not (tmp_path / "new").exists()


def test_pooled_queries_phases_missing_metrics_and_versions():
    first, second = report([1]), report([3, 3, 3])
    first["recovery"] = {"queries": [{"seconds": 1, "hit": True, "chars": 50}]}
    second["recovery"] = {"queries": [{"seconds": 3, "hit": False, "chars": 10}] * 3}
    first["python"] = "3.11.16 (main, private-host)"
    data = tool().aggregate([first, second, {"passed": False}])
    assert data["query_seconds"]["mean"] == 2.5
    assert data["query_hit_rate"] == .25
    assert data["callback_by_phase"]["add"]["mean"] == 2.5
    assert data["runs"][0]["python_version"] == "3.11.16"
    assert data["callbacks_per_second_including_recovery"] is None
    assert data["metric_reporting_attempts"]["successful_callbacks"] == 2
    assert data["peak_single_reaped_child_rss_kib"] is None


def test_error_count_maps_missing_receipts_and_phase_by_worker(tmp_path):
    import json
    raw = report([1])
    raw["error_counts"] = {"timeout": 2, "private exception": 3}
    raw["passed"] = True
    data = tool().aggregate([raw])
    assert data["error_counts"] == {"timeout": 2, "other": 3}
    assert data["failed_attempts"] == 1
    assert data["runs"][0]["callback_by_worker"][0]["callback_by_phase"]["add"]["count"] == 1
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps(["nonexistent"]))
    reports, tags = tool().load_manifest(manifest)
    assert tool().aggregate(reports, tags)["error_counts"] == {"missing_report": 1}


def test_zero_error_counts_are_not_failures_and_nested_failures_survive():
    raw = report([1])
    raw["error_counts"] = {"timeout": 0}
    assert tool().aggregate([raw])["passed"] is True
    raw["workers"][0]["passed"] = False
    assert tool().aggregate([raw])["passed"] is False


@pytest.mark.parametrize("key", ["successful_callbacks", "planned_callbacks", "artifact_bytes"])
def test_counts_cannot_be_fractional(key):
    raw = report([1])
    raw[key] = .5
    with pytest.raises(ValueError, match="integer"):
        tool().aggregate([raw])
