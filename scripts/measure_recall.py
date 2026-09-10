#!/usr/bin/env python3
"""Measure synthetic recall in an isolated HOME, Hermes profile and zg state.

Seeds 20 facts before native rebuild/readiness and provider initialization.
Reports top-5 hybrid/FTS retrieval, capped visibility, raw latency samples and
p50/p95. The repeated-identical-query cache microbenchmark is separate from
distinct next-turn queries; it does not establish predictive prefetch benefits.

Gates: near hybrid retrieved_hit@5 >= 0.8, visible_hit@5 >= 0.8,
context <= 2000 characters, and no execution errors. No latency threshold.
The default local model is unchanged. Native indexing may download that model.

Example (from repository root):
    HERMES_AGENT_DIR=/path/to/hermes-agent .venv/bin/python scripts/measure_recall.py \
        --zg-bin .test-tools/node_modules/.bin/zg --output benchmark.json

Use --model-cache /explicit/cache to reuse local model downloads; otherwise
cache and synthetic data are temporary. Shutdown and thread completion precede
cleanup. Run as a standalone process, not inside a multithreaded application.
The JSON file retains exact observations, command output, errors and SHAs;
stdout prints the same record followed by PASS/FAIL. Exit status is 0/1.
"""

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
HERMES_AGENT_DIR = Path(os.environ.get(
    "HERMES_AGENT_DIR", str(Path.home() / ".hermes" / "hermes-agent")))
sys.path.insert(0, str(HERMES_AGENT_DIR))

FACTS = [
    ("project", "Production deploys require the canary gate to pass before any rollout proceeds", "deploy"),
    ("user_pref", "User prefers concise replies with no filler and plain-text formatting", "style"),
    ("tool", "The staging database lives at db-staging.internal on port 5433 with read-only credentials", "db"),
    ("general", "The office espresso machine needs descaling every third Friday", "office"),
    ("project", "API rate limits are 100 requests per minute per key, burst 150", "api"),
    ("user_pref", "User works in the America/Chicago timezone and reads English only", "locale"),
    ("tool", "Log aggregation runs through Loki; retention is 30 days for app logs", "observability"),
    ("project", "Feature flags are managed in Unleash; the kill switch is named payments-off", "flags"),
    ("general", "The team offsite is booked in Austin for the second week of October", "team"),
    ("tool", "GPU nodes need nvidia-container-toolkit installed before any CUDA workload", "gpu"),
    ("project", "Migrations run forward-only; rollbacks restore from snapshot instead", "db"),
    ("user_pref", "User reviews infrastructure diffs before any apply and never auto-approves", "infra"),
    ("general", "The shared 3D printer filament stock is PLA only; no ABS indoors", "office"),
    ("tool", "CI runners cache pip wheels under /opt/wheelhouse to survive outages", "ci"),
    ("project", "Customer data exports must be approved by security and expire in 7 days", "privacy"),
    ("user_pref", "User likes benchmark evidence with exact SHAs, never bare claims", "evidence"),
    ("general", "The backup NAS scrubs its array on the first Sunday of each month", "storage"),
    ("tool", "SSH to jump hosts requires the ed25519 key at ~/.ssh/jump_ed25519", "ssh"),
    ("project", "Incident reviews happen within 48 hours and produce dated action items", "incidents"),
    ("general", "The office plants are watered on Tuesdays by whoever arrives first", "office"),
]

PARAPHRASE_QUERIES = [
    ("what must pass before shipping to production", "canary gate"),
    ("where is the pre-production postgres and how do I connect", "db-staging.internal"),
    ("how many calls can one client make in sixty seconds", "100 requests per minute"),
    ("where do we look at old application logs and how far back", "Loki"),
    ("where and when is the group trip", "Austin"),
    ("how do we undo a bad schema change", "snapshot"),
    ("what is the policy on applying infrastructure changes", "auto-approves"),
    ("how do builds keep working when the package index is down", "/opt/wheelhouse"),
    ("what proof is expected in performance reports", "exact SHAs"),
    ("which credential gets me into the bastion servers", "jump_ed25519"),
]

