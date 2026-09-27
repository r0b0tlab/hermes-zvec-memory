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
    # One record: add, replace, remove. Failed attempts may be partial.
    if passed and values:
        values = (values * 3)[:3]
    steps = ("add", "replace", "remove")
    return {"lane": "load", "mode": "offline", "passed": passed and bool(values),
            "arguments": {"workers": 1, "records": 1, "seed_facts": 0, "timeout": 30},
            "planned_callbacks": 3, "successful_callbacks": len(values),
            "elapsed_seconds": 2, "workers": [{"worker": 0,
                "planned_callbacks": 3, "successful_callbacks": len(values),
                "samples": [{"phase": steps[i], "record": 0, "accepted": True, "seconds": v}
                            for i, v in enumerate(values)]}],
            "recovery": {"inbox_empty": True, "mirror_records": 0,
                         "expected_mirror_records": 0, "errors": []}}


def zero_hit_body(key):
    from test_retrieval_evidence import native_fixture
    return native_fixture("zero_hit")["response"]["results"].replace("stressSYNTHETIC002", key)


def soak_report():
    # Synthetic unit fixture ONLY, never a capacity receipt.
    raw = {"schema_version": 1, "lane": "long_lived_soak",
        "transport": "native_server_only", "passed": True, "errors": [],
        "elapsed_seconds": 10, "worker_exit": 0, "leftover_owned_pids": [],
        "worker": {"passed": True, "errors": [],
            "cycles": 1, "active_seconds": 5, "final_mirror_records": 0, "corpus_removed": True},
        "resources": {"sample_count": 2, "peak_sampled_rss_sum_bytes": 4096,
            "rss_semantics": "concurrent_tree_sum_shared_pages_overcounted",
            "trends": {"warm:generation1": {"samples": 2, "rss_bytes_per_second": -10,
                                           "classification": "diagnostic_not_leak_proof"}}},
        # Quantile-shaped fixture values, not a measurement.
        "native_latency": {"query:warm": {"count": 41, "over_prefetch_2s_budget": 0,
            "p50_seconds": .9643653579987586, "p95_seconds": 1.6928382410042104,
            "p99_seconds": 1.7835073890018975, "max_seconds": 1.7835073890018975}},
        "native_nonzero_commands": 1, "prefetch_budget_seconds": 2,
        "arguments": {"duration": 5, "seed_facts": 20, "sample_interval": 1,
                      "convergence_timeout": 120, "engine_restart_at": 0}}
    w = raw["worker"]
    for section in ("callbacks", "queries", "convergence", "prefetch", "restarts", "operations", "ledger"):
        w[section] = []
    generation, cycle, stage, phase = 1, None, "initial", "cold_start"
    def record(section, **row):
        row.update(cycle=cycle, stage=stage, phase=phase, generation=generation)
        w["ledger"].append([section, len(w[section])])
        w[section].append(row)
    control = "soakcontrolanchor confirms synthetic durable control is violet."
    def query(probe, slot=None):
        content = (control if probe == "control" else
                   f"soakslot{slot:02d} revisedanswer cycle{cycle:08d} is indigo." if probe == "revised" else "")
        key = ("soakcontrolanchor" if probe == "control" else "soakcleanupanchor" if probe == "cleanup"
               else "soakbackground000000" if probe == "background" else f"soakslot{slot:02d}")
        body = f"#1 facts/a.md:1\nsource:\n1\t{content}\n" if content else zero_hit_body(key)
        record("queries", probe=probe, slot=slot, key=key, kind="hybrid" if probe == "control" else "fts",
               seconds=.5, chars=len(body), response={"results": body},
               sources={"facts/a.md": content} if content else {}, hit=bool(content),
               required=bool(content), absent=not bool(content))
    files = [f"background-{n:06d}.md" for n in range(20)]
    record("operations", action="seed", files=files)
    record("operations", action="control_store", path="facts/a.md")
    record("convergence", records=0, seconds=2)
    query("control")
    generation, phase, stage, cycle = 2, "warm", "revise", 0
    record("restarts", restart_seconds=3, active_seconds=0)
    for slot in range(2):
        for action in ("add", "replace"):
            revised = f"soakslot{slot:02d} revisedanswer cycle{cycle:08d} is indigo."
            old = revised.replace("revisedanswer", "obsoleteanswer")
            record("callbacks", action=action, slot=slot, seconds=.2,
                   content=old if action == "add" else revised, previous="" if action == "add" else old)
    record("convergence", records=2, seconds=2)
    for slot in range(2):
        query("revised", slot)
        query("obsolete", slot)
    stage = "remove"
    for slot in range(2):
        record("callbacks", action="remove", slot=slot, seconds=.2, content="",
               previous=f"soakslot{slot:02d} revisedanswer cycle{cycle:08d} is indigo.")
    record("convergence", records=0, seconds=2)
    for slot in range(2): query("deleted", slot)
    query("control")
    for kind in ("repeated_query", "distinct_query"):
        record("prefetch", kind=kind, seconds=.2, chars=10, hit=True)
    record("operations", action="cycle_complete")
    cycle, stage, phase = None, "cleanup", "corpus_removal"
    record("operations", action="corpus_remove", files=files)
    for action in ("add", "remove"):
        marker = "soakcleanupanchor synthetic cleanup marker."
        record("callbacks", action=action, slot=None, seconds=.2,
               content=marker if action == "add" else "", previous=marker if action == "remove" else "")
    record("convergence", records=0, seconds=2)
    stage, phase = "final", "warm_after_removal"
    record("convergence", records=0, seconds=2)
    query("control")
    query("background")
    query("cleanup")
    record("operations", action="final_state", records=0)
    return raw


