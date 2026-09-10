"""Offline tests: no native engine, production daemon, or model execution."""
import importlib.util
import os
from pathlib import Path
import sys
import subprocess
import time
import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("soak_memory", ROOT / "scripts/soak_memory.py")
assert spec is not None and spec.loader is not None and spec.origin is not None
soak = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = soak
if Path(spec.origin).exists():
    spec.loader.exec_module(soak)


def test_environment_is_scrubbed_and_server_only(tmp_path, monkeypatch):
    monkeypatch.setenv("ZVEC_GREP_SERVER_URL", "http://127.0.0.1:17999/mcp")
    monkeypatch.setenv("OPENAI_API_KEY", "private")
    env = soak.environment(tmp_path, 23456)
    assert env["ZVEC_GREP_SERVER_URL"] == "http://127.0.0.1:23456/mcp"
    assert env["ZVEC_GREP_MODE"] == "server"
    assert "OPENAI_API_KEY" not in env
    assert Path(env["HOME"]).is_relative_to(tmp_path)
    assert Path(env["ZVEC_GREP_SERVER_TOKEN_FILE"]).is_relative_to(tmp_path)
    assert "17999" not in str(env)
    assert soak.server_args(["query", "--mode", "auto", "--fts=hello"]) == [
        "query", "--fts=hello", "--mode", "server"]
    assert soak.server_args(["index", "vault"]) == ["index", "vault", "--mode", "server"]


def test_owned_tree_metrics_and_cleanup_leave_unrelated_alive():
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(20)"], start_new_session=True)
    stranger = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(20)"], start_new_session=True)
    try:
        tracker = soak.ProcessTree(child.pid)
        sample = tracker.sample("warm", 1)
        assert sample["rss_sum_bytes"] > 0
        assert sample["rss_semantics"] == "concurrent_tree_sum_shared_pages_overcounted"
        assert sample["process_count"] == 1
        assert sample["cpu_seconds"] >= 0 and sample["threads"] >= 1
        assert sample["monotonic_seconds"] > 0 and sample["unix_seconds"] > 0
        assert tracker.stop(grace=.1) == []
        child.wait(timeout=2)
        assert stranger.poll() is None
    finally:
        for p in (child, stranger):
            if p.poll() is None:
                p.kill()
            p.wait()


def test_pid_reuse_is_never_owned_or_signalled(monkeypatch):
    rows = {123: {"pid": 123, "start_ticks": 10, "ppid": 1, "pgrp": 123}}
    monkeypatch.setattr(soak, "proc_rows", lambda: rows)
    tracker = soak.ProcessTree(123)
    rows[123] = dict(rows[123], start_ticks=11)
    assert tracker.identities() == {}
    assert tracker.stop(grace=0) == []


def test_trend_uses_actual_times_and_is_diagnostic():
    rows = [{"monotonic_seconds": t, "rss_sum_bytes": 100 + 2*t,
             "phase": "warm", "generation": 1} for t in (2, 4, 10)]
    result = soak.trend(rows)
    assert result["rss_bytes_per_second"] == pytest.approx(2)
    assert result["classification"] == "diagnostic_not_leak_proof"
    assert soak.trend(rows[:1])["rss_bytes_per_second"] is None


def test_owned_daemon_restart_auth_and_new_identity(tmp_path, monkeypatch):
    fake = tmp_path / "fake-engine"
    fake.write_text(f'''#!{sys.executable}
import http.server, json, os, pathlib, uuid
home = pathlib.Path(os.environ["ZVEC_GREP_HOME"])/"daemon"
home.mkdir(parents=True, exist_ok=True)
url = os.environ["ZVEC_GREP_SERVER_URL"]
token = pathlib.Path(os.environ["ZVEC_GREP_SERVER_TOKEN_FILE"]).read_text().strip()
class Handler(http.server.BaseHTTPRequestHandler):
 def do_GET(self):
  code = 200 if self.path == "/healthz" else (400 if self.headers.get("Authorization") == "Bearer "+token else 401)
  self.send_response(code); self.end_headers(); self.wfile.write(b"{{}}")
 def log_message(self, *a): pass
server = http.server.HTTPServer(("127.0.0.1", int(url.split(":")[2].split("/")[0])), Handler)
(home/"instance.lock").write_text(json.dumps(dict(pid=os.getpid(), instanceToken=uuid.uuid4().hex, ready=True, serverUrl=url)))
server.serve_forever()
''')
    fake.chmod(0o700)
    monkeypatch.setattr(soak, "ZG", fake)
    port = soak.free_port()
    env = soak.environment(tmp_path, port)
    (tmp_path / "token").write_text("test-token-" * 8)
    daemon = soak.Daemon(tmp_path, env)
    try:
        first = daemon.start(time.monotonic()+3)
        assert first["unauthenticated_status"] == 401
        second = daemon.restart(time.monotonic()+3)
        assert second["pid"] != first["pid"]
        assert second["generation"] == 2
        assert daemon.process.poll() is None
    finally:
        assert daemon.stop() == []


