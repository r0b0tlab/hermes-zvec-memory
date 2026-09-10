"""Harness tests: never opt into the native engine."""
import importlib.util
import json
from pathlib import Path
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/stress_memory.py"


def module():
    assert SCRIPT.exists(), "stress runner has not been implemented"
    spec = importlib.util.spec_from_file_location("stress_memory", SCRIPT)
    assert spec is not None and spec.loader is not None
    obj = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(obj)
    return obj


def test_environment_is_constructed_not_inherited(tmp_path, monkeypatch):
    s = module()
    monkeypatch.setenv("ZVEC_GREP_SERVER_URL", "http://127.0.0.1:17999/mcp")
    monkeypatch.setenv("OPENAI_API_KEY", "poison")
    env = s.environment(tmp_path, ROOT)
    assert not {"ZVEC_GREP_SERVER_URL", "OPENAI_API_KEY", "PYTHONPATH"} & env.keys()
    assert env["ZVEC_GREP_MODE"] == "direct"
    assert env["HF_HUB_OFFLINE"] == "1"
    for key in ("HOME", "HERMES_HOME", "XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_CACHE_HOME", "ZVEC_GREP_HOME"):
        assert Path(env[key]).is_relative_to(tmp_path)
    assert "zg-default" not in str(env)


def test_offline_load_smoke():
    child = subprocess.run([sys.executable, str(SCRIPT), "--lane", "load", "--mode", "offline",
                            "--records", "2", "--timeout", "20"],
                           cwd=ROOT, capture_output=True, text=True, timeout=30)
    assert child.stdout.strip(), child.stderr
    pointer = json.loads(child.stdout)
    report = json.loads(Path(pointer["report"]).read_text())
    assert child.returncode == 0, report
    assert report["passed"] is True
    assert report["evidence"] == "offline engine mock; NOT native evidence"
    assert report["successful_callbacks"] == report["planned_callbacks"] == 5
    assert report["recovery"]["mirror_records"] == 1
    assert report["recovery"]["errors"] == []
    assert report["elapsed_seconds"] > 0
    assert report["callback_latency_by_phase"]["add"]["count"] == 2
    assert report["artifact_inventory"]["file_count"] > 0


def test_regression_lane_receipts(tmp_path, monkeypatch, capsys):
    s = module()
    monkeypatch.setattr(s, "RUNS", tmp_path)
    calls = []
    def command(cmd, env, log, timeout):
        calls.append((cmd, env, timeout))
        xml = Path(cmd[cmd.index("--junitxml")+1])
        xml.write_text('<testsuites><testsuite tests="3" skipped="0" failures="0" errors="0"/></testsuites>')
        return 0
    monkeypatch.setattr(s, "run_command", command)
    assert s.main(["--lane", "regression", "--iterations", "2"]) == 0
    report = json.loads(Path(json.loads(capsys.readouterr().out)["report"]).read_text())
    assert len(report["iterations"]) == 2
    assert report["iterations"][0]["tests"] == 3
    assert report["iterations"][0]["elapsed_seconds"] >= 0
    assert report["planned_iterations"] == report["completed_iterations"] == 2
    assert len(calls) == 2
    assert "tests/test_durable_recovery.py" in calls[0][0]
    assert "ZVEC_RUN_NATIVE" not in calls[0][1]


@pytest.mark.parametrize("xml", [
    '<testsuites><testsuite tests="1" skipped="1"/></testsuites>',
    '<testsuite tests="0"/>', '<testsuite tests="1" failures="1"/>',
    '<testsuite tests="1" errors="1"/>'])
def test_invalid_junit_is_not_success(tmp_path, xml):
    s = module()
    path = tmp_path / "junit.xml"
    path.write_text(xml)
    with pytest.raises(ValueError):
        s.read_junit(path)


def test_native_regression_selects_real_tests(tmp_path, monkeypatch, capsys):
    s = module()
    monkeypatch.setattr(s, "RUNS", tmp_path)
    calls = []
    def command(cmd, env, log, timeout):
        calls.append((cmd, env))
        Path(cmd[cmd.index("--junitxml")+1]).write_text('<testsuite tests="2"/>')
        return 0
    monkeypatch.setattr(s, "run_command", command)
    assert s.main(["--lane", "native-regression", "--iterations", "1"]) == 0
    report = json.loads(Path(json.loads(capsys.readouterr().out)["report"]).read_text())
    assert report["mode"] == "native"
    assert calls[0][1]["ZVEC_RUN_NATIVE"] == "1"
    assert calls[0][1]["ZVEC_TEST_BIN"] == str(s.ZG)
    assert "tests/test_zg_native.py" in calls[0][0]
    assert "NODE_OPTIONS" in calls[0][1]