def test_actual_soak_schema_exports_metrics_without_pooling_native_percentiles():
    data = tool().aggregate([soak_report(), soak_report()])
    assert data["passed"] is True
    assert data["successful_callbacks"] == 16
    assert data["callback_seconds"]["mean"] == pytest.approx(.2)
    assert data["query_seconds"]["count"] == 22
    row = data["runs"][0]
    assert (row["lane"], row["mode"]) == ("long_lived_soak", "native_server_only")
    assert "planned_callbacks" not in row  # duration-driven; no invented plan
    assert row["callback_by_phase"]["add"]["count"] == 3
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


@pytest.mark.parametrize("lane", ["long_lived_soak", "load", "future"])
@pytest.mark.parametrize("invalid", [None, "product_identity", "harness_identity", "host_provenance"])
def test_incomplete_failure_retains_each_valid_identity(lane, invalid):
    raw = {"lane": lane, "passed": False, "errors": ["private preflight exception"],
           "worker": {"arbitrary": "not metrics"}, "elapsed_seconds": "not measured"}
    for key, fingerprint in zip(("product_identity", "harness_identity", "host_provenance"), "abc"):
        raw[key] = {"kind": "installed_snapshot", "content_sha256": fingerprint * 64,
                    "git_sha": "d" * 40, "dirty": True, "files": {"/private": "secret"}}
    if invalid: raw[invalid]["content_sha256"] = "/private/malformed"
    adapted = tool().adapt_report(raw)
    result = tool().aggregate([raw])
    row = result["runs"][0]
    assert result["attempts"] == result["failed_attempts"] == 1
    assert result["callbacks_per_second_including_recovery"] is None
    for key in ("elapsed_seconds", "workers", "worker", "successful_callbacks", "planned_callbacks"):
        assert key not in adapted, "never invent incomplete workload metrics"
    for key in ("product_identity", "harness_identity", "host_provenance"):
        if key == invalid:
            assert key not in adapted and key not in row
        else:
            expected = {k: v for k, v in raw[key].items() if k != "files"}
            assert adapted[key] == row[key] == expected
    assert "/private" not in str(row) and "secret" not in str(adapted)


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
    data = tool().aggregate([raw, raw])
    assert data["query_count"] == 22
    assert data["query_hits"] == 10
    assert data["runs"][0]["soak"]["query_gate_failures"] == 0
    raw["worker"]["queries"][0]["hit"] = False
    with pytest.raises(ValueError): tool().aggregate([raw])


@pytest.mark.parametrize("damage", ["replace", "obsolete", "deleted", "final_probe", "cycles",
    "generation", "restart", "removals", "convergence", "unreferenced", "echo", "reordered", "ledger", "wrong_key"])
