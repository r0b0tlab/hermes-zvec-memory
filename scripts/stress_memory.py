#!/usr/bin/env python3
"""Finite, opt-in synthetic memory stress; never uses production state.

Run under a dedicated externally managed cgroup (KillMode=control-group) with
MemoryMax/CPUQuota/TasksMax and RuntimeMaxSec. This runner pins observed process
identities with pidfds; the cgroup must clean up unobserved/escaped descendants.
Artifacts and failed receipts are retained; no automatic retries or deletion.
"""
import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import resource
import signal
import subprocess
import sys
import tempfile
import time
import traceback

ROOT = Path(__file__).resolve().parents[1]
RUNS = ROOT / ".test-tools/stress-runs"
ZG = ROOT / ".test-tools/node_modules/@zvec/zvec-grep/dist/cli/index.js"


HOST = Path(os.environ.get("HERMES_AGENT_DIR", str(Path.home() / ".hermes/hermes-agent"))).resolve()


def environment(run, repo):
    home = run / "home"
    return {"PATH": "/usr/bin:/bin", "HOME": str(home),
            "HERMES_HOME": str(home / "hermes"), "HERMES_AGENT_DIR": str(HOST),
            "XDG_CONFIG_HOME": str(home / "config"), "XDG_DATA_HOME": str(home / "data"),
            "XDG_CACHE_HOME": str(home / "cache"), "ZVEC_GREP_HOME": str(run / "zg-state"),
            "ZVEC_GREP_MODEL_CACHE": str(repo / ".test-tools/models"),
            "ZVEC_GREP_MODE": "direct", "HF_HUB_OFFLINE": "1",
            "TRANSFORMERS_OFFLINE": "1", "PYTHONDONTWRITEBYTECODE": "1",
            "LANG": "C.UTF-8", "TZ": "UTC",
            "NODE_OPTIONS": f'--require="{run / "deny-network.cjs"}"'}


def fact(run_id, worker, number, revised):
    token = hashlib.sha256(f"{run_id}:{worker}:{number}".encode()).hexdigest()[:20]
    adjective = "verified" if revised else "obsolete"
    return f"For key stress{token}, the {adjective} answer is payload{token}."


def expected(run_id, workers, records):
    return [fact(run_id, w, n, True) for w in range(workers)
            for n in range(records) if n % 2]


def validate_state(state, wanted):
    rows = list(state.get("records", {}).values())
    errors = []
    if sorted(row["content"] for row in rows) != sorted(wanted):
        errors.append("mirror contents differ from exact oracle")
    if len({row["path"] for row in rows}) != len(rows):
        errors.append("duplicate mirror paths")
    if any(state.get(k) for k in ("pending_creates", "pending_deletes", "refresh_required")):
        errors.append("journal recovery incomplete")
    return errors


def owned_file(vault, path):
    result = (vault / path).resolve()
    if not result.is_relative_to(vault.resolve()) or result.is_symlink():
        raise ValueError("source path escapes owned vault")
    return result


def validate_sources(vault, state, forbidden, controls):
    errors = []
    try:
        texts = {p: owned_file(vault, p).read_text() for p in (vault / "facts").rglob("*.md")}
        for row in state["records"].values():
            path = owned_file(vault, row["path"])
            if row["content"] not in path.read_text():
                errors.append("owned source content mismatch")
            if sum(text.count(row["content"]) for text in texts.values()) != 1:
                errors.append("duplicate or missing mirror source content")
        if any(content in text for content in forbidden for text in texts.values()):
            errors.append("removed or obsolete source remains")
        for control in controls:
            if not control or control["content"] not in owned_file(vault, control["path"]).read_text():
                errors.append("explicit control fact lost")
    except Exception as exc:
        errors.append(f"source validation: {exc!r}")
    return errors


def hit_body(text):
    return text.partition("\n#1 ")[2]


