#!/usr/bin/env python3
"""Opt-in same-handle native soak; private artifacts under .test-tools/soak-runs.

No LLM calls. Use an outer MemoryMax=2G/CPUQuota=200%/TasksMax=128 cgroup.
Duration is active churn time; startup/recovery/cleanup have an additional 240s
whole-command allowance. No arbitrary home, output, engine or URL arguments.

Examples (run one campaign at a time under the external resource limits):
  .venv/bin/python scripts/soak_memory.py --duration 60 --engine-restart-at 30
  .venv/bin/python scripts/soak_memory.py --duration 1800 --engine-restart-at 900

Receipt v1: report.json contains errors/passed, worker (callbacks, convergence,
queries, repeated/distinct prefetch, restart identities), native_latency by
command/phase, resources (sample count, peak sampled RSS sum, grouped slopes),
versions and arguments. resources.jsonl retains actual sample times and each
PID/start-time's RSS, CPU, FDs and threads. native-commands.jsonl contains only
safe command kinds/timings/status, never argv or output. *.private.log, token,
and the generated vault are PRIVATE; report.json has no absolute paths/secrets.

RSS sum double-counts shared pages and is NOT PSS. Sampled CPU can miss short
children. Slopes are diagnostics, not proof of a leak or its absence. Warm
corpus removal and engine generations are separate groups; short runs may not
have enough samples for a trend. SIGTERM/ordinary exceptions write receipts;
SIGKILL/kernel OOM cannot guarantee them, so keep the enclosing cgroup receipt.
"""
import argparse
import importlib.util
import json
import math
import os
from pathlib import Path
import secrets
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import urllib.error
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
RUNS = ROOT / ".test-tools/soak-runs"
ZG = ROOT / ".test-tools/node_modules/@zvec/zvec-grep/dist/cli/index.js"
HOST = Path(os.environ.get("HERMES_AGENT_DIR", str(Path.home() / ".hermes/hermes-agent"))).resolve()
RUNTIME_FILE = "zg-runtime.json"


def runtime_command(manifest_path=None, allowed_root=None):
    """Return (argv prefix, metadata) for the engine, optionally from a prepared runtime.

    A manifest-selected runtime is an experiment artifact: it must live inside
    the harness-owned tree, be executable, and never be the production wrapper.
    Requested native pool sizes are NOT an observed process thread cap.
    """
    if manifest_path is None:
        return [str(ZG)], {"runtime": "raw_test_package"}
    root = Path(allowed_root) if allowed_root is not None else ROOT / ".test-tools"
    path = Path(manifest_path)
    if not path.is_file():
        raise ValueError("runtime manifest is missing")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except ValueError as exc:
        raise ValueError("runtime manifest is not valid JSON") from exc
    if not isinstance(data, dict):
        raise ValueError("runtime manifest must be an object")
    entrypoint = data.get("entrypoint")
    if not isinstance(entrypoint, str) or not entrypoint:
        raise ValueError("runtime manifest has no entrypoint")
    resolved = Path(entrypoint).resolve()
    if not resolved.is_relative_to(root.resolve()):
        raise ValueError("runtime entrypoint escapes the harness tree")
    if not resolved.is_file() or not os.access(resolved, os.X_OK):
        raise ValueError("runtime entrypoint is not an executable file")
    return [str(resolved)], {
        "runtime": "patched_thread_runtime",
        "requested_native_threads": data.get("requested_native_threads"),
        "observed_total_threads": data.get("observed_total_threads"),
        "source_sha256": data.get("source_sha256"),
        "patched_sha256": data.get("patched_sha256"),
    }


def write_runtime(run, command, metadata):
    """Record the selected engine for the worker and the run-local launcher."""
    write_json(Path(run) / RUNTIME_FILE, {"entrypoint": command[0], **metadata})