def test_soak_requires_reconciled_observed_ledger(damage):
    raw = soak_report()
    w = raw["worker"]
    if damage == "replace": w["callbacks"][1]["action"] = "add"
    elif damage in {"obsolete", "deleted"}:
        next(q for q in w["queries"] if q["probe"] == damage)["probe"] = "control"
    elif damage == "final_probe": w["queries"][-1]["probe"] = "control"
    elif damage == "cycles": w["cycles"] += 1
    elif damage == "generation": w["queries"][-1]["generation"] = 1
    elif damage == "restart": w["restarts"][0]["generation"] = 3
    elif damage == "removals": next(o for o in w["operations"] if o["action"] == "corpus_remove")["files"] = []
    elif damage == "convergence": w["convergence"][1]["records"] = 0
    elif damage == "wrong_key": w["queries"][-1]["key"] = "nonexistent-wrong-query"
    elif damage == "unreferenced": w["queries"].append(dict(w["queries"][0]))
    elif damage == "echo":
        q = w["queries"][0]
        q["response"]["results"] = "Q1: soakcontrolanchor confirms synthetic durable control is violet.\nhits: 0"
        q["chars"] = len(q["response"]["results"])
    elif damage == "reordered": w["ledger"][6:8] = reversed(w["ledger"][6:8])
    else: w.pop("ledger")
    with pytest.raises(ValueError, match="incomplete receipt"):
        tool().aggregate([raw])


def test_final_cleanup_removal_requires_absence_probe():
    raw = soak_report()
    for query in raw["worker"]["queries"]:
        if query["probe"] == "cleanup": query["probe"] = "background"
    with pytest.raises(ValueError, match="incomplete receipt"):
        tool().aggregate([raw])


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
               callback_latency_seconds={"p99": 9999}, passed=False)
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
    assert row["callback_by_worker"][0]["callback_seconds"]["count"] == 4
    assert result["convergence_seconds"]["mean"] == 2
    assert result["artifact_bytes"] == 20
    assert result["peak_single_reaped_child_rss_kib"] == 120


@pytest.mark.parametrize("value", [True, -1, float("nan"), float("inf"), "0.1"])
def test_invalid_numeric_samples_are_rejected_not_silently_dropped(value):
    with pytest.raises(ValueError, match="invalid numeric metric"):
        tool().stats([value])
    with pytest.raises(ValueError):
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
    fixed["passed"] = False  # mismatched/incomplete lane is an explicit failure
    fixed["arguments"]["workers"] = 2
    assert tool().aggregate([baseline, fixed], tags=[tags[0], tags[2]])["comparisons"] == []
    fixed["arguments"]["workers"] = 1
    fixed["arguments"]["unknown_condition"] = "different"
    assert tool().aggregate([baseline, fixed], tags=[tags[0], tags[2]])["comparisons"] == []


def test_worker_admission_and_drain_timings_remain_distinct():
    raw = report([0.1])
    raw["workers"][0].update(admission_seconds=1.25, drain_seconds=9.5,
                             pending_at_shutdown={"inbox_pending": False, "private": "secret"})
    data = tool().aggregate([raw])
    row = data["runs"][0]
    assert row["worker_admission_seconds"]["p99"] == 1.25
    assert row["worker_drain_seconds"]["p99"] == 9.5
    rendered = tool().render(data)["aggregate.csv"]
    assert "worker_drain_p99_seconds" in rendered
    assert "secret" not in str(data)


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
    first["recovery"].update(queries=[{"seconds": 1, "hit": True, "chars": 50}])
    second["recovery"].update(queries=[{"seconds": 3, "hit": False, "chars": 10}] * 3)
    first["python"] = "3.11.16 (main, private-host)"
    data = tool().aggregate([first, second, {"passed": False}])
    assert data["query_seconds"]["mean"] == 2.5
    assert data["query_hit_rate"] == .25
    assert data["callback_by_phase"]["add"]["mean"] == 2.0
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
    with pytest.raises(ValueError):
        tool().aggregate([raw])


