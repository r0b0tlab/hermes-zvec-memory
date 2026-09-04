#!/usr/bin/env python3
"""Measure zvec-memory recall quality, latency, and context cost.

Seeds an isolated vault with 20 known facts, rebuilds the index from scratch,
then runs two query sets through ``memory_search`` in hybrid vs fts-only mode:

* ``paraphrase`` — semantic wording with no shared keywords (worst case for a
  small static embedding model);
* ``near`` — the user's own vocabulary (the realistic memory-recall case).

Reports retrieved_hit@5 (was the fact in the top-5?), visible_hit@5 (did it
survive the 2000-char injection cap?), p50/p95 latency, and injected context
size — plus cold vs warmed-cache prefetch latency, which is what
``queue_prefetch`` buys.

Run with the Hermes gateway venv::

    ~/.hermes/hermes-agent/venv/bin/python scripts/measure_recall.py
    ~/.hermes/hermes-agent/venv/bin/python scripts/measure_recall.py \\
        --embedding local/embeddinggemma-300m

Needs ``zg`` on PATH. Exits nonzero if near-vocabulary hybrid hit@5 < 0.8.
"""

import argparse
import json
import os
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
HERMES_AGENT_DIR = Path(os.environ.get(
    "HERMES_AGENT_DIR", str(Path.home() / ".hermes" / "hermes-agent")))
sys.path.insert(0, str(HERMES_AGENT_DIR))

import importlib.util  # noqa: E402

_spec = importlib.util.spec_from_file_location(
    "zvec_memory_provider", str(REPO_ROOT / "zvec-memory" / "__init__.py"))
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

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


def pct(xs, q):
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(q * len(xs)))]


def run_set(p, p_full, queries):
    """Return (hits, visible_hits, lat, chars, misses) for one query set."""
    lat = {"hybrid": [], "fts": []}
    hits = {"hybrid": 0, "fts": 0}
    visible_hits = 0
    chars = []
    misses = []
    for q, expected in queries:
        for mode in ("hybrid", "fts"):
            t0 = time.time()
            out = json.loads(p_full.handle_tool_call(
                "memory_search", {"query": q, "mode": mode, "limit": 5}))
            dt = time.time() - t0
            lat[mode].append(dt)
            body = out.get("results", "")
            if expected in body:
                hits[mode] += 1
            elif mode == "hybrid":
                misses.append((q, expected, body[:200].replace("\n", " | ")))
        capped_body = json.loads(p.handle_tool_call(
            "memory_search", {"query": q, "mode": "hybrid", "limit": 5})).get("results", "")
        chars.append(len(capped_body))
        if expected in capped_body:
            visible_hits += 1
    return hits, visible_hits, lat, chars, misses


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--embedding", default="local/potion-retrieval-32m")
    args = ap.parse_args()

    tmp = Path(tempfile.mkdtemp(prefix="zvec-measure-"))
    vault = tmp / "vault"
    cfg = {"vault": str(vault), "recall_limit": 5, "context_chars": 2000,
           "reindex_min_seconds": 3600}
    p = _mod.ZvecMemoryProvider(config=cfg)
    # Second handle on the same vault with no context cap: separates
    # retrieval quality (was the fact in the top-5?) from visibility
    # (did it survive the 2000-char injection cap?). Created after the
    # rebuild below, when the manifest exists, so it spawns no build.
    p_full = None
    try:
        p.initialize("measure", hermes_home=str(tmp))
        for cat, text, tags in FACTS:
            p._write_fact(text, cat, tags)

        # Let the background first-build finish before the clean rebuild:
        # two concurrent index runs on one vault conflict.
        for _ in range(24):
            s = subprocess.run(["zg", "status", str(vault), "--check-ready"],
                               capture_output=True, text=True, timeout=60)
            if s.returncode == 0:
                break
            time.sleep(5)

        t0 = time.time()
        r = subprocess.run(
            ["zg", "index", "--rebuild", "--embedding", args.embedding, str(vault)],
            capture_output=True, text=True, timeout=900)
        build_s = time.time() - t0
        assert r.returncode == 0, r.stderr[-1000:]

        # Wait for a ready index.
        ready = False
        for _ in range(24):
            s = subprocess.run(["zg", "status", str(vault), "--check-ready"],
                               capture_output=True, text=True, timeout=60)
            if s.returncode == 0:
                ready = True
                break
            time.sleep(5)
        assert ready, "index never became ready"

        p_full = _mod.ZvecMemoryProvider(config={**cfg, "context_chars": 100000})
        p_full.initialize("measure-full", hermes_home=str(tmp))

        print(f"model={args.embedding} facts={len(FACTS)} rebuild_index_s={build_s:.1f}")
        ok = True
        for set_name, queries in (("paraphrase", PARAPHRASE_QUERIES), ("near", NEAR_QUERIES)):
            n = len(queries)
            hits, visible, lat, chars, misses = run_set(p, p_full, queries)
            print(f"[{set_name}] hybrid retrieved_hit@5={hits['hybrid']}/{n} "
                  f"visible_hit@5={visible}/{n} "
                  f"p50={pct(lat['hybrid'], .5):.2f}s p95={pct(lat['hybrid'], .95):.2f}s")
            print(f"[{set_name}] fts retrieved_hit@5={hits['fts']}/{n} "
                  f"p50={pct(lat['fts'], .5):.2f}s p95={pct(lat['fts'], .95):.2f}s")
            if set_name == "paraphrase":
                for q, expected, snippet in misses:
                    print(f"  miss: q={q!r} expected={expected!r} top={snippet!r}")
            else:
                if hits["hybrid"] < 0.8 * n:
                    print(f"FAIL: [{set_name}] hybrid retrieved_hit@5 below 0.8")
                    ok = False
        print(f"hybrid context chars (near set): mean={statistics.mean(chars):.0f} "
              f"max={max(chars)} (cap=2000)")

        # Cold (subprocess) vs warmed-cache prefetch latency.
        q0 = PARAPHRASE_QUERIES[0][0]
        t0 = time.time()
        p._run_prefetch_query(q0)
        cold_s = time.time() - t0
        p.queue_prefetch(q0)
        deadline = time.time() + 60
        while time.time() < deadline and not p._cached_prefetch(q0):
            time.sleep(1)
        t0 = time.time()
        p.prefetch(q0)
        warm_s = time.time() - t0
        print(f"prefetch latency: cold={cold_s:.2f}s warmed_cache={warm_s:.3f}s "
              f"speedup={cold_s / max(warm_s, 1e-3):.0f}x")

        print("PASS" if ok else "FAIL")
        return 0 if ok else 1
    finally:
        p.shutdown()
        if p_full is not None:
            p_full.shutdown()


if __name__ == "__main__":
    sys.exit(main())