NEAR_QUERIES = [
    ("what is required before a production rollout", "canary gate"),
    ("staging database host and port", "db-staging.internal"),
    ("API rate limit per key", "100 requests per minute"),
    ("log retention for app logs", "Loki"),
    ("team offsite location and dates", "Austin"),
    ("how are database rollbacks handled", "snapshot"),
    ("policy on applying infrastructure diffs", "auto-approves"),
    ("where are pip wheels cached for CI", "/opt/wheelhouse"),
    ("what evidence belongs in benchmark reports", "exact SHAs"),
    ("which SSH key for jump hosts", "jump_ed25519"),
]


def evaluate_metrics(sets, errors, cap=2000):
    """Pure correctness gates; latency is evidence, not a guessed threshold."""
    near = sets.get("near", {})
    n = near.get("count", 0)
    failures = []
    if not n or near.get("hits", {}).get("hybrid", 0) / n < 0.8:
        failures.append("near retrieved_hit@5 < 0.8")
    if not n or near.get("visible_hits", 0) / n < 0.8:
        failures.append("near visible_hit@5 < 0.8")
    if any(size > cap for result in sets.values() for size in result.get("chars", [])):
        failures.append(f"context exceeds cap={cap}")
    if errors:
        failures.append("execution errors invalidate benchmark")
    return {"passed": not failures, "failures": failures}


def pct(xs, q):
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(q * len(xs)))]


def run_set(p, p_full, queries):
    """Retain per-query evidence; failed queries are errors, never misses."""
    result = {"count": len(queries), "hits": {"hybrid": 0, "fts": 0},
              "visible_hits": 0, "chars": [], "misses": [], "errors": [],
              "latency_s": {"hybrid": [], "fts": [], "visible": []}, "samples": []}
    for query, expected in queries:
        for mode, provider in (("hybrid", p_full), ("fts", p_full), ("visible", p)):
            raw = None
            start = time.perf_counter()
            try:
                raw = provider.handle_tool_call("memory_search", {
                    "query": query, "mode": "fts" if mode == "fts" else "hybrid", "limit": 5})
                out = json.loads(raw)
                if (not isinstance(out, dict) or "error" in out
                        or out.get("success") is False or not isinstance(out.get("results"), str)):
                    raise ValueError("invalid or failed memory_search response")
                body = out["results"]
            except Exception as exc:
                result["errors"].append({"query": query, "mode": mode,
                                         "raw": raw, "error": str(exc)})
                continue
            elapsed = time.perf_counter() - start
            hit = expected in body
            result["latency_s"][mode].append(elapsed)
            result["samples"].append({"query": query, "expected": expected, "mode": mode,
                                      "elapsed_s": elapsed, "body": body, "hit": hit})
            if mode == "visible":
                result["chars"].append(len(body))
                result["visible_hits"] += int(hit)
            else:
                result["hits"][mode] += int(hit)
                if not hit and mode == "hybrid":
                    result["misses"].append({"query": query, "expected": expected, "body": body})
    n = result["count"]
    result["retrieved_hit_at_5"] = {mode: hits / n if n else None
                                    for mode, hits in result["hits"].items()}
    result["visible_hit_at_5"] = result["visible_hits"] / n if n else None
    chars = result["chars"]
    result["context_chars"] = {"mean": sum(chars) / len(chars) if chars else None,
                               "max": max(chars) if chars else None}
    return result


def load_provider():
    """Import only after HOME/HERMES_HOME have been isolated."""
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "zvec_benchmark_provider", REPO_ROOT / "zvec-memory" / "__init__.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module.ZvecMemoryProvider


def latency_summary(samples):
    return {"p50_s": pct(samples, .5), "p95_s": pct(samples, .95)} if samples else {
        "p50_s": None, "p95_s": None}