@pytest.mark.parametrize("damage", [
    "no_workers", "no_samples", "missing_sample", "duplicate_sample", "reordered_sample",
    "unaccepted", "worker_count", "worker_id", "boolean_count", "no_recovery",
    "pending_inbox", "wrong_mirror", "native_no_queries", "native_empty_negatives",
])
def test_success_requires_complete_observed_load_coverage(damage):
    raw = report([.1, .2, .3])
    worker = raw["workers"][0]
    if damage == "no_workers": raw["workers"] = []
    elif damage == "no_samples": worker["samples"] = []
    elif damage == "missing_sample": worker["samples"].pop()
    elif damage == "duplicate_sample": worker["samples"][1] = dict(worker["samples"][0])
    elif damage == "reordered_sample": worker["samples"].reverse()
    elif damage == "unaccepted": worker["samples"][0]["accepted"] = False
    elif damage == "worker_count": worker["successful_callbacks"] = 2
    elif damage == "worker_id": worker["worker"] = True
    elif damage == "boolean_count": raw["arguments"]["workers"] = True
    elif damage == "no_recovery": raw.pop("recovery")
    elif damage == "pending_inbox": raw["recovery"]["inbox_empty"] = False
    elif damage == "wrong_mirror": raw["recovery"]["mirror_records"] = 1
    else:
        raw["mode"] = "native"
        raw["recovery"].update(native_status={"exit": 0}, queries=[], negative_queries=[])
        if damage == "native_empty_negatives":
            raw["recovery"]["queries"] = [{"hit": True, "chars": 1}]
    if damage == "boolean_count":
        with pytest.raises(ValueError):
            tool().summarize(raw, 1)
    else:
        assert tool().summarize(raw, 1)["passed"] is False
    with pytest.raises(ValueError, match="incomplete receipt"):
        tool().aggregate([raw])


def test_partial_failed_coverage_remains_an_attempt():
    raw = report([.1], passed=False)
    result = tool().aggregate([raw])
    assert result["attempts"] == result["failed_attempts"] == 1
    assert result["successful_callbacks"] == 1
    assert result["unfulfilled_callbacks"] == 2


def complete_native_report():
    import hashlib
    raw = report([.1, .2, .3])
    raw.update(mode="native", run="/private/stress-fixture")
    control = "Explicit control fact for worker 0 in stress-fixture."
    token = hashlib.sha256(b"stress-fixture:0:0").hexdigest()[:20]
    def digest(text): return hashlib.sha256(text.encode()).hexdigest()
    raw["recovery"].update(native_status={"exit": 0}, queries=[{
        "key": control, "hit": True, "chars": len(f"#1 facts/control.md:1\nsource:\n1\t{control}\n"), "seconds": .1,
        "sources": {"facts/control.md": control + "\n"},
        "expected_sha256": digest(control), "response": {"results": f"#1 facts/control.md:1\nsource:\n1\t{control}\n"}}],
        negative_queries=[{"key": "stress"+token, "stale": False, "chars": len(zero_hit_body("stress"+token)),
                           "seconds": .1, "response": {"results": zero_hit_body("stress"+token)}, "sources": {},
                           "forbidden_sha256": digest(f"For key stress{token}, the {adjective} answer is payload{token}.")}
                          for adjective in ("obsolete", "verified")])
    return raw


@pytest.mark.parametrize("damage", [None, "missing", "duplicate", "wrong_key", "no_negative", "overflow", "native_error"])
def test_native_coverage_requires_exact_selected_oracles(damage):
    raw = complete_native_report()
    negatives = raw["recovery"]["negative_queries"]
    if damage == "missing": negatives.pop()
    elif damage == "duplicate": negatives[1] = dict(negatives[0])
    elif damage == "wrong_key": negatives[0]["key"] = "not-the-expected-key"
    elif damage == "no_negative": negatives.clear()
    elif damage == "overflow": negatives[0]["chars"] = 2001
    elif damage == "native_error": negatives[0]["response"]["error"] = "failed"
    if damage is None:
        assert tool().aggregate([raw])["passed"] is True
    else:
        assert tool().summarize(raw, 1)["passed"] is False
        with pytest.raises(ValueError): tool().aggregate([raw])


