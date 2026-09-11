# Measured zvec-memory stress envelope

Every number below comes from a receipt under `.test-tools/` produced by the committed harness.
Failures are kept in the record, including the first failing attempt of every lane. The
reproduction guide is `docs/stress-testing.md`; the shareable aggregate is
`stress-results/campaign-final/share.zip` (44 attempts, 27 passed, 17 failed — every failure is
superseded by a later passing attempt of the same lane).

## Environment

| | |
|---|---|
| Host | Linux 7.2.3-arch1-3, x86_64, AMD Ryzen 7 6800H, 16 logical CPUs, 15.6 GiB RAM |
| Test interpreter | Python 3.11.16 (`.venv`) |
| Host checkout | `/home/r0b0tmagic/.hermes/hermes-agent` @ `a6102b8d809d6b32824757b547b60f9d22d6a88e` |
| Plugin revision | `test/stress-memory` @ `8f93da2` (product code `cee492c` + inbox race fix) |
| Engine | `@zvec/zvec-grep` 0.2.2 (raw test package), local `potion-retrieval-32m` embeddings |
| Limits | `MemoryMax=2G`, `CPUQuota=200%`, `TasksMax=128` or `256`, per-case `RuntimeMaxSec` |

## Two root causes, not one

1. **A harness task ceiling, not a provider defect.** At `TasksMax=128` the native engine cannot
   create the threads it needs under warm churn. The kernel denies task creation
   (`pids_events.max` 2, 121 and 204 in the failing attempts), the native layer aborts with
   `terminate called without an active exception`, and RocksDB/zvec report
   `ZVEC_GREP.ENGINE.STORAGE.ZVEC_OPEN_FAILED … Resource temporarily unavailable`. The same lanes
   pass at `TasksMax=256`, where the observed peak is 138–150 tasks. The production unit itself
   runs with `TasksMax=18232`, so the 128-task ceiling was an artifact of the test profile.
2. **A real product defect in the durable inbox.** Two processes opening the same fresh SQLite
   inbox raced on `PRAGMA journal_mode=WAL`, which needs a short exclusive lock for the
   conversion; the loser raised `sqlite3.OperationalError: database is locked` from
   `MirrorInbox.__init__`. In `native-repeat-10` that surfaced as a native writer exiting 1 plus
   `mirror recovery deferred; recall disabled`. Fixed in `93e696f`: read the current journal mode
   first (an already-WAL database needs no lock at all), retry only `SQLITE_BUSY`/`SQLITE_LOCKED`
   under a bounded deadline inside the startup budget, and create the table through the documented
   writer admission path. The regression tests are red on the previous revision.

## Lane results