def selected_entrypoint(run, allowed_root=None):
    """Resolve the engine for a run: the recorded runtime, else the raw test package."""
    record = Path(run) / RUNTIME_FILE
    if not record.is_file():
        return ZG
    root = Path(allowed_root) if allowed_root is not None else ROOT / ".test-tools"
    data = json.loads(record.read_text(encoding="utf-8"))
    entrypoint = data.get("entrypoint")
    if not isinstance(entrypoint, str) or not entrypoint:
        raise ValueError("recorded runtime has no entrypoint")
    resolved = Path(entrypoint).resolve()
    if not resolved.is_relative_to(root.resolve()):
        raise ValueError("recorded runtime escapes the harness tree")
    if not resolved.is_file():
        raise ValueError("recorded runtime is missing")
    return resolved


def environment(run, port):
    if not 1024 <= port <= 65535 or port == 17999:
        raise ValueError("unsafe test port")
    home = run / "home"
    return {"PATH": "/usr/bin:/bin", "HOME": str(home),
            "HERMES_HOME": str(home / "hermes"), "HERMES_AGENT_DIR": str(HOST),
            "XDG_CONFIG_HOME": str(home / "config"), "XDG_CACHE_HOME": str(home / "cache"),
            "XDG_DATA_HOME": str(home / "data"), "LANG": "C.UTF-8", "TZ": "UTC",
            "ZVEC_GREP_HOME": str(run / "zg-state"),
            "ZVEC_GREP_MODEL_CACHE": str(ROOT / ".test-tools/models"),
            "ZVEC_GREP_MODE": "server", "ZVEC_GREP_DEVICE": "cpu",
            "ZVEC_GREP_SERVER_URL": f"http://127.0.0.1:{port}/mcp",
            "ZVEC_GREP_SERVER_TOKEN_FILE": str(run / "token"),
            "PYTHONDONTWRITEBYTECODE": "1"}


def server_args(args):
    # Provider's query explicitly requests auto, which overrides the env and
    # falls back to direct on daemon failure. The run-local transport launcher
    # removes ONLY that mode option and pins server; product code is unmodified.
    if not args or args[0] not in ("query", "index", "status"):
        return list(args)
    result, skip = [], False
    for arg in args:
        if skip:
            skip = False
        elif arg == "--mode":
            skip = True
        elif not arg.startswith("--mode="):
            result.append(arg)
    return result + ["--mode", "server"]


def proc_rows():
    """Linux /proc only. Disappearing processes are normal sampling races."""
    rows = {}
    for path in Path("/proc").iterdir():
        if not path.name.isdigit():
            continue
        try:
            fields = (path / "stat").read_text().rsplit(")", 1)[1].split()
            rows[int(path.name)] = {
                "pid": int(path.name), "ppid": int(fields[1]), "pgrp": int(fields[2]),
                "state": fields[0], "start_ticks": int(fields[19]),
                "cpu_seconds": (int(fields[11]) + int(fields[12])) / os.sysconf("SC_CLK_TCK"),
                "rss_bytes": int(fields[21]) * os.sysconf("SC_PAGE_SIZE"),
                "threads": int(fields[17]),
            }
        except (OSError, ValueError, IndexError):
            continue
    return rows