def test_native_queries_require_cited_bodies(tmp_path):
    s = module()
    class Reader:
        def handle_tool_call(self, name, args):
            return json.dumps({"results": "query\nQ1: " + s.expected("run", 1, 2)[0]})
    result = {"errors": [], "queries": []}
    s.native_queries(Reader(), s.expected("run", 1, 2), result)
    assert result["errors"]
    assert result["queries"][0]["hit"] is False
    assert result["hit_at_5"] == 0


@pytest.mark.parametrize("args", [["--workers", "99"], ["--records", "0"],
    ["--iterations", "501"], ["--seed-facts", "10001"], ["--timeout", "0"],
    ["--run", "/tmp/ignored"], ["--worker", "-1"], ["--campaign-timeout", "1801"],
    ["--lane", "regression", "--mode", "native"]])
def test_rejects_unsafe_arguments_before_workspace(tmp_path, monkeypatch, args):
    s = module()
    monkeypatch.setattr(s, "RUNS", tmp_path / "runs")
    monkeypatch.setattr(s, "regression_campaign", lambda *a: None)
    with pytest.raises(SystemExit) as exc:
        s.main(args)
    assert exc.value.code == 2
    assert not s.RUNS.exists()


def test_completed_leader_with_live_descendant_is_failure(tmp_path):
    s = module()
    # This child is owned by this test; no discovery by process names.
    code = "import subprocess,sys; subprocess.Popen([sys.executable,'-c','import time; time.sleep(20)'])"
    assert s.run_command([sys.executable, "-c", code], s.environment(tmp_path, ROOT), tmp_path / "child.log", 3) == 125


def test_phase_deadline_preserves_worker_receipts(tmp_path, monkeypatch):
    from types import SimpleNamespace
    s = module()
    s.prepare_home(tmp_path)
    real_popen = subprocess.Popen
    def sleeping_child(cmd, **kwargs):
        return real_popen([sys.executable, "-c", "import time; time.sleep(20)"], **kwargs)
    monkeypatch.setattr(s.subprocess, "Popen", sleeping_child)
    recoveries = []
    monkeypatch.setattr(s, "run_command", lambda *a: recoveries.append(a))
    args = SimpleNamespace(workers=2, records=2, seed_facts=0, mode="offline", timeout=.1,
                           recovery_timeout=.1)
    report = {"errors": [], "workers": []}
    s.load_campaign(tmp_path, args, report)
    assert report["errors"]
    assert len(report["workers"]) == 2
    assert all(w["errors"] for w in report["workers"])
    assert not recoveries


def test_campaign_deadline_receipt(tmp_path, monkeypatch, capsys):
    import time
    s = module()
    monkeypatch.setattr(s, "RUNS", tmp_path)
    monkeypatch.setattr(s, "load_campaign", lambda *a: time.sleep(2))
    start = time.monotonic()
    assert s.main(["--lane", "load", "--campaign-timeout", "1"]) == 1
    report = json.loads(Path(json.loads(capsys.readouterr().out)["report"]).read_text())
    assert any("deadline" in str(e) for e in report["errors"])
    assert time.monotonic()-start < 1.8
    assert report["status"] == "finished"


def test_recovery_timeout_is_forwarded(tmp_path, monkeypatch):
    from types import SimpleNamespace
    s = module()
    # Zero workers is an internal fixture, not accepted by the CLI.
    args = SimpleNamespace(workers=0, records=2, seed_facts=0, mode="offline", timeout=1, recovery_timeout=3)
    calls = []
    def command(cmd, env, log, timeout):
        calls.append((cmd, timeout))
        return 124
    monkeypatch.setattr(s, "run_command", command)
    report = {"errors": [], "workers": []}
    s.load_campaign(tmp_path, args, report)
    assert "--recovery-timeout" in calls[0][0]
    assert calls[0][1] == 13  # convergence budget plus one shutdown grace
    assert report["recovery"]["errors"]
    assert (tmp_path / "recovery-failure.json").exists()