def test_readiness_rejects_wrong_pid_or_token(tmp_path):
    record = {"pid": 100, "ready": True, "instanceToken": "identity", "serverUrl": "url"}
    assert soak.ready_identity(record, 100, "url")
    assert not soak.ready_identity(record, 101, "url")
    assert not soak.ready_identity(dict(record, instanceToken=""), 100, "url")
    assert not soak.ready_identity(dict(record, ready=False), 100, "url")


@pytest.mark.parametrize("restart_at", [None, 0])
def test_same_host_handles_churn_and_remove_corpus(tmp_path, monkeypatch, restart_at):
    import json
    from types import SimpleNamespace
    from test_provider import ZvecMemoryProvider
    calls = []
    def native_boundary(self, args, timeout):
        calls.append(args[0])
        if args[0] == "index":
            (self._vault / ".zvec-grep").mkdir(exist_ok=True)
            (self._vault / ".zvec-grep/manifest.json").write_text("{}")
            return 0, "indexed", ""
        query = next((a.split("=", 1)[1] for a in args if a.startswith(("--fts=", "--hybrid="))), "")
        hits = [p for p in (self._vault / "facts").glob("*.md") if query in p.read_text()]
        body = "query groups (1):\nQ1: " + query + "\nhits: " + str(len(hits))
        for i, path in enumerate(hits, 1):
            body += f"\n#{i} facts/{path.name}:1\n" + path.read_text()
        return 0, body, ""
    monkeypatch.setattr(ZvecMemoryProvider, "_run_zg", native_boundary)
    monkeypatch.setattr(ZvecMemoryProvider, "is_available", lambda self: True)
    args = SimpleNamespace(duration=.4, seed_facts=2, engine_restart_at=restart_at,
                           convergence_timeout=3, sample_interval=.05)
    report = {"errors": [], "queries": [], "callbacks": [], "convergence": [], "restarts": []}
    class FakeDaemon:
        generation = 1
        process = SimpleNamespace(poll=lambda: None)
        def restart(self, deadline):
            self.generation += 1
            return {"generation": self.generation, "pid": 123}
    handles = []
    def factory(run, number):
        p = ZvecMemoryProvider(config={"vault": str(run / "vault"), "zg_bin": "/no/native",
            "context_chars": 2000, "reindex_min_seconds": 0})
        p.initialize(f"soak-{number}", hermes_home=str(tmp_path / "home/hermes"))
        handles.append(p)
        return p
    try:
        soak.workload(tmp_path, args, report, FakeDaemon(), factory)
        assert len(handles) == 2
        assert len(report["restarts"]) == int(restart_at is not None)
        assert report["cycles"] >= 1
        assert report["corpus_removed"] is True
        assert report["final_mirror_records"] == 0
        assert report["queries"] and all(q["hit"] for q in report["queries"] if q["required"])
        assert report["same_handle_ids"] == [id(p) for p in handles]
        assert {q["kind"] for q in report["prefetch"]} == {"repeated_query", "distinct_query"}
        assert all(q["chars"] <= 2000 for q in report["prefetch"])
        assert "index" in calls
        assert not list((tmp_path / "vault/facts").glob("background-*.md"))
    finally:
        for p in handles:
            p.shutdown()


def test_query_echo_does_not_pass_retrieval():
    assert not soak.retrieval_hit("query groups (1):\nQ1: secret\nhits: 0", "secret")
    assert soak.retrieval_hit("\n#1 facts/a.md:1\nsecret", "secret")