class ProcessTree:
    """Own exact start-time identities and descendants, never names/ambient PIDs.

    Retain discovered identities across reparenting. Signal with Linux pidfds,
    rechecking start-time AFTER opening the pidfd to close the PID-reuse race.
    """
    def __init__(self, pid):
        rows = proc_rows()
        if pid not in rows:
            raise RuntimeError("owned child disappeared before tracking")
        self.known = {pid: rows[pid]["start_ticks"]}
        self.lock = threading.RLock()
        self.cpu_seen = {}

    def identities(self):
        with self.lock:
            rows = proc_rows()
            current = {pid: rows[pid] for pid, start in self.known.items()
                       if pid in rows and rows[pid]["start_ticks"] == start}
            changed = True
            while changed:
                changed = False
                for pid, row in rows.items():
                    if pid not in current and row["ppid"] in current:
                        current[pid] = row
                        self.known[pid] = row["start_ticks"]
                        changed = True
            return current

    def sample(self, phase, generation):
        rows = self.identities()
        fds = 0
        for pid, row in rows.items():
            self.cpu_seen[(pid, row["start_ticks"])] = row["cpu_seconds"]
            try:
                row["fds"] = len(list(Path(f"/proc/{pid}/fd").iterdir()))
                fds += row["fds"]
            except OSError:
                row["fds"] = None
        return {"monotonic_seconds": time.monotonic(), "unix_seconds": time.time(),
                "phase": phase, "generation": generation,
                "rss_semantics": "concurrent_tree_sum_shared_pages_overcounted",
                "rss_sum_bytes": sum(r["rss_bytes"] for r in rows.values()),
                "cpu_seconds": sum(self.cpu_seen.values()),
                "cpu_semantics": "observed_lifetime_sum_short_lived_children_may_be_missed",
                "threads": sum(r["threads"] for r in rows.values()), "fds": fds,
                "process_count": len(rows), "processes": list(rows.values())}

    def stop(self, grace=2):
        def live():
            return {p: r for p, r in self.identities().items() if r.get("state") != "Z"}
        def send(sig):
            for pid, row in reversed(list(live().items())):
                try:
                    fd = os.pidfd_open(pid)
                    try:
                        now = proc_rows().get(pid)
                        if now and now["start_ticks"] == row["start_ticks"]:
                            signal.pidfd_send_signal(fd, sig)
                    finally:
                        os.close(fd)
                except ProcessLookupError:
                    pass
        send(signal.SIGTERM)
        deadline = time.monotonic() + grace
        while live() and time.monotonic() < deadline:
            time.sleep(.02)
        send(signal.SIGKILL)
        deadline = time.monotonic() + 1
        while live() and time.monotonic() < deadline:
            time.sleep(.02)
        return list(live())


def trend(samples):
    """Caller groups equal corpus/warmth/daemon generation; no leak verdict."""
    n = len(samples)
    slope = None
    if n > 1:
        xs = [r["monotonic_seconds"] for r in samples]
        ys = [r["rss_sum_bytes"] for r in samples]
        mx, my = sum(xs)/n, sum(ys)/n
        denominator = sum((x-mx)**2 for x in xs)
        if denominator:
            slope = sum((x-mx)*(y-my) for x, y in zip(xs, ys))/denominator
    return {"samples": n, "rss_bytes_per_second": slope,
            "classification": "diagnostic_not_leak_proof"}


def free_port():
    # Binding to zero asks the OS. Reservation cannot span zg's bind; a racing
    # listener is a startup failure, never a reason to adopt an ambient daemon.
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    if port == 17999:
        return free_port()
    return port


def ready_identity(record, pid, url):
    return (record.get("pid") == pid and record.get("ready") is True
            and bool(record.get("instanceToken")) and record.get("serverUrl") == url)


def http_status(url, token=None):
    request = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"} if token else {})
    # Explicitly disable ambient proxies, even in tests.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(request, timeout=1) as response:
            return response.status
    except urllib.error.HTTPError as exc:
        return exc.code


class Daemon:
    def __init__(self, run, env):
        self.run, self.env = run, env
        self.process = self.tree = self.log = None
        self.generation = 0
        self.instance_token = None

    def start(self, deadline):
        self.generation += 1
        url = self.env["ZVEC_GREP_SERVER_URL"]
        listen = url.removeprefix("http://").removesuffix("/mcp")
        self.log = (self.run / f"daemon-{self.generation}.private.log").open("w")
        self.process = subprocess.Popen([str(selected_entrypoint(self.run)), "server", "run",
            "--listen", listen, "--token-file", self.env["ZVEC_GREP_SERVER_TOKEN_FILE"]],
            cwd=self.run, env=self.env, stdout=self.log, stderr=subprocess.STDOUT,
            start_new_session=True)
        self.tree = ProcessTree(self.process.pid)
        token = Path(self.env["ZVEC_GREP_SERVER_TOKEN_FILE"]).read_text().strip()
        lock = Path(self.env["ZVEC_GREP_HOME"]) / "daemon/instance.lock"
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                raise RuntimeError("owned_daemon_exited_during_startup")
            try:
                record = json.loads(lock.read_text())
                if ready_identity(record, self.process.pid, url):
                    if http_status(url.replace("/mcp", "/healthz")) == 200:
                        unauth, auth = http_status(url), http_status(url, token)
                        if unauth != 401 or auth not in (200, 400, 405, 406):
                            raise RuntimeError("daemon_authentication_gate_failed")
                        if record["instanceToken"] == self.instance_token:
                            raise RuntimeError("daemon_instance_identity_reused")
                        self.instance_token = record["instanceToken"]
                        return {"pid": self.process.pid, "generation": self.generation,
                                "unauthenticated_status": unauth, "authenticated_status": auth,
                                "ready_monotonic_seconds": time.monotonic()}
            except (OSError, ValueError, urllib.error.URLError):
                pass
            time.sleep(.05)
        raise TimeoutError("owned_daemon_readiness_timeout")

    def stop(self):
        remaining = self.tree.stop() if self.tree else []
        if self.process:
            self.process.wait(timeout=2)
        if self.log:
            self.log.close()
        return remaining

    def restart(self, deadline):
        if self.stop():
            raise RuntimeError("owned_daemon_cleanup_failed")
        return self.start(deadline)