def prepare_home(run):
    (run / "home/hermes").mkdir(parents=True, exist_ok=True)
    # Raw direct zg only. Fail closed on JS network access, including cache misses.
    (run / "deny-network.cjs").write_text(
        "const deny = () => { throw new Error('stress: network disabled'); };\n"
        "globalThis.fetch = deny;\n"
        "require('node:net').Socket.prototype.connect = deny;\n"
        "require('node:http').request = deny; require('node:https').request = deny;\n")


def write_json(path, value):
    path.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")


def latency(values):
    """Lower order-statistic quantiles; null for empty samples, seconds units."""
    values = sorted(values)
    return {"count": len(values), **{name: values[int((len(values)-1)*q)] if values else None
            for name, q in (("p50", .5), ("p95", .95), ("p99", .99), ("max", 1))}}


def inventory(run):
    files = [p for p in run.rglob("*") if p.is_file() and not p.is_symlink()]
    return {"scope": "snapshot before final report write; excludes symlinks",
            "file_count": len(files), "bytes": sum(p.stat().st_size for p in files),
            "internal_files": {str(p.relative_to(run)): p.stat().st_size for p in files
                               if p.name.startswith(".mirror") or ".zvec-grep" in p.parts}}


def provider(run, mode, session):
    sys.path.insert(0, str(HOST))
    spec = importlib.util.spec_from_file_location("stress_provider", ROOT / "zvec-memory/__init__.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    cls = module.ZvecMemoryProvider
    if mode == "offline":
        cls._run_zg = lambda *a, **k: (0, "", "")
    obj = cls(config={"vault": str(run / "vault"), "zg_bin": str(ZG),
                      "embedding": "local/potion-retrieval-32m", "context_chars": 2000,
                      "reindex_min_seconds": 0})
    return initialize_provider(obj, run, session)


def initialize_provider(obj, run, session):
    try:
        obj.initialize(session, hermes_home=str(run / "home/hermes"))
    except Exception:
        try:
            obj.shutdown()
        except Exception:
            traceback.print_exc()
        raise
    return obj


def close_provider(obj, result):
    if obj is not None:
        try:
            obj.shutdown()
            result["live_threads_after_shutdown"] = [w._thread.name for w in
                (obj._disk_worker, obj._index_worker) if w and w._thread.is_alive()]
            if result["live_threads_after_shutdown"]:
                result["errors"].append("provider threads alive after shutdown")
        except Exception as exc:
            result["errors"].append(f"shutdown: {exc!r}")


def validate_acceptance(receipt, number, records):
    planned = [(phase, n) for phase in ("add", "replace", "remove") for n in range(records)
               if phase != "remove" or not n % 2]
    actual = [(s["phase"], s["record"]) for s in receipt["samples"]]
    return [] if receipt["worker"] == number and actual == planned else ["accepted operations differ from exact workload"]


def pipeline_pending(obj):
    """Non-atomic diagnostic snapshot; no manual refresh or native vault lock."""
    state = obj._mirror_state()
    pending = {"inbox_pending": obj._mirror_inbox is None or obj._mirror_inbox.pending(),
               "pending_creates": len(state["pending_creates"]),
               "pending_deletes": len(state["pending_deletes"]),
               "refresh_required": bool(state["refresh_required"]),
               "index_requested": obj._index_requested, "index_running": obj._index_running,
               "recall_blocked": obj._recall_token() is None,
               "index_not_ready": not obj._index_ready()}
    for name in ("disk", "index"):
        worker_obj = getattr(obj, f"_{name}_worker")
        if worker_obj is None:
            pending[f"{name}_unfinished_tasks"] = 0
        else:
            with worker_obj._queue.mutex:
                pending[f"{name}_unfinished_tasks"] = worker_obj._queue.unfinished_tasks
    return pending


def drain_provider(obj, deadline):
    """Exercise demand-driven automatic convergence, not a manual refresh."""
    while time.monotonic() < deadline:
        # Busy inbox discovery is nonwaiting and already proves work remains.
        # Avoid repeatedly validating every journal path while writers drain;
        # this reduces harness observer overhead, not provider work. An absent
        # inbox also needs automatic discovery via queue_prefetch below.
        inbox = obj._mirror_inbox
        if inbox is not None and not inbox.pending() and not any(pipeline_pending(obj).values()):
            return
        obj.queue_prefetch("Inspect synthetic stress drain status")
        time.sleep(min(.1, max(0, deadline - time.monotonic())))
    raise RuntimeError("automatic convergence deadline exceeded before shutdown")


def worker(run, number, records, mode, shutdown_policy="immediate", timeout=180):
    result = {"worker": number, "samples": [], "errors": [],
              "shutdown_policy": shutdown_policy, "admission_seconds": None,
              "drain_seconds": 0.0, "pending_at_shutdown": None,
              "planned_callbacks": 2 * records + (records + 1)//2}
    obj = None
    admission_start = None
    start = time.monotonic()
    try:
        obj = provider(run, mode, f"stress-worker-{number}")
        admission_start = time.monotonic()
        for phase in ("add", "replace", "remove"):
            for n in range(records):
                if phase == "remove" and n % 2:
                    continue
                previous = fact(run.name, number, n, phase == "remove")
                content = "" if phase == "remove" else fact(run.name, number, n, phase == "replace")
                t = time.perf_counter()
                obj.on_memory_write(phase, "user", content, {} if phase == "add" else {"old_text": previous})
                sample = {"phase": phase, "record": n, "seconds": time.perf_counter()-t, "accepted": True}
                result["samples"].append(sample)
                with (run / f"worker-{number}-callbacks.jsonl").open("a") as progress:
                    progress.write(json.dumps(sample) + "\n")
                    progress.flush()
        result["admission_seconds"] = time.monotonic() - admission_start
        content = f"Explicit control fact for worker {number} in {run.name}."
        out = json.loads(obj.handle_tool_call("memory_store", {"content": content}))
        if out.get("status") != "stored":
            raise RuntimeError(str(out))
        result["control"] = {"path": out["path"], "content": content}
        if shutdown_policy == "drain":
            drain_start = time.monotonic()
            try:
                # The existing writer phase cap includes initialization/admission
                # and reserves the provider's unchanged five-second shutdown grace.
                drain_provider(obj, start + max(0, timeout - 5))
            finally:
                result["drain_seconds"] = time.monotonic() - drain_start
    except Exception as exc:
        result["errors"].append(repr(exc))
        result["traceback"] = traceback.format_exc()
        print(result["traceback"], file=sys.stderr)
    finally:
        if admission_start is not None and result["admission_seconds"] is None:
            result["admission_seconds"] = time.monotonic() - admission_start
        if obj is not None:
            try:
                result["pending_at_shutdown"] = pipeline_pending(obj)
                if shutdown_policy == "drain" and any(result["pending_at_shutdown"].values()):
                    result["errors"].append("pipeline pending at drain-policy shutdown")
            except Exception as exc:
                # Diagnostics must never prevent the original immediate shutdown.
                result["pending_at_shutdown_error"] = repr(exc)
                if shutdown_policy == "drain":
                    result["errors"].append(f"shutdown snapshot: {exc!r}")
        close_provider(obj, result)
        result["elapsed_seconds"] = time.monotonic()-start
        result["successful_callbacks"] = len(result["samples"])
        seconds = result["admission_seconds"]
        result["callbacks_per_second_admission"] = len(result["samples"])/seconds if seconds else None
        result["acceptance_count_complete"] = True
        result["latency_by_phase"] = {p: latency([s["seconds"] for s in result["samples"] if s["phase"] == p])
                                       for p in ("add", "replace", "remove")}
        write_json(run / f"worker-{number}.json", result)
    return int(bool(result["errors"]))


def native_queries(obj, wanted, result, forbidden=()):
    selected = wanted[::max(1, len(wanted)//20)][:20]
    for content in selected:
        key = content.split(",", 1)[0].removeprefix("For key ")
        start = time.perf_counter()
        out = json.loads(obj.handle_tool_call("memory_search", {
            "query": key, "mode": "fts", "limit": 5, "globs": ["facts/**"]}))
        text = out.get("results", "")
        body = hit_body(text)
        hit = bool(re.match(r"facts/[^\n]+:\d+", body)) and content in body
        result["queries"].append({"key": key, "hit": hit, "chars": len(text),
                                  "seconds": time.perf_counter()-start, "response": out})
        if out.get("error") or not hit or len(text) > 2000:
            result["errors"].append(f"native query failed: {key}")
    result["hit_at_5"] = sum(q["hit"] for q in result["queries"])/len(selected) if selected else None
    result["query_latency_seconds"] = latency([q["seconds"] for q in result["queries"]])
    result["negative_queries"] = []
    for content in forbidden[::max(1, len(forbidden)//20)][:20]:
        key = content.split(",", 1)[0].removeprefix("For key ")
        start = time.perf_counter()
        out = json.loads(obj.handle_tool_call("memory_search", {
            "query": key, "mode": "fts", "limit": 5, "globs": ["facts/**"]}))
        text = out.get("results", "")
        stale = content in hit_body(text)
        result["negative_queries"].append({"key": key, "stale": stale, "response": out,
                                           "seconds": time.perf_counter()-start})
        if stale or out.get("error") or len(text) > 2000:
            result["errors"].append(f"stale/failed negative native query: {key}")


def recover(run, workers, records, mode, timeout=120, shutdown_policy="immediate"):
    result = {"errors": [], "queries": [], "shutdown_policy": shutdown_policy}
    obj = None
    start = time.monotonic()
    try:
        obj = provider(run, mode, "stress-recovery")
        while time.monotonic()-start < timeout:
            obj.queue_prefetch("Inspect synthetic stress recovery status")
            if obj._recall_token() is not None and obj._index_ready():
                break
            time.sleep(.1)
        else:
            raise RuntimeError("automatic convergence deadline exceeded")
        result["convergence_seconds"] = time.monotonic()-start
        state = obj._mirror_state()
        wanted = expected(run.name, workers, records)
        result["mirror_records"] = len(state["records"])
        result["expected_mirror_records"] = len(wanted)
        result["errors"].extend(validate_state(state, wanted))
        result["inbox_empty"] = obj._mirror_inbox.first() is None
        if not result["inbox_empty"]:
            result["errors"].append("inbox not drained")
        if (run / "vault/.mirror-delivery-failed.json").exists():
            result["errors"].append("delivery-failure marker present")
        forbidden = [fact(run.name, w, n, revised) for w in range(workers) for n in range(records)
                     for revised in (False, True) if not revised or not n % 2]
        controls = [json.loads((run / f"worker-{n}.json").read_text()).get("control") for n in range(workers)]
        result["errors"].extend(validate_sources(run / "vault", state, forbidden, controls))
        if mode == "native":
            status = subprocess.run([str(ZG), "status", str(run / "vault"), "--check-ready"],
                                    capture_output=True, text=True, timeout=30)
            result["native_status"] = {"exit": status.returncode, "stdout": status.stdout, "stderr": status.stderr}
            if status.returncode:
                raise RuntimeError("native index not ready")
            native_queries(obj, wanted or [c["content"] for c in controls if c], result, forbidden)
    except Exception as exc:
        result["errors"].append(repr(exc))
        result["traceback"] = traceback.format_exc()
        print(result["traceback"], file=sys.stderr)
    finally:
        close_provider(obj, result)
        result["elapsed_seconds"] = time.monotonic()-start
        write_json(run / "recovery.json", result)
    return int(bool(result["errors"]))


def enable_subreaper():
    # Adopt only descendants; reap below by owned process-group, never waitpid(-1).
    import ctypes
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(36, 1, 0, 0, 0) != 0:  # Linux PR_SET_CHILD_SUBREAPER
        raise OSError(ctypes.get_errno(), "cannot establish child subreaper")


def process_identity(pid):
    fields = Path(f"/proc/{pid}/stat").read_text().rsplit(") ", 1)[1].split()
    return int(fields[19]), int(fields[2]), int(fields[3])  # birth, group, session


def pin_identity(pid, expected_identity):
    fd = os.pidfd_open(pid)
    try:
        if process_identity(pid) != expected_identity:
            raise RuntimeError("process identity changed while opening pidfd")
        return fd
    except BaseException:
        os.close(fd)
        raise


def track_child(child):
    child._stress_identity = process_identity(child.pid)
    child._stress_handles = {child.pid: pin_identity(child.pid, child._stress_identity)}
    return child


def observe_descendants(child):
    # Never discover using a reusable group ID after the leader is reaped.
    if child.returncode is not None:
        return
    if process_identity(child.pid) != child._stress_identity:
        raise RuntimeError("owned child identity changed")
    pending = [child.pid, os.getpid()]
    visited = set()
    while pending:
        parent = pending.pop()
        if parent in visited:
            continue
        visited.add(parent)
        for path in Path(f"/proc/{parent}/task").glob("*/children"):
            try:
                descendants = [int(pid) for pid in path.read_text().split()]
            except FileNotFoundError:
                continue
            for pid in descendants:
                try:
                    identity = process_identity(pid)
                    if identity[1:] != (child.pid, child.pid) or identity[0] < child._stress_identity[0]:
                        continue
                    pending.append(pid)
                    if pid not in child._stress_handles:
                        child._stress_handles[pid] = pin_identity(pid, identity)
                except (FileNotFoundError, ProcessLookupError):
                    continue


def poll_owned(child):
    if child.returncode is not None:
        return child.returncode
    observe_descendants(child)
    status = os.waitid(os.P_PIDFD, child._stress_handles[child.pid],
                       os.WEXITED | os.WNOHANG | os.WNOWAIT)
    if status is None:
        return None
    # Keep the exited leader unreaped so its PID/session cannot be reused
    # until the final post-reparenting descendant scan pins live identities.
    observe_descendants(child)
    return child.poll()


def cleanup_owned(child):
    # Signal pinned kernel identities ONLY; the parent cgroup catches unobserved
    # descendants (including processes deliberately escaping their session).
    leftover = False
    for pid, fd in child._stress_handles.items():
        try:
            signal.pidfd_send_signal(fd, signal.SIGKILL)
            leftover |= pid != child.pid
        except ProcessLookupError:
            pass
        finally:
            os.close(fd)
    child.wait()
    for pid in child._stress_handles:
        if pid == child.pid:
            continue
        try:
            os.waitpid(pid, 0)
        except ChildProcessError:
            pass
    return leftover


def run_command(command, env, logfile, timeout):
    enable_subreaper()
    with logfile.open("w") as log:
        child = track_child(subprocess.Popen(command, cwd=ROOT, env=env, stdout=log,
                                 stderr=subprocess.STDOUT, start_new_session=True))
        deadline = time.monotonic() + timeout
        try:
            while poll_owned(child) is None:
                if time.monotonic() >= deadline:
                    break
                time.sleep(.01)
            rc = child.returncode if child.returncode is not None else 124
        finally:
            leftover = cleanup_owned(child)
        return rc or (125 if leftover else 0)


def load_campaign(run, args, report):
    enable_subreaper()
    vault = run / "vault"
    (vault / "facts").mkdir(parents=True)
    (vault / "sessions").mkdir()
    for n in range(args.seed_facts):
        (vault / "facts" / f"background-{n:06d}.md").write_text(f"Synthetic archival fixture {n}; no user data.\n")
    if args.mode == "offline":
        (vault / ".zvec-grep").mkdir()
        (vault / ".zvec-grep/manifest.json").write_text("{}")
    report["initial_inventory"] = inventory(run)
    env = environment(run, ROOT)
    children, handles = [], []
    deadline = time.monotonic() + args.timeout
    began = time.monotonic()
    try:
        for n in range(args.workers):
            log = (run / f"worker-{n}.log").open("w")
            handles.append(log)
            cmd = [sys.executable, str(Path(__file__).resolve()), "--worker", str(n), "--run", str(run),
                   "--records", str(args.records), "--workers", str(args.workers), "--mode", args.mode,
                   "--shutdown-policy", getattr(args, "shutdown_policy", "immediate"),
                   "--timeout", str(args.timeout)]
            children.append(track_child(subprocess.Popen(cmd, cwd=ROOT, env=env, stdout=log,
                                             stderr=subprocess.STDOUT, start_new_session=True)))
        while any(poll_owned(child) is None for child in children):
            if any(poll_owned(child) not in (None, 0) for child in children):
                raise RuntimeError("unexpected child exit; aborting peer writers")
            if time.monotonic() >= deadline:
                raise TimeoutError("writer phase deadline exceeded")
            time.sleep(.02)
    except Exception as exc:
        report["errors"].append(f"writers: {exc!r}")
    finally:
        for child in children:
            if cleanup_owned(child):
                report["errors"].append(f"leftover descendants of worker pid {child.pid}")
        for log in handles:
            log.close()
    report["writers_seconds"] = time.monotonic()-began
    for n in range(args.workers):
        path = run / f"worker-{n}.json"
        rc = children[n].returncode if n < len(children) else None
        if rc or rc is None or not path.exists():
            report["errors"].append(f"worker {n} exited {rc}; receipt present={path.exists()}")
        try:
            receipt = json.loads(path.read_text())
        except Exception as exc:
            receipt = {"worker": n, "samples": [], "errors": [f"missing/invalid worker receipt: {exc!r}"],
                       "receipt_origin": "coordinator", "successful_callbacks": 0,
                       "shutdown_policy": getattr(args, "shutdown_policy", "immediate"),
                       "admission_seconds": None, "drain_seconds": None,
                       "pending_at_shutdown": None, "callbacks_per_second_admission": None,
                       "acceptance_count_complete": False,
                       "planned_callbacks": 2*args.records+(args.records+1)//2,
                       "elapsed_seconds": report["writers_seconds"], "exit": rc}
            progress = run / f"worker-{n}-callbacks.jsonl"
            if progress.exists():
                for line in progress.read_text().splitlines():
                    try:
                        receipt["samples"].append(json.loads(line))
                    except ValueError:
                        receipt["errors"].append("truncated callback progress line")
                receipt["successful_callbacks"] = len(receipt["samples"])
            write_json(run / f"worker-{n}-failure.json", receipt)
        receipt["errors"].extend(validate_acceptance(receipt, n, args.records))
        report["workers"].append(receipt)
        report["errors"].extend(receipt["errors"])
    if report["errors"]:
        report["recovery_skipped"] = "writer failure; escalation stopped"
        return
    rc = None
    try:
        rc = run_command([sys.executable, str(Path(__file__).resolve()), "--recover", "--run", str(run),
                          "--workers", str(args.workers), "--records", str(args.records), "--mode", args.mode,
                          "--shutdown-policy", getattr(args, "shutdown_policy", "immediate"),
                          "--recovery-timeout", str(args.recovery_timeout)],
                         env, run / "recovery.log", args.recovery_timeout + 10)
    except Exception as exc:
        report["errors"].append(f"recovery: {exc!r}")
    if rc:
        report["errors"].append(f"recovery exited {rc}")
    path = run / "recovery.json"
    if path.exists():
        report["recovery"] = json.loads(path.read_text())
        report["errors"].extend(report["recovery"]["errors"])
    else:
        report["recovery"] = {"errors": ["missing recovery receipt"], "receipt_origin": "coordinator", "exit": rc}
        write_json(run / "recovery-failure.json", report["recovery"])
        report["errors"].extend(report["recovery"]["errors"])


FAULT_FILES = ["tests/test_process_safety.py", "tests/test_mirror_queue.py",
               "tests/test_durable_recovery.py", "tests/test_workers.py", "tests/test_inbox_contention.py"]


def read_junit(path):
    from xml.etree import ElementTree
    suites = [s for s in ElementTree.parse(path).iter("testsuite") if not s.findall("testsuite")]
    counts = {k: sum(int(s.get(k, "0")) for s in suites) for k in ("tests", "skipped", "failures", "errors")}
    if counts["tests"] <= 0 or any(counts[k] for k in ("skipped", "failures", "errors")):
        raise ValueError(f"empty, skipped or failed coverage: {counts}")
    return counts


def regression_campaign(run, args, report):
    native = args.lane == "native-regression"
    files = ["tests/test_zg_native.py", "tests/test_provider_native.py"] if native else FAULT_FILES
    report["iterations"] = []
    report["planned_iterations"] = args.iterations
    report["completed_iterations"] = 0
    for i in range(args.iterations):
        root = run / f"iteration-{i}"
        prepare_home(root)
        xml = root / "junit.xml"
        cmd = [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
               "--basetemp", str(root / "pytest"), "--junitxml", str(xml), *files]
        env = environment(root, ROOT)
        if native:
            env.update(ZVEC_RUN_NATIVE="1", ZVEC_TEST_BIN=str(ZG),
                       ZVEC_TEST_MODEL_CACHE=str(ROOT / ".test-tools/models"))
        began = time.monotonic()
        receipt = {"number": i, "errors": []}
        try:
            receipt["exit"] = run_command(cmd, env, root / "pytest.log", args.timeout)
            if receipt["exit"]:
                raise RuntimeError(f"pytest exited {receipt['exit']}")
            receipt.update(read_junit(xml))
            report["completed_iterations"] += 1
        except Exception as exc:
            receipt["errors"].append(repr(exc))
            report["errors"].append(f"iteration {i}: {exc!r}")
        finally:
            receipt["elapsed_seconds"] = time.monotonic()-began
            write_json(root / "receipt.json", receipt)
            report["iterations"].append(receipt)
        if receipt["errors"]:
            break


def interrupted(signum, frame):
    reason = "campaign deadline exceeded" if signum == signal.SIGALRM else f"campaign interrupted by signal {signum}"
    raise RuntimeError(reason)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--lane", choices=["regression", "native-regression", "load"], default="regression")
    ap.add_argument("--mode", choices=["offline", "native"], default="offline")
    ap.add_argument("--shutdown-policy", choices=["immediate", "drain"], default="immediate",
                    help="load shutdown policy; drain waits for automatic convergence before the unchanged shutdown grace")
    ap.add_argument("--iterations", type=int, default=10)
    ap.add_argument("--workers", type=int, default=1)
    ap.add_argument("--records", type=int, default=20)
    ap.add_argument("--seed-facts", type=int, default=0)
    ap.add_argument("--timeout", type=int, default=180, help="writer or per-regression phase seconds")
    ap.add_argument("--campaign-timeout", type=int, default=1800)
    ap.add_argument("--recovery-timeout", type=int, default=120)
    ap.add_argument("--worker", type=int, help=argparse.SUPPRESS)
    ap.add_argument("--recover", action="store_true", help=argparse.SUPPRESS)
    ap.add_argument("--run", type=Path, help=argparse.SUPPRESS)
    args = ap.parse_args(argv)
    if not (1 <= args.workers <= 8 and 1 <= args.records <= 1000 and 0 <= args.seed_facts <= 10000
            and 1 <= args.iterations <= 500 and 1 <= args.timeout <= 1800
            and 1 <= args.campaign_timeout <= 1800 and 1 <= args.recovery_timeout <= 120):
        ap.error("workload outside explicit safety bounds")
    if args.run is not None and args.worker is None and not args.recover:
        ap.error("--run is internal only; arbitrary paths forbidden")
    if args.worker is not None and (args.recover or not 0 <= args.worker < args.workers):
        ap.error("invalid internal worker selection")
    if args.worker is not None or args.recover:
        if args.run is None or args.run.resolve().parent != RUNS.resolve() or not (args.run / "OWNER.json").is_file():
            ap.error("refusing non-harness run directory")
        try:
            if args.run.is_symlink() or json.loads((args.run / "OWNER.json").read_text()) != {"kind": "zvec-stress", "repo": str(ROOT)}:
                raise ValueError("owner marker mismatch")
            for path in args.run.rglob("*"):
                if path.is_symlink():
                    raise ValueError("symlink inside run")
        except Exception as exc:
            ap.error(f"refusing invalid owned run: {exc}")
        os.environ.clear()
        os.environ.update(environment(args.run, ROOT))
        if args.worker is not None:
            return worker(args.run, args.worker, args.records, args.mode, args.shutdown_policy, args.timeout)
        return recover(args.run, args.workers, args.records, args.mode, args.recovery_timeout, args.shutdown_policy)
    if args.lane == "regression" and args.mode == "native":
        ap.error("regression is offline; choose native-regression for native evidence")
    if args.lane == "native-regression":
        args.mode = "native"
    RUNS.mkdir(parents=True, exist_ok=True)
    run = Path(tempfile.mkdtemp(prefix="stress-", dir=RUNS))
    prepare_home(run)
    write_json(run / "OWNER.json", {"kind": "zvec-stress", "repo": str(ROOT)})
    report = {"lane": args.lane, "mode": args.mode, "run": str(run), "errors": [], "workers": [],
              "shutdown_policy": args.shutdown_policy,
              "arguments": vars(args), "python": sys.version,
              "evidence": "offline engine mock; NOT native evidence" if args.mode == "offline" else "native direct engine"}
    report.update(status="running", passed=False)
    write_json(run / "report.json", report)
    began = time.monotonic()
    handlers = {sig: signal.signal(sig, interrupted) for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGALRM)}
    signal.setitimer(signal.ITIMER_REAL, args.campaign_timeout)
    try:
        if args.mode == "native":
            if not ZG.is_file() or ZG.is_symlink():
                raise RuntimeError("raw test zg missing or symlink; wrappers forbidden")
            package = json.loads((ZG.parents[2] / "package.json").read_text())
            report["zg_version"] = package["version"]
            if report["zg_version"] != "0.2.2":
                raise RuntimeError("reviewed native engine version must be 0.2.2")
        for key, repo in (("plugin_sha", ROOT), ("host_sha", HOST)):
            report[key] = subprocess.check_output(["git", "-C", str(repo), "rev-parse", "HEAD"], text=True, timeout=5).strip()
        if args.lane == "load":
            load_campaign(run, args, report)
        else:
            regression_campaign(run, args, report)
    except Exception as exc:
        report["errors"].append(repr(exc))
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        for sig, handler in handlers.items():
            signal.signal(sig, handler)
    report["status"] = "finished"
    report["elapsed_seconds"] = time.monotonic()-began
    samples = [s for w in report["workers"] for s in w["samples"]]
    report["successful_callbacks"] = len(samples)
    report["planned_callbacks"] = args.workers * (2*args.records + (args.records+1)//2) if args.lane == "load" else 0
    if len(samples) != report["planned_callbacks"]:
        report["errors"].append("successful callback count differs from workload")
    report["callback_latency_seconds"] = latency([s["seconds"] for s in samples])
    report["callback_latency_by_phase"] = {p: latency([s["seconds"] for s in samples if s["phase"] == p])
                                           for p in ("add", "replace", "remove")}
    report["callbacks_per_second_including_recovery"] = len(samples)/report["elapsed_seconds"]
    report["artifact_inventory"] = inventory(run)
    report["artifact_bytes"] = report["artifact_inventory"]["bytes"]
    if "initial_inventory" in report:
        report["artifact_growth"] = {k: report["artifact_inventory"][k]-report["initial_inventory"][k]
                                     for k in ("file_count", "bytes")}
    report["peak_single_reaped_child_rss_kib"] = resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss
    report["passed"] = not report["errors"]
    write_json(run / "report.json", report)
    print(json.dumps({"passed": report["passed"], "report": str(run / "report.json")}))
    return int(not report["passed"])


if __name__ == "__main__":
    raise SystemExit(main())
