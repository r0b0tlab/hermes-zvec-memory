# Provider review and execution evidence

## Source baseline

- Plugin base: `85040fabda96b618344ad4e0cd5df4449902542f`.
- Installed Hermes examined: `a6102b8d809d6b32824757b547b60f9d22d6a88e`.
- Upstream Hermes compatibility target: `cfdbbb6e35010ace89fbe8243ee82fa4de143e10`.
- Upstream checkout used only for tests under `.test-tools/hermes-upstream`; the running Hermes checkout is not modified.

## Baseline reproduced

Original test suite, without zg on PATH: **6 passed, 1 failed, 1 skipped**.
The failing cache test patched the subprocess boundary but not availability;
`queue_prefetch` correctly declined to run without an installed zg. This is a
test-fixture defect, not a reason to remove the provider's availability gate.

The installed Hermes interpreter lacked pytest and PyYAML. A separate `.venv`
was created in this repository with Python 3.11.16; test dependencies are not
installed into the running Hermes interpreter or system Python.

## Native engine validation

Tested npm package: `@zvec/zvec-grep@0.2.2`, requiring Node >=22.
Registry integrity at installation:
`sha512-6xsF21zgUh98W3BmH7+Nz/Esdx4Ps3ibs+DHluh/uo2O45xCMeM+pkmdRlcagkONHRO2pM2KiGgKHVmRGKxBtA==`.

Installed under `.test-tools/`, not globally. The first npm installation tried
to build sharp against the host's global libvips and failed for missing node-gyp;
`SHARP_IGNORE_GLOBAL_LIBVIPS=1` selected the packaged binary successfully.

Synthetic-vault contract suite:

```sh
ZVEC_RUN_NATIVE=1 \
ZVEC_TEST_BIN="$PWD/.test-tools/node_modules/.bin/zg" \
ZVEC_TEST_MODEL_CACHE="$PWD/.test-tools/models" \
.venv/bin/python -m pytest tests/test_zg_native.py -q \
  --basetemp="$PWD/.test-tools/pytest-native"
```

Observed: **7 passed in 15.40s**. These are engine contract tests, not yet proof
that the hardened provider's full lifecycle passes native integration.

Validated:

- Hybrid, FTS, and vector retrieval with a real local potion model.
- Unicode search and fact globs.
- Explicit `--hybrid=--allow-remote` treats option-like input as query data.
- Markdown-only indexing excludes a JSON canary.
- A removed Markdown fact disappears from FTS after a successful index refresh.
- Concurrent native index processes retain a healthy index but may reject one
  caller with `ZVEC_GREP.ENGINE.LOCK.BUSY` or
  `ZVEC_GREP.ENGINE.DAEMON_LEASE_ACTIVE`. The latter can say pid 0 without a
  deliberately started daemon. These errors require caller-side retry; they
  are not evidence that native operations queue themselves.

No shared zg daemon was started and no remote embedding grant was issued.
Native tests use a temporary HOME and synthetic facts. Downloaded local model
files are confined to the project test cache.

## Integration findings guiding implementation

The declared Hermes UI uses `<HERMES_HOME>/zvec-memory/config.json`; the original
plugin read only `plugins.zvec-memory` in YAML. Backup loads a provider without
initialize, so a custom vault must be resolvable without starting threads.
Successful built-in memory replacements/removals carry `metadata.old_text` in
current Hermes. FIFO manager callbacks do not order extra plugin threads unless
the plugin preserves that ordering itself.

Hermes warms recall using the just-completed user query. A benchmark that reuses
that identical query establishes cache-hit latency, not general next-turn speedup.
Direct zg queries with `--refresh background` warn and fall back to no refresh;
a daemon-free provider must not rely on that flag for write visibility.

## Provider and host lifecycle validation

The real loader → manager → provider → zg test passed on both installed Hermes
and the pinned upstream checkout, using synthetic facts. It covers store/search,
prefetch, originating session labels, mirrored replacement/removal, drained
shutdown, and cold backup discovery. The standalone declared-config/host suite
also passes all nine cases on both targets.

A dedicated authenticated loopback engine was prepared for the default profile.
`zg server status --check-ready` reports ready; systemd reports active/running;
an unauthenticated HTTP request to its MCP endpoint returns 401. Before
activation, the same native provider lifecycle
passed through this daemon. Ten warm repeated native queries (without the Python
provider cache) measured p50 0.404 seconds and maximum 0.412 seconds; this is a
small synthetic-workspace measurement, not a general next-turn latency guarantee.