def test_initializer_failure_still_writes_safe_receipt(tmp_path, monkeypatch):
    from types import SimpleNamespace
    import json
    class BrokenDaemon:
        def __init__(self, *a): pass
        def start(self, deadline): raise RuntimeError("sensitive-token /home/private")
        def stop(self): return []
    monkeypatch.setattr(soak, "Daemon", BrokenDaemon)
    args = SimpleNamespace(duration=1, seed_facts=2, engine_restart_at=None,
                           convergence_timeout=1, sample_interval=.1)
    assert soak.worker(tmp_path, args, soak.environment(tmp_path, 23456)) == 1
    text = (tmp_path / "worker.json").read_text()
    assert "sensitive-token" not in text and "/home/private" not in text
    assert json.loads(text)["errors"] == ["worker_exception:RuntimeError"]
    assert (tmp_path / "worker-error.private.log").exists()


def test_supervisor_timeout_writes_receipt_and_reaps_child(tmp_path):
    import json
    report = soak.supervise(tmp_path, [sys.executable, "-c", "import time; time.sleep(10)"],
                            soak.environment(tmp_path, 23456), budget=.15, interval=.05)
    assert report["passed"] is False
    assert "whole_command_timeout" in report["errors"]
    assert report["resources"]["sample_count"] >= 1
    assert report["leftover_owned_pids"] == []
    assert json.loads((tmp_path / "report.json").read_text())["passed"] is False


def test_cli_bounds_and_missing_native_receipt(tmp_path, monkeypatch, capsys):
    import json
    monkeypatch.setattr(soak, "RUNS", tmp_path / "runs")
    monkeypatch.setattr(soak, "ZG", tmp_path / "missing-raw-engine")
    for argv in (["--duration", "1801"], ["--duration", "nan"], ["--seed-facts", "99999"],
                 ["--duration", "1", "--engine-restart-at", "2"]):
        with pytest.raises(SystemExit) as exc:
            soak.main(argv)
        assert exc.value.code == 2
    assert not (tmp_path / "runs").exists()
    assert soak.main(["--duration", "1"]) == 1
    pointer = json.loads(capsys.readouterr().out)
    report = json.loads(Path(pointer["report"]).read_text())
    assert report["errors"] == ["preflight_exception:FileNotFoundError"]
    assert "missing-raw-engine" not in json.dumps(report)


def test_native_latency_summary_uses_real_rows_and_separates_cold():
    rows = [{"kind": "index", "phase": phase, "seconds": seconds, "exit": 0}
            for phase, seconds in (("cold_start", 30), ("warm", 1), ("warm", 3))]
    result = soak.latency_summary(rows)
    assert result["index:warm"]["p99_seconds"] == 3
    assert result["index:warm"]["over_prefetch_2s_budget"] == 1
    assert result["index:cold_start"]["p99_seconds"] == 30


def test_source_oracle_rejects_orphaned_old_generation(tmp_path):
    (tmp_path / "facts").mkdir()
    path = tmp_path / "facts/a.md"
    path.write_text("# Fact\nsoakslot00 revisedanswer cycle00000001 is indigo.\n")
    with pytest.raises(RuntimeError, match="source"):
        soak.validate_sources(tmp_path, [])
    soak.validate_sources(tmp_path, ["soakslot00 revisedanswer cycle00000001 is indigo."])


def test_transport_launcher_pins_server_and_records_private_free_metrics(tmp_path, monkeypatch):
    import json
    fake = tmp_path / "fake-cli"
    fake.write_text(f"#!{sys.executable}\nimport sys\nassert '--mode' in sys.argv\nassert sys.argv[sys.argv.index('--mode')+1] == 'server'\n")
    fake.chmod(0o700)
    monkeypatch.setattr(soak, "ZG", fake)
    monkeypatch.setattr(sys, "argv", ["launcher", "query", "--mode", "auto", "--fts=private-query"])
    assert soak.transport_main(tmp_path) == 0
    text = (tmp_path / "native-commands.jsonl").read_text()
    assert "private-query" not in text and str(tmp_path) not in text
    assert json.loads(text)["kind"] == "query"


def test_termination_handler_is_catchable():
    with pytest.raises(InterruptedError):
        soak.termination_handler(15, None)


def test_daemon_constructor_failure_still_has_receipt(tmp_path, monkeypatch):
    from types import SimpleNamespace
    class BrokenDaemon:
        def __init__(self, *args): raise ValueError("private")
    monkeypatch.setattr(soak, "Daemon", BrokenDaemon)
    assert soak.worker(tmp_path, SimpleNamespace(), {}) == 1
    assert (tmp_path / "worker.json").exists()