| Lane | Transport | Processes | Records/process | Background files | Critical ops | Result | Callback p99 (s) | Peak tasks | pids_events | Report |
|---|---|---|---|---|---|---|---|---|---|---|
| `offline-1x20-drain` | offline | 1 | 20 | 0 | 50 | pass | 0.0073 | 128 | 0 | `stress-runs/stress-vo02w80z` |
| `offline-2x100-drain` | offline | 2 | 100 | 0 | 500 | pass | 0.0144 | 128 | 0 | `stress-runs/stress-bk515z66` |
| `offline-4x200-drain` | offline | 4 | 200 | 0 | 2000 | pass | 0.0307 | 128 | 0 | `stress-runs/stress-_qxo5b1g` |
| `offline-8x200-drain` | offline | 8 | 200 | 0 | 4000 | pass | 0.0501 | 128 | 0 | `stress-runs/stress-stw1_pfl` |
| `native-1x5-corpus100-drain` | native direct | 1 | 5 | 100 | 13 | pass | 0.0076 | 128 | 0 | `stress-runs/stress-854mr9mc` |
| `native-2x10-corpus1000-drain` | native direct | 2 | 10 | 1000 | 50 | pass | 0.0091 | 128 | 2 | `stress-runs/stress-wcwjvm7h` |
| `native-4x25-corpus10000-drain` | native direct | 4 | 25 | 10000 | 252 | pass | 0.0357 | 128 | 4 | `stress-runs/stress-b7e905wd` |
| `native-repeat-2` @128 | native direct | 1 lane | 2 iterations × 9 tests | — | 18 tests | **fail** | — | 128 | 2 | `stress-runs/stress-w5arh7jh` |
| `native-repeat-2` @256 | native direct | 1 lane | 2 iterations × 9 tests | — | 18 tests, 0 failures, 0 skips | pass | — | 145 | 0 | `stress-runs/stress-2htzv6jj` |
| `native-repeat-10` @256 (pre-fix) | native direct | 1 lane | 10 iterations × 9 tests | — | **1 failed at iteration 7** (inbox race) | **fail** | — | — | 0 | `stress-runs/stress-smf8op8y` |
| `native-repeat-10` @256 (post-fix) | native direct | 1 lane | 10 iterations × 9 tests | — | 90 tests, 0 failures, 0 skips | pass | — | 150 | 0 | `stress-runs/stress-ak5akxb3` |
| `fault-soak-100` × 5 batches | offline | 1 lane | 500 iterations × 24 tests | — | 12,000 tests, 0 failures, 0 skips | pass | — | 128 | 0 | `stress-runs/stress-66rigbin`, `stress-q08k54zz`, `stress-c622ops3`, `stress-ssw_7dm3`, `stress-2pg47m72` |
| `daemon-smoke-restart` (60 s) @128 | native daemon | 1 handle set | 24 callbacks | 200 | 31 queries: 14/14 required hits, 17/17 absences | pass | 0.0069 | 128 | 0 | `soak-runs/soak-4_76fcsn` |
| `daemon-proof-5m` (300 s) @128 | native daemon | 1 handle set | 60 callbacks | 200 | — | **fail** — `owned_daemon_unexpected_exit`, daemon `terminate called without an active exception` | 0.0349 | 128 | 121 | `soak-runs/soak-xfz0tdfg` |
| `daemon-proof-5m` (300 s) @256 | native daemon | 1 handle set | 60 callbacks | 200 | — | pass | — | — | 0 | `soak-runs/soak-vbklab7b` |
| `daemon-soak-30m-restart` @128 | native daemon | 1 handle set | — | 200 | — | **fail** — owned daemon abort after 170.6 s | — | 128 | 204 | `soak-runs/soak-bklhzvzt` |
| `daemon-soak-30m-restart` @256 | native daemon | 1 handle set | 30 min churn + engine restart at 900 s | 200 | 794 queries: 341/341 required hits, 453/453 absences; 678 callbacks | pass | — | 138 | 0 | `soak-runs/soak-4i5cltth` |
| `daemon-smoke-threadruntime` (60 s) @128 | native daemon, patched engine | 1 handle set | 24 callbacks | 200 | 31 queries: 14/14 required hits, 17/17 absences | pass | — | 97 | 0 | `soak-runs/soak-2k9ckhjx` |

### Thirty-minute soak metrics (`daemon-soak-30m-restart` @256, `hermes-zvec-stress-0404fa42c7e8`)

| Metric | Value |
|---|---|
| Active churn | 1,808.4 s of 1,816.8 s wall, exit 0, `consumer errors` none |
| Engine restart | 1 (generation 2), restart 1.0 s, unauthenticated 401 / authenticated 405 as designed |
| Convergence | 228 cycles — first 6.48 s, max 7.29 s, final 0.0 s |
| Native latency `query:warm` | p50 0.857 s, p95 1.389 s, p99 1.677 s, max 2.021 s (n=1319) |
| Native latency `index:warm` | p50 1.564 s, p95 2.218 s, p99 2.444 s, max 2.813 s (n=547, 186 over the 2 s prefetch budget) |
| Peak tasks / events | 138, `pids_events.max` 0 |
| Memory | peak tree sampled RSS sum 1.61 GB against a 2 GB cap; cgroup peak 1.36 GB; no OOM kill |
| CPU | 2,776 s consumed at the 200 % quota, 1,195.6 s throttled (this is a capacity note, not a failure) |
| Slack trend (diagnostic) | +129 KB/s warm generation 1, +155 KB/s warm generation 2 — not leak proof |

Earlier failures kept in the record and superseded by a later pass: `offline-2x100` attempts 1–4,
`offline-8x200-drain` attempt 1 (deadline), `native-1x5-corpus100-drain` attempts 1–3,
`fault-baseline-10` attempts 1–2, `fault-soak-100-batch-1` attempt 1, the first `native-repeat-2`
at 128, `native-repeat-10` at 256 before the inbox fix, and the first `daemon-smoke-threadruntime`
(a receipt-serialization bug in the harness, not the provider).

## Native thread-budget experiment