@pytest.mark.parametrize("damage", ["empty", "echo", "wrong_source", "wrong_line", "nonexistent",
                                   "unnumbered", "shape", "chars", "negative_body"])
def test_native_response_evidence_is_independently_validated(damage):
    raw = complete_native_report()
    q = raw["recovery"]["queries"][0]
    control = q["key"]
    if damage == "empty": q["response"]["results"] = ""
    elif damage == "echo": q["response"]["results"] = "Q1: " + control + "\nhits: 0"
    elif damage == "wrong_source": q["sources"]["facts/control.md"] = "unrelated\n"
    elif damage == "wrong_line": q["response"]["results"] = f"#1 facts/control.md:1\nsource:\n2\t{control}\n"
    elif damage == "nonexistent": q["sources"] = {}
    elif damage == "unnumbered": q["response"]["results"] = f"#1 facts/control.md:1\n{control}\n"
    elif damage == "shape": q["response"]["results"] = []
    elif damage == "chars": q["chars"] = 0
    else:
        import hashlib
        token = hashlib.sha256(b"stress-fixture:0:0").hexdigest()[:20]
        forbidden = f"For key stress{token}, the obsolete answer is payload{token}."
        q = raw["recovery"]["negative_queries"][0]
        q["sources"] = {"facts/stale.md": forbidden + "\n"}
        q["response"]["results"] = f"#1 facts/stale.md:1\nsource:\n1\t{forbidden}\n"
    if damage != "chars": q["chars"] = len(q["response"]["results"])
    with pytest.raises(ValueError, match="incomplete receipt"):
        tool().aggregate([raw])


def regression_report():
    return {"schema_version": 1, "lane": "regression", "mode": "offline", "passed": True,
            "arguments": {"iterations": 1}, "planned_iterations": 1, "completed_iterations": 1,
            "planned_callbacks": 0, "successful_callbacks": 0, "elapsed_seconds": 1,
            "workers": [], "iterations": [{"number": 0, "exit": 0, "tests": 3,
                "skipped": 0, "failures": 0, "errors": 0, "diagnostic_errors": [], "elapsed_seconds": .5}]}


@pytest.mark.parametrize("damage", [None, "missing", "empty", "skipped", "failures", "errors", "short", "diagnostic"])
def test_regression_success_requires_observed_junit_coverage(damage):
    raw = regression_report()
    item = raw["iterations"][0]
    if damage == "missing": item.pop("tests")
    elif damage == "empty": item["tests"] = 0
    elif damage in ("skipped", "failures", "errors"): item[damage] = 1
    elif damage == "short": raw["completed_iterations"] = 0
    elif damage == "diagnostic": item["diagnostic_errors"] = ["private failure"]
    if damage is None:
        result = tool().aggregate([raw])
        assert result["passed"] is True
        assert result["runs"][0]["planned_iterations"] == 1
        assert result["runs"][0]["completed_iterations"] == 1
        assert result["runs"][0]["junit"]["tests"] == 3
        assert not result["error_counts"]
    else:
        assert tool().summarize(raw, 1)["passed"] is False
        with pytest.raises(ValueError): tool().aggregate([raw])


@pytest.mark.parametrize("legacy", [False, True])
def test_failed_iteration_diagnostics_survive_export(legacy):
    raw = regression_report()
    raw["passed"] = False
    raw["iterations"][0]["errors" if legacy else "diagnostic_errors"] = ["private failure"]
    result = tool().aggregate([raw])
    assert result["passed"] is False and result["attempts"] == 1
    assert result["error_counts"] == {"other": 1}
    assert "private failure" not in str(result)


@pytest.mark.parametrize("damage", ["short", "no_samples", "missed_restart", "missed_removal"])
def test_soak_success_requires_full_requested_coverage(damage):
    raw = soak_report()
    if damage == "short": raw["arguments"]["duration"] = 1800
    elif damage == "no_samples": raw["resources"]["sample_count"] = 0
    elif damage == "missed_restart":
        raw["arguments"]["engine_restart_at"] = 2
        raw["worker"]["restarts"] = []
    elif damage == "missed_removal": raw["worker"]["corpus_removed"] = False
    with pytest.raises(ValueError): tool().aggregate([raw])