def measure_prefetch(provider, errors, timeout=60):
    """Identical-query microbenchmark is not a next-turn prediction benchmark."""
    query = PARAPHRASE_QUERIES[0][0]
    start = time.perf_counter()
    cold_body = provider.prefetch(query)
    cold = time.perf_counter() - start
    provider.queue_prefetch(query)
    deadline = time.monotonic() + timeout
    while provider._cached_prefetch(query) is None and time.monotonic() < deadline:
        time.sleep(.05)
    ready = provider._cached_prefetch(query) is not None
    repeat = {"query": query, "uncached_s": cold, "cache_ready": ready,
              "uncached_chars": len(cold_body), "cached_chars": None, "cached_s": None}
    if ready:
        start = time.perf_counter()
        cached_body = provider.prefetch(query)
        repeat["cached_s"] = time.perf_counter() - start
        repeat["cached_chars"] = len(cached_body)
    else:
        errors.append({"stage": "prefetch", "query": query, "error": "cache readiness timeout"})
    if max(repeat["uncached_chars"], repeat["cached_chars"] or 0) > 2000:
        errors.append({"stage": "repeated_prefetch", "error": "context exceeds cap"})
    samples = []
    for query, expected in NEAR_QUERIES:
        hit = provider._cached_prefetch(query) is not None
        start = time.perf_counter()
        body = provider.prefetch(query)
        samples.append({"query": query, "cache_hit_before": hit,
                        "elapsed_s": time.perf_counter() - start,
                        "chars": len(body), "visible_hit": expected in body})
        # Mirrors the host's SAME completed-turn query, not prediction of the next.
        provider.queue_prefetch(query)
    return {"repeated_identical_query": repeat,
            "distinct_next_turn_queries": {"samples": samples,
                **latency_summary([s["elapsed_s"] for s in samples])}}