def test_sources_reject_escaping_paths_and_duplicate_content(tmp_path):
    s = module()
    vault = tmp_path / "vault"
    (vault / "facts").mkdir(parents=True)
    content = s.expected("run", 1, 2)[0]
    (vault / "facts/a.md").write_text(content)
    state = {"records": {"a": {"content": content, "path": "facts/a.md"}}}
    assert s.validate_sources(vault, state, [], []) == []
    (vault / "facts/duplicate.md").write_text(content)
    assert s.validate_sources(vault, state, [], [])
    (vault / "facts/duplicate.md").unlink()
    state["records"]["a"]["path"] = "../../outside.md"
    assert s.validate_sources(vault, state, [], [])


def test_internal_owner_marker_is_validated(tmp_path, monkeypatch):
    s = module()
    monkeypatch.setattr(s, "RUNS", tmp_path)
    run = tmp_path / "fake"
    run.mkdir()
    (run / "OWNER.json").write_text('{}')
    called = []
    monkeypatch.setattr(s, "worker", lambda *a: called.append(a))
    with pytest.raises(SystemExit) as exc:
        s.main(["--worker", "0", "--run", str(run)])
    assert exc.value.code == 2
    assert not called


def test_initialization_failure_keeps_zero_acceptance_receipt(tmp_path, monkeypatch):
    s = module()
    def fail(*a):
        raise RuntimeError("injected startup failure")
    monkeypatch.setattr(s, "provider", fail)
    assert s.worker(tmp_path, 0, 2, "offline") == 1
    receipt = json.loads((tmp_path / "worker-0.json").read_text())
    assert receipt["successful_callbacks"] == 0
    assert receipt["planned_callbacks"] == 5
    assert "startup failure" in receipt["errors"][0]
    assert "injected startup failure" in receipt["traceback"]
    assert s.recover(tmp_path, 1, 2, "offline") == 1
    assert json.loads((tmp_path / "recovery.json").read_text())["errors"]


def test_node_guard_blocks_network_without_connecting(tmp_path):
    s = module()
    s.prepare_home(tmp_path)
    result = subprocess.run(["/usr/bin/node", "-e", "require('net').connect(9, '127.0.0.1')"],
                            env=s.environment(tmp_path, ROOT), capture_output=True, text=True, timeout=3)
    assert result.returncode != 0
    assert "stress: network disabled" in result.stderr


def test_callback_progress_survives_failed_shutdown(tmp_path, monkeypatch):
    s = module()
    class Provider:
        def on_memory_write(self, *a):
            pass
        def handle_tool_call(self, *a):
            raise RuntimeError("control failed")
        def shutdown(self):
            raise RuntimeError("shutdown failed")
    monkeypatch.setattr(s, "provider", lambda *a: Provider())
    assert s.worker(tmp_path, 0, 2, "offline") == 1
    rows = [json.loads(line) for line in (tmp_path / "worker-0-callbacks.jsonl").read_text().splitlines()]
    assert len(rows) == 5
    assert rows[-1]["phase"] == "remove"
    assert all(row["accepted"] for row in rows)


def test_acceptance_oracle_rejects_duplicate_operations():
    s = module()
    samples = [{"phase": "add", "record": 0}] * 5
    assert s.validate_acceptance({"worker": 0, "samples": samples}, 0, 2)
    samples = [{"phase": p, "record": n} for p in ("add", "replace", "remove")
               for n in range(2) if p != "remove" or not n % 2]
    assert s.validate_acceptance({"worker": 0, "samples": samples}, 0, 2) == []


def test_recovery_interrupt_has_receipt(tmp_path, monkeypatch):
    from types import SimpleNamespace
    s = module()
    def fail(*a):
        raise RuntimeError("campaign deadline exceeded")
    monkeypatch.setattr(s, "run_command", fail)
    args = SimpleNamespace(workers=0, records=2, seed_facts=0, mode="offline", timeout=1, recovery_timeout=3)
    report = {"errors": [], "workers": []}
    s.load_campaign(tmp_path, args, report)
    assert report["recovery"]["errors"]
    assert (tmp_path / "recovery-failure.json").exists()