def test_shutdown_samples_and_recovery_remain_separate():
    raw = report([.1])
    raw["workers"][0]["shutdown_seconds"] = 2.5
    raw["recovery"]["shutdown_seconds"] = .3
    data = tool().aggregate([raw, report([.1])])
    first, historical = data["runs"]
    assert first["worker_shutdown_seconds"]["count"] == 1
    assert first["worker_shutdown_seconds"]["mean"] == 2.5
    assert first["recovery_shutdown_seconds"] == .3
    assert historical["worker_shutdown_seconds"]["count"] == 0
    assert historical["worker_shutdown_seconds"]["mean"] is None
    assert historical["recovery_shutdown_seconds"] is None
    csv = tool().render(data)["aggregate.csv"]
    assert "worker_shutdown_p99_seconds" in csv and "recovery_shutdown_seconds" in csv


def test_comparison_binds_instrument_and_installed_host_not_product_path():
    import json
    baseline, fixed = report([2]), report([1])
    for raw, source in ((baseline,"a"),(fixed,"b")):
        raw["arguments"]["provider_source"] = "/private/"+source
        raw["product_identity"] = {"kind":"source_snapshot","content_sha256":source*64,
                                   "files":{"/private/secret":"not public"}}
        raw["harness_identity"] = {"kind":"source_snapshot","content_sha256":"c"*64}
        raw["host_provenance"] = {"kind":"installed_snapshot","content_sha256":"d"*64}
    tags = [{"scenario":"same","variant":"baseline"},{"scenario":"same","variant":"fix"}]
    result = tool().aggregate([baseline,fixed],tags=tags)
    assert len(result["comparisons"]) == 1
    assert result["comparisons"][0]["identity_basis"] == "content_fingerprints"
    assert result["comparisons"][0]["baseline_product_content_sha256"] == "a"*64
    assert result["comparisons"][0]["fix_product_content_sha256"] == "b"*64
    import csv, io
    rendered = tool().render(result)
    rows = list(csv.DictReader(io.StringIO(rendered["aggregate.csv"])))
    assert rows[0]["product_content_sha256"] == "a"*64
    assert rows[1]["harness_content_sha256"] == "c"*64
    assert rows[1]["host_content_sha256"] == "d"*64
    assert "harness content fingerprints" in rendered["aggregate.md"]
    assert result["runs"][0]["product_identity"]["content_sha256"] == "a"*64
    assert result["runs"][1]["product_identity"]["content_sha256"] == "b"*64
    assert "/private" not in json.dumps(result) and "not public" not in json.dumps(result)
    fixed["harness_identity"]["content_sha256"] = "e"*64
    assert tool().aggregate([baseline,fixed],tags=tags)["comparisons"] == []
    fixed["harness_identity"]["content_sha256"] = "c"*64
    fixed["host_provenance"]["content_sha256"] = "f"*64
    assert tool().aggregate([baseline,fixed],tags=tags)["comparisons"] == []


@pytest.mark.parametrize("variant", ["baseline", "fix"])
def test_comparison_rejects_mixed_products_within_one_side(variant):
    raws = [report([1]), report([2]), report([3], passed=False)]
    for raw, fingerprint in zip(raws, "abc"):
        raw["product_identity"] = {"kind": "source_snapshot", "content_sha256": fingerprint * 64}
        raw["harness_identity"] = {"content_sha256": "d" * 64}
        raw["host_provenance"] = {"content_sha256": "e" * 64}
        raw["arguments"]["provider_source"] = "/private/" + fingerprint
    tags = [{"scenario": "same", "variant": "baseline"}, {"scenario": "same", "variant": "fix"},
            {"scenario": "same", "variant": variant}]
    with pytest.raises(ValueError, match="multiple product fingerprints"):
        tool().aggregate(raws, tags=tags)
    raws[2]["product_identity"] = dict(raws[0 if variant == "baseline" else 1]["product_identity"])
    comparison = tool().aggregate(raws, tags=tags)["comparisons"][0]
    assert comparison[variant + "_attempts"] == 2
    assert comparison[variant + "_failed_attempts"] == 1