def main(argv=None):
    import threading
    import platform
    import hashlib
    from contextlib import contextmanager

    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--embedding", default="local/potion-retrieval-32m")
    ap.add_argument("--zg-bin", default="zg")
    ap.add_argument("--output", type=Path, help="write full JSON evidence, including failures")
    ap.add_argument("--model-cache", type=Path,
                    help="explicit reusable zg model cache (default: isolated temporary cache)")
    args = ap.parse_args(argv)
    # Resolve supplied paths BEFORE replacing HOME.
    args.zg_bin = str(Path(args.zg_bin).expanduser().resolve()) if "/" in args.zg_bin else args.zg_bin
    cache = args.model_cache.expanduser().resolve() if args.model_cache else None
    errors = []
    record = {"schema_version": 1, "embedding": args.embedding, "facts": len(FACTS),
              "cap": 2000, "errors": errors, "sets": {}, "commands": [],
              "provenance": {"python": sys.version, "platform": platform.platform(),
                  "hermes_agent_dir": str(HERMES_AGENT_DIR), "zg_bin": args.zg_bin,
                  "model_cache": str(cache) if cache else None,
                  "facts_sha256": hashlib.sha256(json.dumps(FACTS).encode()).hexdigest()}}

    def command(argv, timeout=60, required=True):
        start = time.perf_counter()
        try:
            result = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            def decoded(value):
                return value.decode("utf-8", errors="replace") if isinstance(value, bytes) else value
            evidence = {"argv": argv, "error": "timeout", "timeout_s": timeout,
                        "stdout": decoded(exc.stdout), "stderr": decoded(exc.stderr),
                        "elapsed_s": time.perf_counter() - start}
            record["commands"].append(evidence)
            errors.append(evidence)
            raise
        evidence = {"argv": argv, "returncode": result.returncode,
                    "stdout": result.stdout, "stderr": result.stderr,
                    "elapsed_s": time.perf_counter() - start}
        record["commands"].append(evidence)
        if required and result.returncode:
            errors.append(evidence)
            raise RuntimeError(f"command failed: {argv!r}")
        return result

    @contextmanager
    def environment(home):
        values = {"HOME": str(home), "HERMES_HOME": str(home / "hermes"),
                  "XDG_CONFIG_HOME": str(home / "config"),
                  "XDG_DATA_HOME": str(home / "data"),
                  "ZVEC_GREP_HOME": str(home / "zvec-state"),
                  "ZVEC_GREP_MODE": "direct",
                  "ZVEC_GREP_MODEL_CACHE": str(cache or home / "cache" / "zvec-models"),
                  "XDG_CACHE_HOME": str(home / "cache"),
                  "HF_HOME": str((cache or home / "cache") / "huggingface"),
                  "HUGGINGFACE_HUB_CACHE": str((cache or home / "cache") / "huggingface" / "hub"),
                  "TRANSFORMERS_CACHE": str((cache or home / "cache") / "transformers")}
        previous = {key: os.environ.get(key) for key in values}
        os.environ.update(values)
        try:
            yield
        finally:
            for key, value in previous.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value

    try:
        with tempfile.TemporaryDirectory(prefix="zvec-measure-") as directory:
            with environment(Path(directory)):
                for key, argv in (
                    ("plugin_sha", ["git", "-C", str(REPO_ROOT), "rev-parse", "HEAD"]),
                    ("plugin_status", ["git", "-C", str(REPO_ROOT), "status", "--porcelain"]),
                    ("hermes_sha", ["git", "-C", str(HERMES_AGENT_DIR), "rev-parse", "HEAD"]),
                    ("node_version", ["node", "--version"]),
                    ("zg_version", [args.zg_bin, "--version"]),
                ):
                    record["provenance"][key] = command(argv).stdout.strip()
                provider_class = load_provider()
                vault = Path(directory) / "vault"
                facts = vault / "facts"
                facts.mkdir(parents=True)
                for i, (category, text, tags) in enumerate(FACTS):
                    (facts / f"fact-{i:02d}.md").write_text(
                        f"---\ncategory: {category}\ntags: {tags}\n---\n\n{text}\n", encoding="utf-8")
                start = time.perf_counter()
                command([args.zg_bin, "index", "--rebuild", "--embedding", args.embedding, str(vault)], 900)
                record["rebuild_index_s"] = time.perf_counter() - start
                command([args.zg_bin, "status", str(vault), "--check-ready"])
                cfg = {"vault": str(vault), "recall_limit": 5, "context_chars": 2000,
                       "reindex_min_seconds": 3600, "zg_bin": args.zg_bin, "embedding": args.embedding}
                providers = []
                existing_threads = set(threading.enumerate())
                try:
                    for label, budget in (("measure", 2000), ("measure-full", 100000)):
                        provider = provider_class(config={**cfg, "context_chars": budget})
                        providers.append(provider)
                        # Observe actual native failures, including best-effort prefetch
                        # which intentionally returns empty context to the host on failure.
                        original = provider._run_zg
                        def observed(argv, _original=original, **kwargs):
                            rc, out, err = _original(argv, **kwargs)
                            if rc:
                                errors.append({"stage": "provider_native", "argv": argv,
                                               "returncode": rc, "stdout": out, "stderr": err})
                            return rc, out, err
                        provider._run_zg = observed
                        provider.initialize(label, hermes_home=os.environ["HERMES_HOME"])
                    p, p_full = providers
                    for name, queries in (("paraphrase", PARAPHRASE_QUERIES), ("near", NEAR_QUERIES)):
                        result = run_set(p, p_full, queries)
                        result["latency_summary"] = {mode: latency_summary(values)
                            for mode, values in result["latency_s"].items()}
                        record["sets"][name] = result
                        errors.extend(result["errors"])
                    record["prefetch"] = measure_prefetch(p, errors)
                    prefetch_chars = [s["chars"] for s in record["prefetch"]["distinct_next_turn_queries"]["samples"]]
                    if any(size > record["cap"] for size in prefetch_chars):
                        errors.append({"stage": "prefetch", "error": "context exceeds cap"})
                finally:
                    for provider in providers:
                        try:
                            provider.shutdown()
                        except Exception as exc:
                            errors.append({"stage": "shutdown", "error": repr(exc)})
                    # Standalone benchmark owns threads created during this runtime.
                    # Do not inspect evolving provider worker internals or use shutdown
                    # as a mid-run drain. Wait even after provider's bounded shutdown
                    # expires: deleting a vault while a worker uses it is unsafe.
                    while True:
                        remaining = [t for t in threading.enumerate() if t not in existing_threads]
                        if not remaining:
                            break
                        for thread in remaining:
                            thread.join()
    except Exception as exc:
        errors.append({"stage": "benchmark", "error": repr(exc)})
    record["gates"] = evaluate_metrics(record["sets"], errors, record["cap"])
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(record, indent=2))
    print("PASS" if record["gates"]["passed"] else "FAIL")
    return 0 if record["gates"]["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