def test_internal_worker_scrubs_even_direct_invocation(tmp_path, monkeypatch):
    s = module()
    monkeypatch.setattr(s, "RUNS", tmp_path)
    run = tmp_path / "owned"
    s.prepare_home(run)
    s.write_json(run / "OWNER.json", {"kind": "zvec-stress", "repo": str(ROOT)})
    monkeypatch.setattr(s.os, "environ", {"ZVEC_GREP_SERVER_URL": "poison", "OPENAI_API_KEY": "poison"})
    captured = []
    monkeypatch.setattr(s, "worker", lambda *a: captured.append(dict(s.os.environ)))
    s.main(["--worker", "0", "--run", str(run)])
    assert "ZVEC_GREP_SERVER_URL" not in captured[0]
    assert "OPENAI_API_KEY" not in captured[0]
    assert captured[0]["HOME"] == str(run / "home")


def test_native_negative_hits_fail(tmp_path):
    s = module()
    content = s.fact("run", 0, 0, True)
    class Reader:
        def handle_tool_call(self, *a):
            return json.dumps({"results": "query\n#1 facts/a.md:1\n" + content})
    result = {"errors": [], "queries": []}
    s.native_queries(Reader(), [], result, forbidden=[content])
    assert result["errors"]
    assert result["negative_queries"][0]["stale"] is True


def test_partial_initialization_is_closed(tmp_path, monkeypatch):
    s = module()
    class Provider:
        closed = False
        def initialize(self, *a, **k):
            raise RuntimeError("partial initialization")
        def shutdown(self):
            self.closed = True
    obj = Provider()
    with pytest.raises(RuntimeError, match="partial initialization"):
        s.initialize_provider(obj, tmp_path, "session")
    assert obj.closed


def test_inventory_growth_metadata(tmp_path, monkeypatch, capsys):
    s = module()
    monkeypatch.setattr(s, "RUNS", tmp_path)
    def load(run, args, report):
        report["initial_inventory"] = s.inventory(run)
        (run / "new-file").write_text("abc")
    monkeypatch.setattr(s, "load_campaign", load)
    assert s.main(["--lane", "load", "--records", "1"]) == 1
    report = json.loads(Path(json.loads(capsys.readouterr().out)["report"]).read_text())
    assert report["artifact_growth"]["file_count"] == 1
    assert report["artifact_growth"]["bytes"] == 3
    assert report["artifact_inventory"]["scope"] == "snapshot before final report write; excludes symlinks"


def test_reaps_killed_descendants(tmp_path):
    s = module()
    pidfile = tmp_path / "descendant.pid"
    code = ("import subprocess,sys,pathlib; p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(20)']); "
            f"pathlib.Path({str(pidfile)!r}).write_text(str(p.pid))")
    assert s.run_command([sys.executable, "-c", code], s.environment(tmp_path, ROOT), tmp_path / "child.log", 3) == 125
    pid = int(pidfile.read_text())
    assert not Path(f"/proc/{pid}").exists(), "owned descendant must be reaped, not merely signalled"


def test_cleanup_never_signals_reusable_numeric_process_groups(tmp_path, monkeypatch):
    s = module()
    def forbidden(*a):
        raise AssertionError("numeric process-group signaling is unsafe")
    monkeypatch.setattr(s.os, "killpg", forbidden)
    assert s.run_command([sys.executable, "-c", "pass"], s.environment(tmp_path, ROOT), tmp_path / "child.log", 3) == 0


def test_pin_identity_closes_fd_on_mismatch(monkeypatch):
    s = module()
    closed = []
    real_close = s.os.close
    def close(fd):
        closed.append(fd)
        real_close(fd)
    monkeypatch.setattr(s.os, "close", close)
    with pytest.raises(RuntimeError, match="identity"):
        s.pin_identity(s.os.getpid(), (-1, -1, -1))
    assert len(closed) == 1


def test_exact_oracle():
    s = module()
    wanted = s.expected("run", 2, 3)
    assert len(wanted) == 2
    assert s.fact("run", 1, 1, True) in wanted
    assert s.fact("run", 0, 0, True) not in wanted
    row = {"content": wanted[0], "path": "facts/a.md"}
    good = {"records": {"a": row}}
    assert s.validate_state(good, wanted[:1]) == []
    assert s.validate_state(good, wanted)
    assert s.validate_state({"records": {"a": row, "b": row}}, wanted[:1])
    assert s.validate_state(good | {"refresh_required": True}, wanted[:1])
    assert s.hit_body("query groups (1):\nQ1: answer\nhits: 0\n") == ""
    assert s.hit_body("query\n#1 facts/a.md:7\nanswer") == "facts/a.md:7\nanswer"