def write_json(path, value):
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2) + "\n")
    temporary.replace(path)


def retrieval_hit(text, content):
    body = text.partition("\n#1 ")[2]
    return bool(body) and "facts/" in body.splitlines()[0] and content in body


def provider_factory(run, number):
    sys.path.insert(0, str(HOST))
    name = "soak_provider"
    if name not in sys.modules:
        spec = importlib.util.spec_from_file_location(name, ROOT / "zvec-memory/__init__.py")
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
    p = sys.modules[name].ZvecMemoryProvider(config={
        "vault": str(run / "vault"), "zg_bin": str(run / "zg-server-only"),
        "embedding": "local/potion-retrieval-32m", "context_chars": 2000,
        "preview": "full", "reindex_min_seconds": 0})
    try:
        p.initialize(f"soak-handle-{number}", hermes_home=str(run / "home/hermes"))
    except BaseException:
        p.shutdown()
        raise
    return p


def validate_sources(vault, wanted):
    actual = [line for path in (vault / "facts").glob("*.md")
              for line in path.read_text().splitlines() if line.startswith("soakslot")]
    if sorted(actual) != sorted(wanted):
        raise RuntimeError("owned_source_oracle_mismatch")


def workload(run, args, report, daemon, factory=provider_factory):
    vault = run / "vault"
    (vault / "facts").mkdir(parents=True, exist_ok=True)
    (vault / "sessions").mkdir(exist_ok=True)
    for n in range(args.seed_facts):
        (vault / "facts" / f"background-{n:06d}.md").write_text(
            f"# Synthetic archive {n}\nsoakbackground{n:06d} stores synthetic shade amber.\n")
    handles = []
    phase = "cold_start"
    active_start = None
    report.update(cycles=0, corpus_removed=False, prefetch=[])

    def heartbeat():
        nonlocal phase
        if daemon.process.poll() is not None:
            raise RuntimeError("owned_daemon_unexpected_exit")
        if (active_start is not None and args.engine_restart_at is not None
                and not report["restarts"]
                and time.monotonic()-active_start >= args.engine_restart_at):
            started = time.monotonic()
            event = daemon.restart(started + 30)
            event["restart_seconds"] = time.monotonic()-started
            report["restarts"].append(event)
        write_json(run / "phase.json", {"phase": phase, "generation": daemon.generation})

    def converge(wanted):
        start = time.monotonic()
        deadline = start + args.convergence_timeout
        while time.monotonic() < deadline:
            heartbeat()
            for p in handles:
                p.queue_prefetch("soakcontrolanchor")
            p = handles[-1]
            state = p._mirror_state()
            rows = list(state.get("records", {}).values())
            valid = (sorted(r["content"] for r in rows) == sorted(wanted)
                and len({r["path"] for r in rows}) == len(rows)
                and not any(state.get(k) for k in ("pending_creates", "pending_deletes", "refresh_required"))
                and all(h._recall_token() is not None and h._index_ready()
                        and h._mirror_inbox is not None and not h._mirror_inbox.pending()
                        and not h._index_running for h in handles))
            if valid:
                if (vault / ".mirror-delivery-failed.json").exists():
                    raise RuntimeError("delivery_failure_marker")
                for row in rows:
                    if row["content"] not in (vault / row["path"]).read_text():
                        raise RuntimeError("owned_source_mismatch")
                source = "\n".join(p.read_text() for p in (vault / "facts").glob("*.md"))
                if "obsoleteanswer" in source:
                    raise RuntimeError("obsolete_source_remains")
                validate_sources(vault, wanted)
                report["convergence"].append({"phase": phase, "seconds": time.monotonic()-start})
                return
            time.sleep(.05)
        raise TimeoutError("automatic_convergence_timeout")

    def search(p, query, content, required=True, absent=False, kind="fts"):
        heartbeat()
        start = time.monotonic()
        result = json.loads(p.handle_tool_call("memory_search", {
            "query": query, "mode": kind, "limit": 5, "globs": ["facts/**"]}))
        body = result.get("results", "")
        hit = retrieval_hit(body, content)
        report["queries"].append({"phase": phase, "generation": daemon.generation,
            "kind": kind, "seconds": time.monotonic()-start, "hit": hit,
            "required": required, "absent": absent, "chars": len(body)})
        if result.get("error") or len(body) > 2000 or (required and not hit) or (absent and hit):
            (run / "retrieval-error.private.log").write_text(json.dumps(result))
            raise RuntimeError("native_retrieval_gate_failed")

    def notify(p, action, text, previous=""):
        start = time.monotonic()
        p.on_memory_write(action, "user", text, {"old_text": previous} if previous else {})
        report["callbacks"].append({"action": action, "phase": phase,
            "seconds": time.monotonic()-start})

    try:
        for number in range(2):
            handles.append(factory(run, number))
        report["same_handle_ids"] = [id(p) for p in handles]
        control = "soakcontrolanchor confirms synthetic durable control is violet."
        stored = json.loads(handles[0].handle_tool_call("memory_store", {"content": control}))
        if stored.get("status") != "stored":
            raise RuntimeError("control_store_failed")
        converge([])
        search(handles[1], "soakcontrolanchor", control, kind="hybrid")
        phase = "warm"
        active_start = time.monotonic()
        report["active_started_monotonic_seconds"] = active_start
        # Always finish at least one bounded cycle, then remove the seeded corpus
        # and keep these SAME handles and daemon warm for the remaining phase.
        while time.monotonic()-active_start < args.duration or not report["cycles"]:
            heartbeat()
            if not report["corpus_removed"] and time.monotonic()-active_start >= args.duration * .8:
                phase = "corpus_removal"
                for path in (vault / "facts").glob("background-*.md"):
                    path.unlink()
                report["corpus_removed"] = True
            revised = [f"soakslot{n:02d} revisedanswer cycle{report['cycles']:08d} is indigo." for n in range(2)]
            old = [t.replace("revisedanswer", "obsoleteanswer") for t in revised]
            for n, p in enumerate(handles):
                notify(p, "add", old[n])
                notify(p, "replace", revised[n], old[n])
            converge(revised)
            for n in range(2):
                search(handles[1-n], f"soakslot{n:02d}", revised[n])
                search(handles[1-n], f"soakslot{n:02d}", "obsoleteanswer", required=False, absent=True)
            for n, p in enumerate(handles):
                notify(p, "remove", "", revised[n])
            converge([])
            for n in range(2):
                search(handles[1-n], f"soakslot{n:02d}", "revisedanswer", required=False, absent=True)
            search(handles[1], "soakcontrolanchor", control, kind="hybrid")
            # Cache-warmed identical query vs a genuinely distinct next-turn
            # prompt. Empty 2s prefetch is diagnostic, never false retrieval PASS.
            for kind, query in (("repeated_query", "soakcontrolanchor"),
                                ("distinct_query", f"soakcontrolanchor cycle {report['cycles']}")):
                started = time.monotonic()
                text = handles[1].prefetch(query)
                report["prefetch"].append({"kind": kind, "phase": phase,
                    "generation": daemon.generation, "seconds": time.monotonic()-started,
                    "chars": len(text), "hit": retrieval_hit(text, control)})
                if len(text) > 2000:
                    raise RuntimeError("prefetch_context_cap_exceeded")
            report["cycles"] += 1
            if report["corpus_removed"]:
                phase = "warm_after_removal"
            time.sleep(min(.05, args.duration/10))
        # Short mode may spend the whole duration in one native cycle. Still
        # perform the corpus-removal gate, but do not pretend it is a long trend.
        if not report["corpus_removed"]:
            phase = "corpus_removal"
            for path in (vault / "facts").glob("background-*.md"):
                path.unlink()
            notify(handles[0], "add", "soakcleanupanchor synthetic cleanup marker.")
            notify(handles[0], "remove", "", "soakcleanupanchor synthetic cleanup marker.")
            converge([])
            report["corpus_removed"] = True
        phase = "warm_after_removal"
        heartbeat()
        converge([])
        search(handles[1], "soakcontrolanchor", control, kind="hybrid")
        if args.seed_facts:
            search(handles[1], "soakbackground000000", "soakbackground000000", required=False, absent=True)
        if args.engine_restart_at is not None and len(report["restarts"]) != 1:
            raise RuntimeError("requested_restart_not_exercised")
        report["final_mirror_records"] = len(handles[1]._mirror_state()["records"])
        report["active_seconds"] = time.monotonic()-active_start
    finally:
        for p in handles:
            p.shutdown()
            if any(w is not None and w._thread.is_alive() for w in (p._disk_worker, p._index_worker)):
                report["errors"].append("provider_worker_alive_after_shutdown")