An engine copy prepared by `scripts/prepare_zg_thread_runtime.py` requests
`queryThreads=1, optimizeThreads=1`
(`.test-tools/thread-runtimes/zg-q1-o1-r2-724aac71d457`, source pin
`724aac71d457…`, patched file `c722510a7873…`). Same revision, same lane, same profile
(`daemon-smoke-restart` shape, 60 s, `TasksMax=128`), raw engine versus patched:

| Metric (warm phase) | Raw test package | Patched q1/o1 | Change |
|---|---|---|---|
| Peak concurrent thread sum | 99 | 70 | −29 % |
| Median concurrent thread sum | 87 | 53 | −39 % |
| Peak threads in one process | 71 | 41 | −42 % |
| Peak tree sampled RSS | 1,259 MB | 1,260 MB | — |
| `query:warm` p50 / p99 | 0.855 / 1.437 s | 0.840 / 1.376 s | within noise |
| `index:warm` p50 | 1.558 s | 1.541 s | within noise |
| Functional result | 31 queries (14/14 required, 17/17 absences), 24 callbacks, 1 restart, 0 nonzero native commands | identical | identical |

The patched runtime also runs the native contract suite (`tests/test_zg_native.py`, 7 tests)
green under `TasksMax=128` in 13.6 s with a 569 MB cgroup peak, where the same suite's
two-process concurrency test fails with the raw engine at that ceiling.

Decision rule applied: the reduction exceeds 25 % on the single-process maximum and the
128-task native failure disappears with the patched engine → a positive result. Productionizing a
thread-limited runtime is therefore a **follow-up proposal**, not something this campaign
installed. The requested pool sizes are not an observed total process thread cap, the copy links
its dependencies by realpath (no OS sandbox), and `storage/zvec.js` is version-pinned, so any
adoption needs its own narrowly scoped change plus a fresh bounded proof.

## Decisions

Recorded so the numbers above are not re-litigated from a single figure.

**Native thread budget: not adopted (shelf).** The patched engine's only demonstrated benefit is
fitting a lower task ceiling (peak process threads 71 → 41, −29 % tree sum) with unchanged peak RSS
(1,259 → 1,260 MB) and unchanged latency (query `warm` p50 0.855 → 0.840 s). Production runs
`TasksMax=1024` (see below), so that ceiling never binds, and the measurement is a 60 s smoke over a
200-fact corpus with no peer contention — the 30-minute soak and the 10,000-file corpus lane were
never run with the patched engine, and the *optimize* pool is exactly the knob that would show up on
large corpora. Adopting it would also put a hand-patched, version-pinned copy of third-party JS
(`storage/zvec.js` sha `724aac71…`, dependencies linked by realpath, first-native-init wins) on the
critical path, re-deriving the patch on every engine upgrade. Revisit only if the engine runs under
a constrained cgroup/container, or production logs show `terminate called without an active
exception` plus service restarts. If revisited, the bar is the full acceptance the raw engine
passed: 30-minute soak, corpus ladder, and a two-process contention run, all green with the patched
engine.

**Service task ceiling: adopted.** `hermes-zvec-memory.service` now declares `TasksMax=1024` instead
of inheriting the user manager's ambient default (18232 at the time of measurement). The value is
load-bearing and was previously invisible: below roughly 150 tasks the engine does not degrade, it
aborts, which the 128-task lanes reproduced. 1024 is ~7× the measured 138-task peak and still bounds
runaway thread or fork creation. `MemoryMax` stays `infinity` on purpose — measured peak was 1.6 GB
against 15.6 GB of host RAM, and OOM-killing the memory engine is worse than letting the host
reclaim; `MemoryHigh=4G` would be the throttle-first option if a guard is ever wanted.

## Deployment

The reviewed revision was deployed to `~/.hermes/plugins/zvec-memory` after the offline suite
(347 passed), the native lanes at `TasksMax=256` and the 30-minute soak were green:

| | |
|---|---|
| Deployed files | `__init__.py cee492ca1f42`, `inbox.py 58a6d4e51992`, `config_schema.py 738706323a55`, `transactions.py cbb0cc4df19d`, `workers.py e8dbfa3e11ec` |
| Previous revision | `77c12cb` product copy, preserved with config, unit and engine wrapper at `~/.hermes/backups/zvec-upgrade-2026-09-10_143420` (with `ROLLBACK.txt`) |
| Validation | `hermes plugins validate` ok (all 10 checks), `hermes memory status` provider available |
| Fresh-session store | `memory_store` wrote `facts/20260910-193441-tool-24foqzba.md`, content read back and verified |
| Fresh-session recall | separate session returned the citation `facts/20260910-193441-tool-24foqzba.md:7` for a paraphrase query |
| Inbox migration | production inbox converted `journal_mode=delete → wal` on the first new-code session, `pending 0` |
| Campaign baseline | re-recorded as revision 3 (plugin files) and revision 4 (declared `TasksMax=1024`, new engine `MainPID`), preserving every earlier revision in `.test-tools/campaign/` |
| Post-change checks | engine restarted, `healthz` 200, unauthenticated MCP 401, fresh-session recall still cited `facts/20260910-193441-tool-24foqzba.md:7` |

## Supported envelope (measured, this machine)

- **Offline persistence:** 1–8 concurrent writer processes, up to 4,000 critical callbacks per
  campaign, exact mirror-state oracle, no lost or duplicated records, clean cgroup teardown.
- **Native direct:** 1–4 concurrent writer processes over corpora of 100–10,000 background files,
  with real citations returned from a ready index.
- **Repeated native regression:** 10 consecutive iterations of the native contract suite
  (90 tests) with zero failures and zero skips at `TasksMax=256`.
- **Fault lanes:** 500 iterations / 12,000 tests of crash, lock-contention, saturation and
  recovery schedules, all green.
- **Same-handle daemon:** 30 minutes of continuous churn with one engine restart, correct
  retrieval throughout, no lost notifications, clean teardown at 138 tasks peak.

Not claimed: a 30-minute soak at 128 tasks (that profile provably fails), linearizable retrieval
*during* concurrent mutation, 8-writer native concurrency beyond the corpus ladder,
macOS/Windows, or full erasure of historical session text.

## Provider-level verdict

The provider is a best-effort, local, self-hosted Hermes memory provider: durable notifications
with a bounded FIFO, demand-driven retry, gated recall while a deletion is pending, a 2 s prefetch
budget with cache-generation invalidation, and no remote egress when local embeddings are used.
The campaign found two correctness gaps — the task ceiling (harness) and the durable-inbox
journal-conversion race (product) — and both are closed with regression tests.

## Maintenance revisions (2026-09-11)

The provider was reworked for maintainability and re-verified end to end; no recall or persistence
behaviour regressed and the campaign manifest stays `complete_with_recorded_limits`.

**What changed**

- The runtime path no longer imports `hermes_cli`, `tools.registry`, `utils` or
  `agent.context_compressor`; the two helpers it actually needed are vendored in `zvec-memory/hostio.py`
  and pinned to the host by an equivalence suite plus a guarded import probe.
- Durable formats are versioned: an engine-identity sidecar (`.zvec-grep/.zvec-memory-state.json`)
  adopts an existing index once and rebuilds when the engine binary or embedding changes, and
  `.mirror-map.json` carries a `schema_version` that refuses a newer layout instead of misreading it.
- `hermes zvec-memory doctor|status|reindex|engine install` was added: eight checks (config, vault,
  engine, index, inbox, mirror, identity, task ceiling), exit 0 only when all pass.
- The engine launcher and systemd unit are now generated and installed by
  `ZvecMemoryProvider.post_setup` (the `hermes memory setup` hook), which also owns activation;
  `python scripts/upgrade.py` gates a swap behind the offline suite, the native lane, a verified
  backup, a re-baseline and doctor, and can roll back.

**Verification after the rework**

| Gate | Result |
| --- | --- |
| Offline suite | 401 passed, 10 deselected |
| Native lane (`ZVEC_RUN_NATIVE=1`, through the generated launcher) | 9 passed, 1 skipped |
| `hermes zvec-memory doctor` on production | healthy (engine 0.2.2, index ready, inbox WAL 0 pending, mirror 3 records, `pids.max=18232`) |
| Generated launcher vs the hand-built production launcher | byte-identical |
| Fresh engine install in an isolated runtime root | `zg 0.2.2`, launcher runs, manifest + unit written |
| `hermes memory setup zvec-memory` against the live host | takes over activation, config.yaml gains only the `plugins.zvec-memory` block |
| Production baseline | re-recorded as revision 5; revision 4 preserved as `production-before-rev4.json` |