The same corrected benchmark against baseline and hardening code retained 10/10
near-wording and 8/10 paraphrase hybrid/visible hits. Near-query p50 was 0.947s
baseline versus 1.059s hardening in these sequential direct-mode runs. No general
speedup is claimed. Raw receipts are under ignored `benchmark-results/`.

## Independent review gate

The independent review rejected deployment despite green tests: separate
processes could overwrite mirror-map transactions; a saturated FIFO could drop
critical removal notifications; exhausted native-lock retries could leave normal
recall disabled until another explicit trigger. Separate fixers added
process-level transaction serialization, durable critical notification delivery,
and bounded retry from later prefetch calls; subsequent reviews additionally
closed deferred-startup and shared-recovery edge cases before activation.

The query-echo benchmark pitfall is separately fixed and regression-tested:
quality checks examine retrieved hits rather than the echoed query heading.
None of the current benchmark expected strings was present in its query, so
this correction does not retroactively change the measured hit counts.

## Review completion and deployment

Independent review approved runtime SHA
`77c12cb4551bdfc4213f3fc6b64d0c7dc43e8907` after the reproduced ownership,
delivery, shared-recovery, and contended-startup bugs were fixed. A reported
intermittent deletion test was traced to a mock claiming successful indexing
while returning stale hardcoded text; its command-aware mock is corrected.

Final combined suite: **137 passed in 65.11s**, no skips, including native zg.
Pinned-upstream host contracts plus native daemon/multi-process lifecycle:
**11 passed in 19.10s**. Fresh isolated pinned dependencies also reproduced the
host/benchmark tests. Hermes's own `plugins validate --json` returned ok with
no warnings. Runtime code deployed matches the reviewed source hashes.

Final direct-mode benchmark at `04038cd46256580113d51875f29ffb31e48aca6c`:
10/10 near and 8/10 paraphrase hybrid/visible hits; maximum context 1089 chars.
Near p50/p95 were 0.924/0.937 seconds; paraphrase p50/p95 0.927/0.932 seconds.
The later reviewed change only reopens a deferred inbox before notifications;
it does not alter retrieval. Timing variation between sequential runs is not a
controlled claim of general speedup.

Default-profile integration is active and verified:

- `memory.provider = zvec-memory`; built-in memory/user-profile injection stay enabled.
- Dedicated zg 0.2.2 runtime outside the test checkout, with local potion embeddings.
- User service `hermes-zvec-memory.service` is enabled and active, listening only
  at `127.0.0.1:17999`, with a private bearer-token file. Unauthenticated MCP
  requests return 401. No remote embedding grant or remote credential is configured.
- A fresh Hermes CLI session `20260910_080020_c466ae` called memory_store to save
  a truthful integration fact. Its returned file was read back and verified.
- Separate fresh session `20260910_080051_bdb859` called memory_search and cited
  that fact at `facts/20260910-130025-tool-sfzq6_m8.md:7`.
- Native status reported a ready index, no queued/failed work, and facts/sessions
  Markdown-only roots.
- Built-in MEMORY.md hash is unchanged from backup. Comparing parsed host
  configuration before/after, every setting except memory.provider is unchanged.
- Existing sessions are not hot-swapped; new sessions load the provider. Other
  profiles were not modified. Historical transcripts and pre-existing built-in
  facts were not bulk-imported; built-in injection continues to supply them.

Rollback is documented in the private installation backup. Use the supported
CLI to unset memory.provider and disable the dedicated user service, then start
a fresh session. Preserve the vault for diagnosis; do not independently delete
inbox/map components or claim full erasure of historical logs/backups.

## Remaining limits

Linux is tested; macOS is not exercised; Windows is unsupported by the POSIX
transaction layer. General turn logging is bounded/best-effort; critical mirror
notifications are durable. Retry is demand-driven with cooldown, not a periodic
timer. Snapshot consistency requires quiescing writers; the provider remains a
best-effort v1 compression hook, not a v2 durable transcript checkpoint.

The changes are local commits on `fix/provider-contract`. No remote push or CI
run is claimed. The integration is complete for the default local profile.