def worker(run, args, env):
    report = {"schema_version": 1, "lane": "long_lived_soak", "transport": "native_server_only",
        "errors": [], "queries": [], "callbacks": [], "convergence": [], "restarts": []}
    daemon = None
    began = time.monotonic()
    try:
        daemon = Daemon(run, env)
        report["daemon_initial"] = daemon.start(began + 30)
        workload(run, args, report, daemon)
    except BaseException as exc:
        report["errors"].append("worker_exception:" + type(exc).__name__)
        (run / "worker-error.private.log").write_text(traceback.format_exc())
    finally:
        try:
            if daemon is not None and daemon.stop():
                report["errors"].append("owned_daemon_leftovers")
        except BaseException as exc:
            report["errors"].append("daemon_cleanup_exception:" + type(exc).__name__)
        report["elapsed_seconds"] = time.monotonic()-began
        report["passed"] = not report["errors"]
        write_json(run / "worker.json", report)
    return int(not report["passed"])


def phase_at(run):
    try:
        value = json.loads((run / "phase.json").read_text())
        if value["phase"] in ("cold_start", "warm", "corpus_removal", "warm_after_removal"):
            return value["phase"], int(value["generation"])
    except (OSError, ValueError, KeyError):
        pass
    return "cold_start", 1


def transport_main(run):
    """Run-local launcher, real raw zg only; argv/output never in public metrics."""
    started = time.monotonic()
    args = server_args(sys.argv[1:])
    code = 127
    try:
        code = subprocess.call([str(selected_entrypoint(run)), *args])
        return code
    finally:
        phase, generation = phase_at(run)
        row = {"kind": args[0] if args and args[0] in ("index", "query", "status") else "probe",
               "phase": phase, "generation": generation, "exit": code,
               "monotonic_seconds": started, "seconds": time.monotonic()-started}
        fd = os.open(run / "native-commands.jsonl", os.O_CREAT | os.O_APPEND | os.O_WRONLY, 0o600)
        try:
            os.write(fd, (json.dumps(row) + "\n").encode())
        finally:
            os.close(fd)


def latency_summary(rows):
    groups = {}
    for row in rows:
        groups.setdefault(row["kind"] + ":" + row["phase"], []).append(row["seconds"])
    result = {}
    for key, values in groups.items():
        values.sort()
        result[key] = {"count": len(values), "over_prefetch_2s_budget": sum(v > 2 for v in values),
            **{f"p{q}_seconds": values[max(0, math.ceil(len(values)*q/100)-1)] for q in (50, 95, 99)},
            "max_seconds": max(values)}
    return result


def supervise(run, command, env, budget, interval):
    report = {"schema_version": 1, "lane": "long_lived_soak", "transport": "native_server_only",
              "errors": [], "leftover_owned_pids": []}
    samples, child, tree = [], None, None
    began = time.monotonic()
    try:
        with (run / "worker.private.log").open("w") as log, (run / "resources.jsonl").open("w") as metrics:
            child = subprocess.Popen(command, cwd=ROOT, env=env, stdout=log,
                                     stderr=subprocess.STDOUT, start_new_session=True)
            tree = ProcessTree(child.pid)
            next_sample = began
            while child.poll() is None:
                now = time.monotonic()
                if now >= began + budget:
                    report["errors"].append("whole_command_timeout")
                    break
                if now >= next_sample:
                    phase, generation = phase_at(run)
                    row = tree.sample(phase, generation)
                    samples.append(row)
                    metrics.write(json.dumps(row) + "\n")
                    metrics.flush()
                    next_sample = now + interval
                else:
                    tree.identities()  # retain descendants before reparenting
                time.sleep(min(.1, interval))
    except BaseException as exc:
        report["errors"].append("supervisor_exception:" + type(exc).__name__)
        (run / "supervisor-error.private.log").write_text(traceback.format_exc())
    finally:
        if tree:
            try:
                # A successful worker must have shut down every owned child.
                live = [r for r in tree.identities().values() if r.get("state") != "Z"]
                if child.poll() is not None and live:
                    report["errors"].append("worker_left_owned_descendants")
                report["leftover_owned_pids"] = tree.stop(grace=2)
            except BaseException as exc:
                report["errors"].append("tree_cleanup_exception:" + type(exc).__name__)
        if child:
            try:
                report["worker_exit"] = child.wait(timeout=2)
                if child.returncode:
                    report["errors"].append("worker_nonzero_exit")
            except subprocess.TimeoutExpired:
                report["errors"].append("worker_reap_timeout")
        try:
            report["worker"] = json.loads((run / "worker.json").read_text())
            if not report["worker"].get("passed"):
                report["errors"].append("worker_failed")
        except (OSError, ValueError):
            report["errors"].append("missing_or_invalid_worker_receipt")
        groups = {}
        for row in samples:
            groups.setdefault(f"{row['phase']}:generation{row['generation']}", []).append(row)
        report["resources"] = {"sample_count": len(samples), "samples_file": "resources.jsonl",
            "rss_semantics": "concurrent_tree_sum_shared_pages_overcounted",
            "peak_sampled_rss_sum_bytes": max((r["rss_sum_bytes"] for r in samples), default=0),
            "trends": {k: trend(v) for k, v in groups.items()},
            "notes": "Cold startup/model downloads, corpus size and daemon generations are not pooled. No hard leak threshold."}
        native = []
        try:
            for line in (run / "native-commands.jsonl").read_text().splitlines():
                native.append(json.loads(line))
        except FileNotFoundError:
            pass
        except ValueError:
            report["errors"].append("invalid_native_metrics")
        report["native_latency"] = latency_summary(native)
        report["native_nonzero_commands"] = sum(r["exit"] != 0 for r in native)
        report["prefetch_budget_seconds"] = 2
        report["prefetch_budget_interpretation"] = "diagnostic future/index-latency comparison, not a forced indexing deadline"
        report["elapsed_seconds"] = time.monotonic()-began
        report["passed"] = not report["errors"] and not report["leftover_owned_pids"]
        write_json(run / "report.json", report)
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--duration", type=float, default=60, help="active seconds, 1..1800 (default 60)")
    parser.add_argument("--seed-facts", type=int, default=200, help="fixed synthetic corpus, 0..1000")
    parser.add_argument("--sample-interval", type=float, default=10, help="/proc sample seconds, .1..30")
    parser.add_argument("--convergence-timeout", type=float, default=120, help="automatic recovery seconds, 1..120")
    parser.add_argument("--engine-restart-at", type=float, help="restart owned daemon after this many active seconds")
    parser.add_argument("--runtime-manifest", type=Path,
                        help="prepared private runtime manifest; default raw test package")
    parser.add_argument("--worker-run", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if not (1 <= args.duration <= 1800 and 0 <= args.seed_facts <= 1000
            and .1 <= args.sample_interval <= 30 and 1 <= args.convergence_timeout <= 120
            and (args.engine_restart_at is None or 0 <= args.engine_restart_at < args.duration)):
        parser.error("workload outside explicit safety bounds")
    try:
        runtime, runtime_metadata = runtime_command(args.runtime_manifest)
    except ValueError as exc:
        parser.error(str(exc))
    if args.worker_run:
        run = args.worker_run.resolve()
        if (run.parent != RUNS.resolve() or not (run / "OWNER.json").is_file()
                or os.environ.get("HERMES_HOME") != str(run / "home/hermes")
                or os.environ.get("HOME") != str(run / "home")):
            parser.error("refusing non-owned worker environment")
        # Environment was constructed by the coordinator, never ambient copy.
        port = int(os.environ["ZVEC_GREP_SERVER_URL"].split(":")[2].split("/")[0])
        return worker(run, args, environment(run, port))
    RUNS.mkdir(parents=True, exist_ok=True)
    run = Path(tempfile.mkdtemp(prefix="soak-", dir=RUNS)).resolve()
    run.chmod(0o700)
    report = {"schema_version": 1, "lane": "long_lived_soak", "transport": "native_server_only",
              "passed": False, "errors": [], "runtime": runtime_metadata}
    try:
        write_json(run / "OWNER.json", {"kind": "zvec-long-lived-soak", "version": 1})
        if not Path(runtime[0]).is_file() or not (HOST / "agent/memory_provider.py").is_file():
            raise FileNotFoundError("test engine or host checkout missing")
        if not hasattr(os, "pidfd_open") or not hasattr(signal, "pidfd_send_signal"):
            raise RuntimeError("Linux pidfd support required")
        env = environment(run, free_port())
        for key in ("HOME", "HERMES_HOME", "XDG_CONFIG_HOME", "XDG_CACHE_HOME", "XDG_DATA_HOME", "ZVEC_GREP_HOME"):
            Path(env[key]).mkdir(parents=True, exist_ok=True)
        with (run / "token").open("x") as token:
            token.write(secrets.token_hex(32) + "\n")
        (run / "token").chmod(0o600)
        write_runtime(run, runtime, runtime_metadata)
        launcher = run / "zg-server-only"
        launcher.write_text(f"#!{sys.executable}\nimport runpy\nfrom pathlib import Path\nm = runpy.run_path({str(Path(__file__).resolve())!r})\nraise SystemExit(m['transport_main'](Path({str(run)!r})))\n")
        launcher.chmod(0o700)
        command = [sys.executable, str(Path(__file__).resolve()), "--worker-run", str(run),
                   "--duration", str(args.duration), "--seed-facts", str(args.seed_facts),
                   "--sample-interval", str(args.sample_interval),
                   "--convergence-timeout", str(args.convergence_timeout)]
        if args.engine_restart_at is not None:
            command += ["--engine-restart-at", str(args.engine_restart_at)]
        report = supervise(run, command, env, args.duration + 240, args.sample_interval)
        report["runtime"] = runtime_metadata
        # argparse Path values are not JSON; the receipt stays serializable.
        report["arguments"] = {k: (str(v) if isinstance(v, Path) else v)
                               for k, v in vars(args).items() if k != "worker_run"}
        report["python"] = sys.version.split()[0]
        for name, path in (("plugin_sha", ROOT), ("host_sha", HOST)):
            report[name] = subprocess.check_output(["git", "-C", str(path), "rev-parse", "HEAD"], text=True, timeout=5).strip()
        report["zg_version"] = json.loads((Path(runtime[0]).parents[2] / "package.json").read_text())["version"]
    except BaseException as exc:
        report["passed"] = False
        report["errors"].append("preflight_exception:" + type(exc).__name__)
        (run / "coordinator-error.private.log").write_text(traceback.format_exc())
    finally:
        write_json(run / "report.json", report)
    print(json.dumps({"passed": report["passed"], "report": str(run / "report.json")}))
    return int(not report["passed"])


def termination_handler(signum, frame):
    raise InterruptedError("termination_requested")


if __name__ == "__main__":
    signal.signal(signal.SIGTERM, termination_handler)
    raise SystemExit(main())
