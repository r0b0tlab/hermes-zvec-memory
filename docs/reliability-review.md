# Zvec-memory reliability review

Review date: 2026-09-27. Tested implementation: `f7f25f38d3a0f343b621199faafa9461b7829280`.

## Verdict and first fixes

Keep the design small. The existing SQLite inbox, bounded workers, local engine, source-aware caches and thin host integration provide a useful foundation. Fix the demonstrated correctness and maintenance hazards before rollout. This is a completed source review with **explicitly incomplete campaign dimensions**, not a green release or capacity certification. No product fix, deployment, service reload/restart, merge, push or catalog submission occurred.

Recommended order:

1. **P0 maintenance safety (PKG-1/3/5):** stop default-profile writes and real writes from `--dry-run --rollback`. Isolate verification before imports; never pass the installed production wrapper into synthetic tests. Prefer host-managed updates over building a second transactional upgrader.
2. **P1 memory identity (R1):** follow the real host's authoritative `previous_content`. Replacements and removals committed in the native store while the mirror retained the retired entry in an exact-match-versus-substring case.
3. **Index obligations (R2/3/7):** carry actual rebuild arguments through coalescing, retain maintenance markers until success, and clear the running latch on bookkeeping failure. The argv/early-ack/latch defects are reproduced; a completed native model-swap outcome is not claimed.
4. **Conditional containment (R4/5):** reject in-vault symlink redirection of owned deletions and revalidate ordinary writer destinations. Both owned-fixture cases reproduce. No remote exploit or normal creation of these links is assumed.
5. **Configuration and health (INT-01/02/03/04):** make empty JSON authoritative everywhere, preserve effective legacy settings, finish private-launcher setup wiring, and stop calling corrupt/unknown state healthy. The empty-JSON and bad-journal cases reproduce; other setup paths are source-reviewed.
6. **Evidence and packaging (H01–H09, PKG-2/7/8):** support non-Git installed-host provenance, require citations and observed samples behind green results, retain effective kernel limits, remove ignored prerequisite dependencies and preserve the shipped license notice. Use small local changes, not a new service/database/scheduler.

The full cards below include exact source locations, status, minimal change, regression target, complexity cost, alternatives and compatibility. There are **34 overlapping reviewer observations**, not that many independently reproduced bugs. A separate service-definition hold remains unresolved.

## Executed evidence

Four independent domain reviews cover all **51 tracked files**. The following **19 directory-backed workload attempts** include failures and separate controls. Guard-only preflight stops are recorded separately. Do not sum repeats into unique test coverage.

- Untouched offline baseline: **397 passed, 10 failed, 1 skipped**. Six failures come from Git-only host provenance, two from absent ignored case data, one from an absent ignored upgrade helper, and one from unsafe ambient-home rollback. Original failures remain preserved.
- Original real-host suites reported nine passes each, but the managed native fake omitted `--version` and the host swallowed initialization failure. Corrected review-only fixtures verify initialization and repeat all nine unchanged contracts on **both** hosts. These are real-host/fake-native checks, not engine-readiness proof.
- Combined installed-host follow-ups: **10 passed, 9 failed**: the nine corrected host contracts and kernel-limit check pass; the targeted reliability assertions fail. Three additional measurement/process-boundary probes fail with their intended assertions/UnicodeDecodeError. The earlier five contract probes remain separate, including weaker superseded identity probes.
- Current-host raw-native integration: **9 passed, no skips**, 43.93 JUnit seconds, 638.14 MiB peak cgroup memory, 141 peak tasks, no task-denial/OOM events.
- Reference-host native load control: **50/50 callbacks**, 32.34s including recovery, admission p95 **1.484 ms**, end-to-end **1.546 callbacks/s**. Admission latency is neither sustained throughput nor shutdown duration.
- Installed-host daemon worker: **301.79s active**, 18 cycles, 108 callbacks, 129 query samples and one restart. Worker exit 0, zero worker errors, corpus removed, zero final mirror records. Peak 1281.92 MiB/132 tasks, no task denials/OOM. **Overall coordinator exit 1 remains failed**: it demanded a Git SHA from the installed snapshot after successful worker completion.

The daemon-proof cgroup also recorded `cpu.stat throttled_usec=215922688` (**215.923 seconds** of cumulative quota-throttling accounting) with `cpu.max=200000 100000` / `CPUQuota=200%`; coordinator elapsed time was **311.08 seconds**. This counter is captured through ExecStopPost, including helper overhead. It is not application latency or a wall-clock stall percentage, and it does not establish how much faster the workload would run without the quota. The exact counter is retained as `daemon-proof-installed-8XxuvD.cpu_throttled_usec` in `evidence.json` and in the raw kernel receipt.

Later receipts read effective `memory.max=2147483648`, `pids.max=256`, `cpu.max=200000 100000`: 2 GiB and two CPU quota units, not hardware-core count. Counters include ExecStopPost helper overhead. Older missing fields remain missing; old test counts do not establish requested effective limits. Every executed unit has matching before/after watched-production snapshots and verified final cgroup cleanup.

| Attempt | Host | Outcome | Coverage |
|---|---|---|---|
| `contract-edges-UxzsuG` | installed | failed | 0 pass / 5 fail / 0 error / 0 skip |
| `corpus-100-installed-rJV3Ww` | installed | blocked_host_provenance | callbacks 0/13 |
| `daemon-proof-installed-8XxuvD` | installed | failed_coordinator_worker_passed | worker pass=True; 301.79s active; 1 restart |
| `faults-installed-ck5GlU` | installed | blocked_host_provenance | callbacks 0/0 |
| `followups-complete-LRYoNq` | installed | failed | 10 pass / 9 fail / 0 error / 0 skip |
| `followups-installed-controlled-8eGFeF` | installed | failed | 1 pass / 4 fail / 0 error / 0 skip |
| `followups-installed-red-V3qPHJ` | installed | failed | 0 pass / 5 fail / 0 error / 0 skip |
| `host-installed-a5UQSA` | installed | passed | 9 pass / 0 fail / 0 error / 0 skip |
| `host-reference-FaBhx1` | reference_control | passed | 9 pass / 0 fail / 0 error / 0 skip |
| `host-reference-corrected-lpPcuZ` | reference_control | passed | 9 pass / 0 fail / 0 error / 0 skip |
| `instrument-oracles-RfC6eV` | installed | failed | 0 pass / 3 fail / 0 error / 0 skip |
| `native-CrTmpq` | installed | passed | 9 pass / 0 fail / 0 error / 0 skip |
| `native-confirmed-limits-ZlDsdc` | installed | passed | 9 pass / 0 fail / 0 error / 0 skip |
| `native-load-git-host-control-hfDCF5` | reference_control | passed | callbacks 50/50 |
| `native-load-installed-kGo80M` | installed | blocked_host_provenance | callbacks 0/50 |
| `offline-btTOjx` | installed | failed | 397 pass / 10 fail / 0 error / 1 skip |
| `offline-git-host-control-bCNVZC` | reference_control | passed | 78 pass / 0 fail / 0 error / 0 skip |
| `recall-quality-git-host-control-LUo4Wy` | reference_control | passed | sets: near, paraphrase |
| `recall-quality-installed-nZMY99` | installed | blocked_host_provenance | sets: none; no quality samples |

## Recall and cache observations

The installed-host recall run blocked before samples; its numeric threshold-failure labels mean missing coverage, not measured poor recall. The separate reference-host control used **20 synthetic facts** and ten queries per set. Parent rescoring checked **60 retained responses / 300 ranked citations** against exact numbered fixture-source lines, excluding query and heading metadata. No discrepancy or invalid source line remained, despite the separately reproduced generic parser weakness.

| Set | Hybrid hit@5 | FTS hit@5 | Visible hit@5 | Largest visible context |
|---|---:|---:|---:|---:|
| paraphrase | 8/10 | 3/10 | 8/10 | 1089 chars |
| near | 10/10 | 10/10 | 10/10 | 1061 chars |

The control meets its near-query 0.8/0.8 and 2,000-character gates. It is not installed-host quality certification or a representative/larger-corpus benchmark. On its already-running handle, an uncached prefetch took 0.975655s, an identical cached call 0.000251s, and distinct next-turn queries p50/p95 0.969214/0.982659s. These are different operations, not a before/after optimization or proof of predictive warming.

Measured toolchain: Python 3.11.16, Node v26.8.1, raw zvec-grep 0.2.2, Linux. The reference-host control revision is `758ad514eb0e800547e015edf05aa18f78b78d82`. Installed source identity/fingerprints are recorded separately, never inferred from installer directory names. No model or thread-pool change was tested.

## Explicit limits and operational hold

- Installed-host ten-iteration faults and the 100-fact scaling rung block before workload on host provenance. The required predecessor failures prevent 8×200 churn and 1,000/10,000-fact scaling under the plan's stop rules. These dimensions are incomplete; the successful 1×20 reference control is not a substitute.
- The optional 30-minute soak was not approved or run. A completed five-minute worker does not establish long-lived leak freedom.
- Native model-change before/after identity, power-loss durability, online-backup restoration, and output-flood/descendant-pipe/cancellation adversarial lanes remain unverified. Invalid UTF-8 at the actual provider runner was exercised; broader supervision changes still require their own evidence.
- The on-disk user service is the pre-existing two-line `/usr/bin/true` stub, while systemd reports active/running, NeedDaemonReload=yes and TasksMax=1024. Cause is unknown; no attribution to a past test is made. Separate diagnosis is required before deployment. A daemon-reload is not itself a restart; do not blindly adopt the disk stub.

## Preserve simplicity and boundaries

Keep SQLite WAL/FULL, bounded admission/workers, source-aware cache invalidation, pending-deletion gates, demand-driven recovery, static reference-only prompt framing, cold schema/CLI loading and explicit installation consent. Existing regression/native paths support these mechanisms, but the findings below rule out blanket correctness claims. Avoid broad rewrites, new services/databases/schedulers, automatic retention/compaction, unmeasured thread-pool patches or an embedding change.

Tests used disposable roots before imports and raw copied native assets, not production wrappers/tokens; no package/model download was performed. The daemon used a private user/network namespace. The original code checkout remained clean. Approved baseline revisions preserve the initial snapshot: revision 2 records the approved onboarding-only change; revision 3 explicitly records the separately authorized `compression` configuration change and the added `onboarding.seen.busy_input_prompt` flag. The watched production state at the end of the measured campaign equals revision 3, **not** the superseded initial config hash. Private probes, corrected ignored limit-capture helper and generated receipts are review instruments, not product fixes or committed implementation.

Private companions: `findings.md`, `coverage.md`, `offline-classification.json`, `parent-dispositions.json`, `private-receipt-manifest.json`, `public/evidence.json`, `public/attempts.csv`, `public/quality-rescore.json`, the rescore script, snapshots and `runs/`. Sanitized artifact copies accompany this document; raw machine paths, tokens and user data are excluded. Guard-only stops before baseline revisions 2 and 3 are preserved separately from the workload attempt count.

## Source-file disposition

Each base-revision file has a domain-review ledger with exact sections/symbols. This table establishes source-review coverage, not exhaustive executed branch coverage.

| File | Domain review(s) |
|---|---|
| `.gitignore` | packaging |
| `CHANGELOG.md` | packaging |
| `LICENSE` | packaging |
| `README.md` | integration, packaging |
| `docs/catalog-submission.md` | packaging |
| `docs/maintenance.md` | packaging |
| `docs/review-results.md` | packaging |
| `docs/stress-results.md` | packaging |
| `docs/stress-testing.md` | packaging |
| `plugin-catalog-entry.yaml` | packaging |
| `pytest.ini` | packaging |
| `requirements-test.txt` | packaging |
| `scripts/measure_recall.py` | harness, packaging |
| `scripts/prepare_zg_thread_runtime.py` | harness, packaging |
| `scripts/report_stress.py` | harness, packaging |
| `scripts/run_campaign.py` | harness, packaging |
| `scripts/soak_memory.py` | harness, packaging |
| `scripts/stress_memory.py` | harness, packaging |
| `scripts/upgrade.py` | packaging, runtime |
| `site/index.html` | packaging |
| `tests/conftest.py` | integration, packaging, runtime |
| `tests/test_cli.py` | integration, packaging |
| `tests/test_durable_recovery.py` | packaging |
| `tests/test_engine.py` | integration, packaging |
| `tests/test_host_contract.py` | integration, packaging |
| `tests/test_hostio_equivalence.py` | integration, packaging |
| `tests/test_inbox_contention.py` | packaging |
| `tests/test_measure_recall.py` | harness, packaging |
| `tests/test_mirror_queue.py` | packaging |
| `tests/test_mirror_validation.py` | packaging, runtime |
| `tests/test_process_safety.py` | packaging |
| `tests/test_provider.py` | integration, packaging, runtime |
| `tests/test_provider_native.py` | packaging, runtime |
| `tests/test_report_stress.py` | harness, packaging |
| `tests/test_run_campaign.py` | harness, packaging |
| `tests/test_soak_memory.py` | harness, packaging |
| `tests/test_stress_memory.py` | harness, packaging |
| `tests/test_upgrade.py` | packaging |
| `tests/test_workers.py` | packaging, runtime |
| `tests/test_zg_native.py` | packaging |
| `tests/test_zg_thread_runtime.py` | harness, packaging |
| `zvec-memory/README.md` | packaging |
| `zvec-memory/__init__.py` | integration, packaging, runtime |
| `zvec-memory/cli.py` | integration, packaging, runtime |
| `zvec-memory/config_schema.py` | integration, packaging |
| `zvec-memory/engine.py` | integration, packaging, runtime |
| `zvec-memory/hostio.py` | integration, packaging |
| `zvec-memory/inbox.py` | integration, packaging, runtime |
| `zvec-memory/plugin.yaml` | integration, packaging |
| `zvec-memory/transactions.py` | packaging |
| `zvec-memory/workers.py` | packaging |

---

# Detailed finding cards

Reviewed implementation: `f7f25f38d3a0f343b621199faafa9461b7829280`.

These are 34 reviewer observations, with overlaps explicitly cross-referenced; they are not 34 independently reproduced bugs. Parent status and evidence below supersede the earlier source-review tense in the original rationale. A confirmed subclaim does not prove every hypothetical variant in its card. No proposed product fix was implemented or deployed.

## Evidence and reproduction conventions

P denotes the reviewed plugin source. H denotes the real installed Hermes snapshot; R denotes the separately pinned reference checkout. N denotes the local raw pinned zvec-grep package. Exact private roots, versions, fingerprints and receipts remain in `provenance.json`, `private-receipt-manifest.json`, and `runs/` in the evidence directory. Installer directory components are not treated as Git SHAs.

All reproduction commands require the prepared private review fixture and its one-off probes; this document is not a self-contained public harness. Source `review-env.sh` from that directory first, then use `REVIEW_TASKS=256` and a bounded `REVIEW_SECONDS`. The wrapper constructs disposable HOME/HERMES_HOME/XDG roots before imports, denies external networking, uses raw copied test binaries/models, records production fingerprints, and verifies exact owned-cgroup cleanup. Never replace it with bare pytest or the installed production launcher. `probes/` means the private evidence directory's probes, not a shipped source directory.

An observed failed assertion is retained as RED evidence, not silently fixed. Future test commands are coverage targets after adding the stated regression oracle; they do not claim a fix or new test already exists. Native future tests additionally require `REVIEW_NATIVE=1` and the same isolated raw runtime.

## R1 — P1: committed old-entry identity is ignored

Status: confirmed
Severity: P1
Recommendation: fix next

Parent reproduction command: `run_review identity pytest -q "$OUT/probes/test_review_followups.py" -k real_host_exact_selection`
Observed result/receipt: followups-complete-LRYoNq: both real-host exact-selection probes fail after the native store commits and the actual manager forwards previous_content.

Exact future regression command: `REVIEW_TASKS=256 REVIEW_NATIVE=1 run_review future-r1 pytest -q "$WT/tests/test_host_contract.py" "$WT/tests/test_provider.py" "$WT/tests/test_provider_native.py"`
Compatibility/recovery: Authoritative previous_content must never fall back to a different selector match. Specify conservative legacy behavior for older hosts without this metadata.
Alternative / keep-versus-change decision: Keeping substring selection is rejected: it can retain retired facts after a successful user-visible mutation.

### Source rationale, invariant, blind spot, minimal change, regression oracle and cost

**Location/mechanism:** P `zvec-memory/__init__.py:626–674`, `_apply_mirror`, especially `638–645`, selects records using only `old_text in record['content']`. It ignores `previous_content`. A zero/multiple-match no-op still advances the notification watermark; `_drain_mirror_inbox:528–535` then acknowledges delivery.

**Actual contract:** H `tools/memory_tool_store.py:298–361,364–461` resolves and returns the full committed old entry, including batch operations. H `agent/memory_manager.py:804–838` deliberately derives `previous_content` from that result, not from the selector/provenance. H `agent/inline_tool_executors.py:127–169` emits after the tool call. `old_text` is selector text, not authoritative identity.

**Impact:** Native exact-match mutation of `I prefer tea` can succeed alongside `I prefer tea with milk`, while the mirror sees substring ambiguity and retains the retired entry. A partial mirror can instead mutate the wrong owned entry.

**Blind spot:** P `tests/test_provider.py:428–445,739–749` and `tests/test_provider_native.py:75–137` use selector-only callbacks, not the installed host's committed identity.

**Smallest change:** Prefer exact `(target, previous_content)` ownership when supplied, with no fallback to another selector match; retain a separately specified conservative legacy path. Match the host's committed outer-whitespace normalization for newly mirrored content, rather than rewriting selector text.

**Regression oracle:** Feed real H single/batch results through `notify_memory_tool_write`; cover complete/partial mirrors, exact/substring collisions, wrong targets and whitespace. Only the selected owned old entry may disappear; explicit/unrelated files remain unchanged; delivery, refresh gating and eventual recall converge.

**Cost:** No new dependencies, services, persistent state or format. Small mutation-path matching change; no ordinary-recall work.


## R2 — P1: identity “rebuild” dispatch is an ordinary index invocation

Status: confirmed
Severity: P1
Recommendation: fix next

Parent reproduction command: `run_review rebuild pytest -q "$OUT/probes/test_review_followups.py" -k engine_identity`
Observed result/receipt: followups-complete-LRYoNq: identity change schedules an index invocation without --rebuild. The native index's resulting identity was not asserted; that stronger claim remains unverified.

Exact future regression command: `REVIEW_TASKS=256 REVIEW_NATIVE=1 run_review future-r2 pytest -q "$WT/tests/test_provider.py" "$WT/tests/test_provider_native.py"`
Compatibility/recovery: Preserve first-use/legacy adoption but retain an explicit rebuild obligation for known incompatible identity, including the selected embedding.
Alternative / keep-versus-change decision: Relabelling provider metadata after an ordinary index is rejected; no model swap or engine fork is proposed.

### Source rationale, invariant, blind spot, minimal change, regression oracle and cost

**Location/mechanism:** P `zvec-memory/__init__.py:308–325` (`_ensure_engine_identity`) requests `_maybe_reindex(force=True)`. At `853–890`, `force` bypasses debounce but adds neither `--rebuild` nor the changed embedding. `_build_index:828–830` supplies embedding only for a missing manifest. Success records the current configured identity at `917–919`; known mismatch does not itself close recall.

**Dependency support:** N `dist/cli/args.js:246–275` distinguishes rebuild/reset-paths/embedding; `commands.js:281–326,709–718` forwards them separately; `auth.js:149–168` requires rebuild for a model change and can resolve an omitted model from existing index state.

**Impact:** The plugin can relabel its sidecar as current after an incremental operation while native storage remains on the previous identity. This is a source-level inconsistency, not an observed native mismatch.

**Blind spot:** P `tests/test_provider.py:934–975` checks mocked `force=True`, not final argv/native metadata. Initial-build retry coverage at `254–276` does not cover existing-index transitions.

**Smallest change:** Preserve a real rebuild obligation through coalescing/retry; send actual rebuild/effective embedding arguments, acknowledge identity only after success, and gate known-incompatible recall. Preserve intentional unknown-version/legacy adoption behavior unless native evidence contradicts it.

**Regression oracle:** Start with a genuine existing identity, change it, observe provider-generated argv and native metadata. Failed rebuild must not relabel the old index or lose/downgrade the obligation.

**Cost:** No new dependency/service/persistent format required; small explicit in-memory transition using existing request machinery. Native rebuild expense occurs on transitions, not ordinary queries.


## R3 — P2: maintenance request is removed before successful completion

Status: confirmed
Severity: P2
Recommendation: fix next

Parent reproduction command: `run_review marker pytest -q "$OUT/probes/test_review_followups.py" -k durable_reindex_request`
Observed result/receipt: followups-complete-LRYoNq: the marker is absent immediately after consumption, before any successful maintenance. This verifies early acknowledgement, not a completed crash/restart experiment.

Exact future regression command: `REVIEW_TASKS=256 run_review future-r3 pytest -q "$WT/tests/test_provider.py" "$WT/tests/test_durable_recovery.py"`
Compatibility/recovery: Keep the existing marker format where possible; acknowledge only the request instance actually completed so a newer request is preserved.
Alternative / keep-versus-change decision: Documenting an acknowledged-but-lost repair request is not sufficient; retain the existing file until successful completion.

### Source rationale, invariant, blind spot, minimal change, regression oracle and cost

**Location/mechanism:** P `zvec-memory/__init__.py:346–357` (`_consume_reindex_request`) unlinks the durable marker before startup dispatch at `249–255`. Rejection/failure paths at `853–871,906–919` retain only in-memory obligation. Marker writer: P `zvec-memory/cli.py:240–260`.

**Impact:** With an existing manifest and matching sidecar, exit/rejection/failure can lose an operator's repair request across restart. Mirror `refresh_required` does not necessarily cover this independent request.

**Blind spot:** Live-handle dirty/retry tests and durable mirror recovery tests do not establish marker survival across failed dispatch plus a fresh provider.

**Smallest change:** Acknowledge the existing marker only after success, matching the request instance so completion cannot erase a newer request. Reuse per-vault serialization.

**Regression oracle:** Reject dispatch, fail indexing, or terminate before completion; restart with an existing index and confirm rediscovery. A newer concurrent marker must survive older completion.

**Cost:** No new dependency, service, file or format; small request-token bookkeeping and maintenance-only filesystem operations.


## R4 — P1, conditional: an in-vault symlink can redirect mirror deletion

Status: confirmed
Severity: P1 conditional
Recommendation: fix next

Parent reproduction command: `run_review mirror-path pytest -q "$OUT/probes/test_review_followups.py" -k unowned_in_vault`
Observed result/receipt: followups-complete-LRYoNq: a deliberately retargeted owned leaf causes deletion of a separate in-vault sentinel file.

Exact future regression command: `REVIEW_TASKS=256 run_review future-r4 pytest -q "$WT/tests/test_mirror_validation.py" "$WT/tests/test_durable_recovery.py"`
Compatibility/recovery: Ordinary regular-file vaults are unchanged. Document refusal of symlink aliases. This is not a demonstrated remote exploit or proof that normal writers create such links.
Alternative / keep-versus-change decision: Accepting a valid in-root referent as sufficient ownership is rejected; reuse existing path validation instead of adding a new security subsystem.

### Source rationale, invariant, blind spot, minimal change, regression oracle and cost

**Location/mechanism:** P `zvec-memory/__init__.py:608–616` (`_mirror_file`) returns a resolved, in-root `.md` referent. Validation at `558–568` accepts it; deletion at `663–665,721–725` unlinks the referent.

**Precondition/impact:** Replace an owned mirror filename with a symlink to an unrelated explicit `.md` inside `facts`. Removing/replacing that mirror can delete the explicit referent. No claim is made that normal writes create these links or an external attacker can modify the vault.

**Blind spot:** P `tests/test_mirror_validation.py:89–127,155–183` covers outside-root candidate links, not a leaf pointing to another valid in-root fact.

**Smallest change:** Reject symlink/noncanonical redirection for owned candidates, including staged paths and actual destructive use. Preserve full-journal validation before effects.

**Regression oracle:** Retarget an owned leaf to a protected in-vault explicit fact, then remove/replace/recover. Fail closed before any destructive change; protected bytes and unrelated pending files remain unchanged. Include candidate-ancestor/staging variants.

**Cost:** No new dependencies/services/state/formats; small mutation/validation path checks. Not a proposal for a general hostile-filesystem sandbox.

## R5 — P1, conditional: ordinary writers bypass containment revalidation

Status: confirmed
Severity: P1 conditional
Recommendation: fix next

Parent reproduction command: `run_review writer-path pytest -q "$OUT/probes/test_review_followups.py" -k redirected_directory`
Observed result/receipt: followups-complete-LRYoNq: after deterministic facts-directory retargeting, an ordinary fact write creates a file outside the configured vault in an owned test directory.

Exact future regression command: `REVIEW_TASKS=256 run_review future-r5 pytest -q "$WT/tests/test_provider.py" "$WT/tests/test_hostio_equivalence.py"`
Compatibility/recovery: Preserve normal paths and file formats; reject redirected roots/ancestors at use and define private session-file creation. The full session/ancestor race matrix is not executed here.
Alternative / keep-versus-change decision: Initialization-only checking is insufficient. General hostile same-UID filesystem sandboxing is deferred; local validation is the smaller change.

### Source rationale, invariant, blind spot, minimal change, regression oracle and cost

**Location/mechanism:** Initial root checks are at P `zvec-memory/__init__.py:220–228`; `_write_fact:978–996` and `_append_turn:998–1010` subsequently use direct paths. Daily `open(path, 'a')` follows a leaf symlink and uses ambient creation permissions.

**Precondition/impact:** Retarget a root/ancestor after initialization, or precreate a daily-session symlink. Explicit/extracted facts or transcript text can be written outside the initialized vault. Session privacy is not explicitly enforced as it is for `mkstemp` facts; actual host permissions were not inspected.

**Blind spot:** P `tests/test_provider.py:128–150` covers initialization-time symlinks; mirror-validation tests exercise journal paths, not ordinary writes or daily-file links.

**Smallest change:** Reuse canonical-root validation immediately before writes; use a no-follow session open with private creation permissions. Preserve the daily format and best-effort logging policy. Path checks alone do not defeat continuously racing same-UID ancestor changes.

**Regression oracle:** Exercise explicit store, queued turn append and extraction after deterministic root retargeting, plus a daily leaf link. Outside bytes must remain unchanged; no successful store receipt for a refused write; verify new session mode.

**Cost:** No new dependencies/services/persistent state/formats; narrow safe-open helper and write-time checks. No additional recall work.


## R6 — P2, malformed-state integrity: replay scalar validation is incomplete

Status: source-only gap
Severity: P2
Recommendation: fix next

Parent reproduction command: `No parent reproduction; use the source locations and proposed regression oracle below.`
Observed result/receipt: No new parent runtime reproduction. This remains source-only evidence, not an executed failure.

Exact future regression command: `REVIEW_TASKS=256 run_review future-r6 pytest -q "$WT/tests/test_mirror_validation.py" "$WT/tests/test_mirror_queue.py" "$WT/tests/test_inbox_contention.py"`
Compatibility/recovery: Retain valid old journals and absent optional fields; reject invalid scalar types without acknowledging queued work.
Alternative / keep-versus-change decision: Keep SQLite/WAL and the existing journal; add narrow schema/watermark validation rather than a migration or second database.

### Source rationale, invariant, blind spot, minimal change, regression oracle and cost

**Location/mechanism:** P `zvec-memory/__init__.py:537–569` does not validate `last_notification` before the deduplication branch at `631–645`. Schema validation at `545–547` accepts booleans/nonpositive integers. Inbox acknowledgement: P `zvec-memory/inbox.py:118–125`.

**Precondition/impact:** A syntactically valid malformed/restored/edited journal, not normal atomic writer output. A boolean/nonnumeric watermark can skip a real notification or wedge replay; acknowledged skipped work may leave stale facts without a pending gate.

**Blind spot:** P `tests/test_provider.py:1010–1024` covers absent/future schema, not invalid scalar types/watermarks. Queue tests assume valid watermark state.

**Smallest change:** Validate actual integer types and supported bounds, excluding booleans while preserving absent-field legacy behavior. Validate queue envelopes without silently discarding an invalid head. Consistent backup pairing is a separate issue.

**Regression oracle:** Use real SQLite IDs with boolean/fractional/string/null/negative/nonfinite watermarks and invalid schema scalars. Invalid state must cause no acknowledgement, mutation or watermark advance and keep recall closed; valid uncertain-ack replay must still converge.

**Cost:** No new dependencies/services/state/formats; scalar checks on already-read state. Preserve narrow SQLite reservation retry, not whole-transaction retry.


## R7 — P2: post-index bookkeeping failure can strand the running latch

Status: confirmed
Severity: P2
Recommendation: fix next

Parent reproduction command: `run_review index-cleanup pytest -q "$OUT/probes/test_review_followups.py" -k index_latch`
Observed result/receipt: followups-complete-LRYoNq: injected UnicodeDecodeError at version retrieval escapes the real identity-bookkeeping method and leaves the index-running latch set. This is a fault-injected unit boundary, not a claim that the pinned engine naturally emits invalid version bytes.

Exact future regression command: `REVIEW_TASKS=256 run_review future-r7 pytest -q "$WT/tests/test_provider.py" "$WT/tests/test_workers.py"`
Compatibility/recovery: Release the latch in every exit path while preserving concurrent dirty/rebuild requests and retry semantics.
Alternative / keep-versus-change decision: A watchdog or scheduler replacement is unnecessary; fix the existing exception/finally boundary.

### Source rationale, invariant, blind spot, minimal change, regression oracle and cost

**Location/mechanism:** P `zvec-memory/__init__.py:873–919` protects recovery/native work, but `_record_engine_state()` at `918` is outside that failure handler. `_record_engine_state:327–335` catches only `OSError`; a non-OS failure can escape into Worker logging (`workers.py:38–44`) before `_index_running` is cleared. Subsequent retry/dispatch at `834–837,864–865` treats the departed job as running.

**Impact:** Later refreshes may never run for that instance, potentially leaving deletion-related recall closed indefinitely. This is an unexecuted exception-path inference.

**Blind spot:** P `tests/test_provider.py:253–276` injects failures inside the protected native block; `tests/test_workers.py:58–70` checks generic continuation, not provider state cleanup.

**Smallest change:** Include post-success bookkeeping in appropriate cleanup/retry handling, or make non-authoritative bookkeeping explicitly nonthrowing while guaranteeing latch release. Preserve concurrent dirty requests/rebuild arguments.

**Regression oracle:** Native index succeeds; identity recording fails once with a non-`OSError`; request another job and prove no stranded latch/lost obligation. Exercise invalid version bytes through the actual runner separately.

**Cost:** No new dependencies/services/persistent state/formats; small exception-boundary correction, no new normal native calls/timers.


## R8 — P2, evidence-gated hardening: capture/tree boundaries are not established

Status: confirmed for the tested boundary
Severity: P2
Recommendation: fix decoding next; defer larger supervision changes

Parent reproduction command: `run_review native-boundary pytest -q "$OUT/probes/test_review_instrument_oracles.py" -k actual_provider_runner`
Observed result/receipt: instrument-oracles: the actual provider subprocess runner raises UnicodeDecodeError on an owned Python fixture emitting one invalid UTF-8 byte. No output-flood, descendant-pipe, cancellation or post-leader-exit experiment was run; those larger boundary questions remain source-only.

Exact future regression command: `REVIEW_TASKS=256 run_review future-r8 pytest -q "$WT/tests/test_provider.py" "$WT/tests/test_process_safety.py"`
Compatibility/recovery: Define noninteractive stdin and decode/error behavior without changing successful CLI output. Preserve shell-free argv and timeout.
Alternative / keep-versus-change decision: Do not transplant the full harness supervisor into the provider without adversarial evidence and a measured maintenance/latency case.

### Source rationale, invariant, blind spot, minimal change, regression oracle and cost

**Location/mechanism:** P `zvec-memory/__init__.py:796–810`, `_run_zg`, uses shell-free argv and timeout but captures complete decoded stdout/stderr before result clipping (`396–406,947–954`). It sets no noninteractive stdin or process-group cleanup, and catches only missing executable/timeout.

**Conditional impact:** A faulty engine/wrapper can exceed returned-context memory bounds, inherit input, emit undecodable output, or leave an independent descendant. This is not a claim that POSIX `subprocess.run` always hangs or an observed child survived.

**Blind spot:** Offline tests mostly mock `_run_zg`; normal native tests do not establish the real boundary under output flood, invalid bytes, timeout or descendants. Other installer/upgrade runner tests would not establish provider behavior.

**Smallest change:** First exercise the actual provider runner. Consider explicit noninteractive stdin and a defined launch/decoding policy. Add byte-limited/tree-aware supervision only if evidence justifies its maintenance cost; no daemon/process manager.

**Regression oracle:** Fixture executables cover missing/nonexecutable launcher, nonzero status, invalid UTF-8, oversized output, timeout and descendant/pipe cases. Record elapsed/return/exception/fixture-child cleanup; distinguish capture bounds from returned-text bounds. Never inspect real environment secrets.

**Cost:** Basic stdin/error policy is small with no new dependencies/services/state/formats. Full bounded supervision is moderate complexity on each native call, not a free cleanup.

## R9 — P2: numeric defaults can defeat valid tool input or misreport a landed store

Status: source-only gap
Severity: P2
Recommendation: fix next

Parent reproduction command: `No parent reproduction; use the source locations and proposed regression oracle below.`
Observed result/receipt: No new parent runtime reproduction. This remains source-only evidence, not an executed failure.

Exact future regression command: `REVIEW_TASKS=256 run_review future-r9 pytest -q "$WT/tests/test_provider.py"`
Compatibility/recovery: Keep supported numeric values; specify finite/type rules and avoid evaluating a bad default when a valid explicit argument exists. Do not misreport a persisted fact as an uncommitted store.
Alternative / keep-versus-change decision: Local checks are smaller than a validation framework or changed storage protocol.

### Source rationale, invariant, blind spot, minimal change, regression oracle and cost

**Location/mechanism:** P `zvec-memory/__init__.py:773–777` (`_recall_limit`) omits `OverflowError`; `937–940` eagerly evaluates that default even when an explicit valid limit is supplied. Debounce conversion at `853–857` can also overflow after fact persistence at `970–974`. Compare `_cap:779–792`, which catches conversion overflow.

**Conditional impact:** Invalid numeric configuration can suppress valid searches or return a store error after bytes landed, encouraging duplicate retries. Ordinary supported settings are not claimed to fail.

**Blind spot:** P `tests/test_provider.py:341–349,605–633,667–713` does not establish nonfinite configuration/default-evaluation behavior.

**Smallest change:** Specify finite/type/default rules, catch relevant overflow, and evaluate defaults only when needed. Distinguish scheduling failure from persistence failure in store receipts; use local validation, not a new package.

**Regression oracle:** Test nonfinite values, booleans, oversized numbers, null/containers and valid explicit/default values. An explicit valid limit must bypass a broken default. Fault scheduling after persistence and assert an honest receipt/no unintended retry duplication.

**Cost:** No new dependencies/services/state/formats; scalar hot-path checks with no extra filesystem/native work.


## INT-01 — P1: the documented fresh setup/install sequence never connects the provider to the installed launcher

Status: source-only gap
Severity: P1
Recommendation: fix next

Parent reproduction command: `No parent reproduction; use the source locations and proposed regression oracle below.`
Observed result/receipt: No new parent runtime reproduction. This remains source-only evidence, not an executed failure.

Exact future regression command: `REVIEW_TASKS=256 run_review future-int-01 pytest -q "$WT/tests/test_engine.py" "$WT/tests/test_host_contract.py"`
Compatibility/recovery: Preserve explicit network opt-in. Complete setup/installation wiring to the verified private launcher before advertising availability; reversing documented commands alone is not a verified fix.
Alternative / keep-versus-change decision: Do not add another installer or rely on an unrelated global zg on PATH.

### Source rationale, invariant, blind spot, minimal change, regression oracle and cost

- **Location:** P:`README.md:20-33`; `zvec-memory/engine.py:175-184,227-249,274-311` (`ensure_engine`, `install_engine`, `post_setup`); `zvec-memory/cli.py:217-231` (`_engine_command`); `zvec-memory/__init__.py:185-203` (`_zg`, `is_available`, instance hook). H:`hermes_cli/memory_setup.py:157-184`; `agent/agent_init.py:1333-1358`.
- **Mechanism/impact:** On a genuinely fresh private runtime, setup's `ensure_engine` returns `missing-engine`, then `post_setup` writes defaults and activation **without `zg_bin`**. The host invokes the instance hook, discards its returned diagnostic dictionary, and stops. The next documented `engine install` creates the private `zg-default` launcher and prints its path, but neither `install_engine` nor `_engine_command` saves that path to provider JSON. Runtime then searches PATH for `zg`; doctor instead requires an explicitly configured `zg_bin`. With no unrelated global `zg`, the fresh agent rejects the provider as unavailable despite successful installation. `unavailable_reason()` also sends the user to an unpinned global npm install rather than the owned private-runtime workflow. This is not a missing-instance-hook defect: the instance hook exists.
- **Evidence:** V-source conditional end-to-end trace; **not reproduced here**. T-inspected `test_engine.py:173-180` explicitly expects setup activation without an engine; its installed-runtime hook tests do not cover the subsequent explicit-install handoff.
- **Minimal correction:** Persist the successful explicit install's launcher through the canonical config writer, preserve unrelated settings, and surface `missing-engine`/failed host-save results at the setup boundary. Update the misleading setup comment/help. Do not make setup download implicitly; simply moving installation before activation can also fail because host CLI discovery exposes only the active provider's command.
- **Regression oracle:** In an outer sandbox, real host setup hook → fake npm/native installation boundary → actual CLI install handler → fresh provider load. Start with neither private runtime nor PATH `zg`; assert the saved `zg_bin` equals the generated launcher and that the cold availability/doctor target that same executable. Inject host-save failure and assert it is not silently reported as completed setup.
- **Complexity:** Small: setup/CLI persistence and focused integration tests; no new dependency or daemon.


## INT-02 — P1: setup and desktop transitions can discard effective settings or generate artifacts for a different configuration

Status: source-only gap
Severity: P1
Recommendation: fix next

Parent reproduction command: `No parent reproduction; use the source locations and proposed regression oracle below.`
Observed result/receipt: No new parent runtime reproduction. This remains source-only evidence, not an executed failure.

Exact future regression command: `REVIEW_TASKS=256 run_review future-int-02 pytest -q "$WT/tests/test_engine.py" "$WT/tests/test_host_contract.py" "$WT/tests/test_provider.py"`
Compatibility/recovery: Migrate effective legacy settings losslessly before native JSON becomes authoritative; preserve malformed configuration as evidence rather than overwriting it.
Alternative / keep-versus-change decision: Silently switching vaults/settings is rejected. Reuse a cold-safe configuration helper rather than a new configuration system.

### Source rationale, invariant, blind spot, minimal change, regression oracle and cost

- **Location:** P:`zvec-memory/engine.py:274-311`; `zvec-memory/__init__.py:122-134,764-769`. H:`hermes_cli/web_routers/memory_providers.py:105-122,148-178,248-263` (`_read_json_dict`, `_write_provider_flat`, declared payload).
- **Mechanism/impact:** `post_setup` calls `ensure_engine` with the **YAML plugin block before reading authoritative native JSON**. Existing native `runtime_dir`/`engine_home`/`server_url`/cache choices therefore need not govern the generated artifacts; the subsequently preserved JSON can describe a different engine home. With legacy-only YAML, the new JSON copies only selected defaults/fields: legacy `vault`, custom `zg_bin`, retrieval/extraction settings and other keys are not merged generally. Once that JSON exists, runtime correctly stops reading YAML, potentially switching to an empty default vault or losing deliberate settings. The native `save_config` method *does* preserve legacy values, but real setup takes the hook instead, and the real desktop flat-JSON writer does not call that method. Its first partial edit on a legacy-only profile writes a sparse native object with the same loss of omitted settings. Malformed native JSON is silently replaced from `{}` by setup/the generic web reader, whereas runtime refuses it; a recovery workflow can overwrite evidence of bad configuration.
- **Evidence:** V-source across actual writers and consumer, not just matching schema declarations. T-inspected `test_engine.py:147-170` checks that native values remain in JSON but supplies engine paths in YAML and does not compare those native values with artifact bytes. No executed migration/corruption reproduction here.
- **Minimal correction:** Read and validate the effective native-or-legacy object **before** generation; merge legacy settings only when JSON is absent; preserve explicit false/zero and native-object authority; refuse malformed/non-object native input rather than replacing it. Make legacy migration an explicit setup step before desktop partial edits, or add a narrowly scoped host-supported migration path. Do not introduce cold-load writes or a second canonical store. Do not claim fixing provider `save_config` alone fixes the generic UI writer.
- **Regression oracle:** Legacy-only nondefault vault plus disabled extraction/zero context cap → real hook and first real desktop partial save → fresh loader; omitted settings must survive and YAML bytes remain untouched except deliberate activation. Native JSON engine settings conflicting with YAML must match launcher/unit/manifest output. Malformed, array and null native roots must fail without rewriting bytes. Retain an explicit empty-object case.
- **Complexity:** Small-to-medium: shared strict reader/setup merge, real-writer fixtures, and an explicit legacy migration boundary. Host UI behavior may require a scoped upstream change; no general configuration framework.


## INT-03 — P2: CLI resolution differs from runtime, including authoritative `{}` and the advertised embedding environment fallback

Status: confirmed
Severity: P2
Recommendation: fix next

Parent reproduction command: `run_review config-contract pytest -q "$OUT/probes/test_review_edges.py" -k empty_json`
Observed result/receipt: contract-edges-UxzsuG: present empty JSON is authoritative in the provider but the CLI selects legacy YAML. Other null/env/path discrepancies remain source-reviewed, not all reproduced.

Exact future regression command: `REVIEW_TASKS=256 run_review future-int-03 pytest -q "$WT/tests/test_cli.py" "$WT/tests/test_provider.py" "$WT/tests/test_host_contract.py"`
Compatibility/recovery: Distinguish absent, empty, malformed and invalid-shape configuration. Explicitly choose a compatibility policy for null rather than silently normalizing it.
Alternative / keep-versus-change decision: Document-only divergence is rejected because doctor/reindex can target the wrong vault.

### Source rationale, invariant, blind spot, minimal change, regression oracle and cost

- **Location:** P:`zvec-memory/cli.py:123-150,164-195,263-290`; `zvec-memory/__init__.py:53,122-134,185-189,205-218,828-830`; `zvec-memory/config_schema.py:34-40`; `zvec-memory/engine.py:295-299`.
- **Mechanism/impact:** Runtime distinguishes *missing native file* from a present `{}` and rejects non-object JSON. CLI falls back to YAML on **any falsy parsed config**, including `{}`, and silently falls back after a JSON parse error. Thus doctor can inspect—and `reindex` can create a marker in—a legacy vault the provider will not use. A truthy non-object root instead reaches `.get` and crashes. A native `vault: null` resolves as the literal relative directory `None` in runtime (`str(None)`), but as the default vault in CLI. Runtime defaults missing `zg_bin` to PATH `zg`; doctor declares it missing. Separately the declarative UI advertises `ZVEC_GREP_EMBEDDING` as fallback, but the runtime's initial build explicitly passes its fixed default when JSON omits embedding, and setup persists that fixed default. The UI's displayed effective model can therefore differ from the provider's selected argument.
- **Evidence:** V-source. The original `{}`-versus-YAML candidate is now supported by exact conflicting branches, **not an executed reproduction**. Existing provider config tests were read; CLI tests cover ordinary relative and placeholder paths, not the complete cross-reader matrix.
- **Minimal correction:** Share presence/type/default/path policy across the cold reader and CLI; either honor the declared environment fallback end-to-end or remove the promise. Treat null/invalid paths explicitly, not by accidental string conversion. Keep `status`'s documented nonfatal exit separate from diagnostic truth.
- **Regression oracle:** Table-driven real consumers for absent/`{}`/nonempty/malformed/array/null roots, absent/blank/null/relative/absolute/`~`/both HERMES_HOME spellings, and conflicting YAML. Assert reindex writes only to the runtime-resolved target. With JSON embedding absent and an environment value present, compare UI field resolution, fresh-build argv and stored engine identity; verify explicit JSON wins.
- **Complexity:** Small: pure local resolver reuse and parameterized tests; avoid another cache or mutable global policy.

## INT-04 — P2: doctor can certify corrupt or unknown state, and malformed shapes can prevent any diagnostic report

Status: confirmed
Severity: P2
Recommendation: fix next

Parent reproduction command: `run_review doctor-contract pytest -q "$OUT/probes/test_review_edges.py" -k bad_journal`
Observed result/receipt: contract-edges-UxzsuG: malformed mirror JSON is reported healthy; a list root raises AttributeError. Identity/task-ceiling subclaims remain source-reviewed.

Exact future regression command: `REVIEW_TASKS=256 run_review future-int-04 pytest -q "$WT/tests/test_cli.py"`
Compatibility/recovery: Return structured corrupt/unknown/not-checked states without treating missing evidence as health. Keep diagnostics cheap and preserve public output compatibility deliberately.
Alternative / keep-versus-change decision: False-green doctor output is rejected; no new monitoring service is needed.

### Source rationale, invariant, blind spot, minimal change, regression oracle and cost

- **Location:** P:`zvec-memory/cli.py:36-69,80-155,158-161` (`vault_state`, `collect_checks`, `_task_ceiling`, `_unit_tasks_max`, `report`).
- **Mechanism/impact:** Bad `.mirror-map.json` syntax sets `mirror_records='unreadable'` but leaves pending `None`; the mirror check accepts `None` and reports success. Valid JSON with a non-object root or malformed pending collections can raise an uncaught attribute/type exception. Engine identity is read but never compared: the check named `identity` only tests absence of `.mirror-delivery-failed.json`, so missing/corrupt/stale identity can pass. An unreadable task ceiling and unlimited task ceiling both become `None`, accepted as healthy. `report()['checked']` checks names only, not whether meaningful evidence was obtained. A green doctor can therefore conceal exactly the state that a recovery decision needs; `--json` is not guaranteed a structured failure on bad input.
- **Evidence:** V-source deterministic branches; **not executed here**. Existing CLI tests inspect normal fake-ready output, pending rows, marker presence and loaded-unit task limits, but do not establish truthful corrupt-map/shape/unknown-resource behavior.
- **Minimal correction:** Return typed read states (absent/valid/corrupt/unreadable/unknown), validate JSON shapes, and make unknown/corrupt evidence fail or explicitly unverified. Separate delivery marker from actual engine-format identity/readiness checks. Keep the command diagnostic-only; do not repair or start a service while checking.
- **Regression oracle:** Corrupt map, JSON list/null, malformed pending list, permission/read failure, missing/stale identity, and unavailable systemd/cgroup information must produce named non-green JSON checks, not traceback or false success. Fake engine readiness must not override a corrupt durable map. Preserve deliberate absent-inbox acceptance and `status` exit semantics.
- **Complexity:** Small: typed diagnostic results and negative fixtures. Reuse the existing identity contract rather than inventing a second format.


## INT-05 — P2: generated launcher/unit strings do not preserve supported path values

Status: source-only gap
Severity: P2
Recommendation: fix next

Parent reproduction command: `No parent reproduction; use the source locations and proposed regression oracle below.`
Observed result/receipt: No new parent runtime reproduction. This remains source-only evidence, not an executed failure.

Exact future regression command: `REVIEW_TASKS=256 run_review future-int-05 pytest -q "$WT/tests/test_engine.py"`
Compatibility/recovery: Either encode supported shell and systemd paths using their distinct grammars or reject unsupported values before writing any artifact.
Alternative / keep-versus-change decision: Generic shell quoting applied to systemd is not accepted. No claim of remote code execution is made.

### Source rationale, invariant, blind spot, minimal change, regression oracle and cost

- **Location:** P:`zvec-memory/engine.py:47-50,119-158` (`_expand`, `launcher_script`, `unit_template`).
- **Mechanism/impact:** Path resolution accepts ordinary paths containing spaces, but the launcher's `exec` interpolates the JS entry path unquoted; generated systemd `ExecStart` also interpolates launcher and token-file paths without argument encoding. The launcher places configuration strings inside unescaped double quotes, leaving shell expansion/quote semantics active. A valid custom runtime, home or cache pathname can consequently become multiple arguments or different text. This is a local configuration execution boundary, **not evidence of a remote exploit or elevated privilege**.
- **Evidence:** V-source generated strings. Existing byte/idempotence tests use simple temporary paths; no invocation was run here.
- **Minimal correction:** Encode each shell argument/environment value and systemd argument using the respective syntax; do not use one shell-quoting routine as if it were systemd escaping. Alternatively reject unsupported characters clearly before writing, if that is the intended narrow support contract.
- **Regression oracle:** Scratch-only generated artifacts with spaces, quotes, dollar signs and percent characters; assert harmless argv-capturing stubs receive exact paths. Include token-file and cache paths, not only the launcher filename. No live unit reload is needed.
- **Complexity:** Small: quoting/validation helpers and focused generated-artifact tests; no service abstraction.


## INT-06 — P2: installation accepts PATH npm but the generated runtime assumes an unchecked `/usr/bin/node`

Status: source-only gap
Severity: P2
Recommendation: fix next

Parent reproduction command: `No parent reproduction; use the source locations and proposed regression oracle below.`
Observed result/receipt: No new parent runtime reproduction. This remains source-only evidence, not an executed failure.

Exact future regression command: `REVIEW_TASKS=256 run_review future-int-06 pytest -q "$WT/tests/test_engine.py"`
Compatibility/recovery: Pick and verify one absolute Node interpreter contract consistently with npm. Keep the pinned package and explicit install policy.
Alternative / keep-versus-change decision: Successful npm download is not runnable-engine readiness; no additional runtime manager is proposed.

### Source rationale, invariant, blind spot, minimal change, regression oracle and cost

- **Location:** P:`zvec-memory/engine.py:27-30,112-116,131,175-184,227-249`; P:`tests/test_engine.py:236-268`.
- **Mechanism/impact:** Explicit installation deliberately selects `npm` from PATH and verifies the package version, yet the launcher always executes `/usr/bin/node`. A supported npm installed with a user-managed Node can successfully fetch the package while `/usr/bin/node` is absent or too old. `ensure_engine` only requires the JS entry file and can publish success with missing/unreadable package version; it does not establish runnable Node compatibility. Package pinning is real on the install branch, but does not prove the generated interpreter contract.
- **Evidence:** V-source; T-inspected test explicitly validates a PATH npm at `/opt/node/bin/npm` but returns install failure before launcher execution. No Node/npm command was executed by this reviewer.
- **Minimal correction:** Decide one supported interpreter contract: validate the fixed interpreter and minimum version explicitly, or resolve and persist one verified absolute Node path coherently with npm. Reject invalid package metadata when claiming a verified runtime. Retain exact package pinning and explicit network opt-in.
- **Regression oracle:** Fake PATH npm success with missing/old fixed Node must fail clearly rather than report usable artifacts; supported Node/package must yield matching launcher and manifest. Include missing, malformed and wrong-shaped package metadata.
- **Complexity:** Small: interpreter/metadata validation and subprocess-boundary tests, not a Node installation manager.


## INT-07 — P2: unit conflict handling protects the unit but not the rest of the shared engine artifacts

Status: source-only gap
Severity: P2
Recommendation: fix next

Parent reproduction command: `No parent reproduction; use the source locations and proposed regression oracle below.`
Observed result/receipt: No new parent runtime reproduction. This remains source-only evidence, not an executed failure.

Exact future regression command: `REVIEW_TASKS=256 run_review future-int-07 pytest -q "$WT/tests/test_engine.py" "$WT/tests/test_host_contract.py"`
Compatibility/recovery: Check profile scope and artifact-set conflicts before rewriting a shared launcher or manifest; preserve customized unit and launcher together.
Alternative / keep-versus-change decision: Keep the intended default-profile scope and fail clearly rather than introducing multi-profile daemon orchestration.

### Source rationale, invariant, blind spot, minimal change, regression oracle and cost

- **Location:** P:`zvec-memory/engine.py:65-90,175-224`; `zvec-memory/cli.py:227-231`; H:`hermes_cli/memory_setup.py:157-164`.
- **Mechanism/impact:** `ensure_engine` rewrites the launcher **before** detecting a differing unit; it preserves the unit and writes `.new`, then updates the manifest and returns top-level `updated`/`present`. CLI treats those statuses as success even with `unit_status='conflict'`; setup's host caller discards the result entirely. A unit preserved byte-for-byte may now execute a changed launcher/environment. Also, the default runtime root and unit name are user-global while engine home depends on the active Hermes home; no default-profile guard prevents a second profile's setup from rewriting that shared launcher. The generated text says default-profile-only, but that restriction is not enforced. No service start/reload occurs here, so this is artifact drift rather than an observed live-service change.
- **Evidence:** V-source; T-inspected `test_engine.py:132-144` protects unit bytes but not the whole artifact set. Parent handles the separate live service anomaly.
- **Minimal correction:** Check scope and conflicts before mutating owned artifacts; expose conflict distinctly and do not claim applied readiness. Preserve customized bytes as a set or stage candidates for explicit adoption. For this default-only design, refuse incompatible named-profile/shared-root use; **do not build profile daemon tenancy**.
- **Regression oracle:** Preexisting customized unit plus changed engine home: compare launcher/unit/manifest bytes before/after and require an explicit conflict without a hidden environment switch. Repeat with two isolated Hermes homes sharing the default runtime root. Verify second-run byte idempotence when there is no conflict.
- **Complexity:** Small-to-medium: ordering, narrow scope guard and multi-artifact assertions; no persistent coordinator.

## INT-08 — P1 recovery risk: `backup_paths()` discovers the vault, but host backup does not snapshot its SQLite inbox consistently

Status: source-only gap
Severity: P1 conditional
Recommendation: fix snapshot contract before claiming online-backup recovery

Parent reproduction command: `No parent reproduction; use the source locations and proposed regression oracle below.`
Observed result/receipt: No new parent runtime reproduction. This remains source-only evidence, not an executed failure.

Exact future regression command: `REVIEW_TASKS=256 run_review future-int-08 pytest -q "$WT/tests/test_host_contract.py" "$WT/tests/test_durable_recovery.py"`
Compatibility/recovery: A consistent SQLite inbox snapshot must correspond to the map/fact/staging generation. Coordinate with host backup support; retaining a path in backup discovery alone is not a restore guarantee.
Alternative / keep-versus-change decision: No corruption was reproduced. Quiescent backup can be a documented restricted policy only after verification; do not add a second backup/database system.

### Source rationale, invariant, blind spot, minimal change, regression oracle and cost

- **Location:** P:`zvec-memory/__init__.py:747-749`; `zvec-memory/inbox.py:1-85` (SQLite inbox/WAL setup). H:`hermes_cli/backup.py:117-121,229-273,461-480,526-542,593-604`.
- **Mechanism/impact:** The plugin correctly returns the resolved vault even before initialization. However, the host's normal archive writer uses SQLite-safe snapshotting **only for `.db`**. The inbox is `.mirror-inbox.sqlite3`, so it is byte-copied. Its `.sqlite3-wal`/`.sqlite3-shm` are also not covered by the host's `.db-*` sidecar exclusion and may be copied independently. External-provider paths bypass the SQLite-safe writer entirely. Concurrent writes/checkpoints therefore have no coherent database-generation capture, and the separate map/fact/staging files have no shared provider-lock snapshot either. Inclusion is not proof that an accepted replacement/removal notification survives restore. This is a **conditional online-backup risk, not a reproduced corrupt archive or observed data loss**. Paths outside the user's home are additionally skipped by the host's explicit portability/security rule, despite being valid provider vault paths.
- **Evidence:** V-source integration mismatch. H/R backup diffs retain this suffix/external-copy behavior in both sources. Existing cold backup tests check returned paths/no side effects, not restored SQLite state or a coherent mirror transaction.
- **Minimal correction:** Until an isolated restore oracle passes, qualify the backup claim and require a quiesced, coherent vault snapshot for recovery. A narrowly scoped host fix should route provider SQLite files through the safe-copy path with matching sidecar rules; that alone does not prove cross-file mirror consistency. Establish the required quiescence/lock boundary instead of adding another durable store or blanket-copying live sidecars. Do not weaken the outside-home safety restriction silently.
- **Regression oracle:** Under a wholly synthetic home, hold a real WAL inbox writer open, enqueue committed notifications and coordinate checkpoint/write activity while archiving. Restore into a separate directory and verify pending/accepted operation IDs, map generation and referenced fact/staging files. Cover in-home and declared external-under-home vaults; record outside-home exclusion. No user vault is needed.
- **Complexity:** Medium: scoped host backup support and a restore/coherence test; interim documented quiescence is lower cost than pretending online backup is validated.


## PKG-1 — Verification can overwrite real configuration or reach the production engine

Status: confirmed
Severity: P0 safety blocker
Recommendation: fix before unguided test/upgrade execution

Parent reproduction command: `run_review upgrade-safety pytest -q "$OUT/probes/test_review_rollback.py"`
Observed result/receipt: Untouched baseline rollback test attempts default-home service writes; followups-complete-LRYoNq overwrites a disposable unselected profile despite HERMES_HOME. Native production-launcher routing is source-reviewed, not exercised against production.

Exact future regression command: `REVIEW_TASKS=256 REVIEW_NATIVE=1 run_review future-pkg-1 pytest -q "$WT/tests/test_upgrade.py" "$WT/tests/test_provider_native.py" "$WT/tests/test_zg_native.py"`
Compatibility/recovery: Isolate HOME, HERMES_HOME and XDG roots before imports; use raw test binaries, never the installed production wrapper. This review did not overwrite production and does not attribute the pre-existing service anomaly to a past actor.
Alternative / keep-versus-change decision: Do not repair tests by creating real home directories. Prefer retiring the duplicated upgrader in favor of host-managed plugin updates.

### Source rationale, invariant, blind spot, minimal change, regression oracle and cost

**Priority P0 (configuration-loss risk); status: source-only gap; fix before any unguided test/upgrade run.**

`tests/test_upgrade.py:154–172` constructs fake `config.yaml` and `hermes-zvec-memory.service`, then directly calls `rollback`. The plugin/launcher destinations are temporary, but `rollback` sends those two files to `Path.home()/.hermes/config.yaml` and `Path.home()/.config/systemd/user/hermes-zvec-memory.service` (`upgrade.py:207–226`). `tests/conftest.py:4–9,21–29` redirects HERMES_HOME/runtime/cache, **not HOME or XDG_CONFIG_HOME**; the test does not patch `Path.home()`. If the real parents exist, the copies overwrite real user files; if absent, an OSError can interrupt the test after earlier copies. Mocking `runner` does not intercept `shutil.copy2`. This is a possible mechanism, not attribution of the plan's pre-existing service anomaly.

Separately, the native gate deliberately passes the installed launcher (`upgrade.py:54–57,88–100`). Its generated script exports absolute engine home, cache, server URL/token-file path and auto mode (`engine.py:119–131`). This overrides the native fixtures' attempted isolation (`test_zg_native.py:29–49`, `test_provider_native.py:35–47,85–99`); provider queries use `--mode auto` (`__init__.py:812–820`). A temporary HOME alone cannot sanitize a launcher embedding production locations. `--dry-run` still executes these suites (`upgrade.py:182–199`).

**Minimal fix:** thread config/unit destinations through Context; establish HOME/XDG isolation before test collection; use a raw pinned, run-owned native engine or a test-only wrapper with wholly isolated roots. Reject the production launcher as a synthetic test prerequisite. Change `tests/test_upgrade.py`, `tests/conftest.py`, `scripts/upgrade.py` and the documented verification recipe only; do not change the production launcher to accommodate tests.

**Oracle:** parent supplies sentinel files outside the test root; rollback and every gate leave their bytes/modes unchanged, all writes stay beneath the test root, and the native receipt contains no production endpoint/root. Future offline command: `$PY -m pytest tests/test_upgrade.py -q`; native tests only after raw-engine containment is verified. Existing tests assert restored temporary plugin/launcher bytes, not config/unit containment.

**Cost:** no runtime dependency/service/new durable format/hot-path work; explicit test and maintenance paths. Merely documenting a warning is insufficient while default upgrade/dry-run executes unsafe gates. This review made no production-state check or repair.


## PKG-2 — Fresh-checkout upgrade and campaign prerequisites are missing

Status: confirmed
Severity: P1
Recommendation: fix next

Parent reproduction command: `run_review prerequisites pytest -q "$WT/tests/test_run_campaign.py" "$WT/tests/test_upgrade.py"`
Observed result/receipt: Untouched baseline: two campaign list/argument tests fail on absent ignored planned.json, and the upgrade green-path test fails its real ignored-helper existence check.

Exact future regression command: `REVIEW_TASKS=256 run_review future-pkg-2 pytest -q "$WT/tests/test_run_campaign.py" "$WT/tests/test_upgrade.py"`
Compatibility/recovery: Ship required non-sensitive inputs/helpers or make the maintainer-only requirement explicit. Keep private baselines, models, tokens and run data untracked.
Alternative / keep-versus-change decision: Do not copy opaque ignored production helpers merely to turn the tests green; remove or replace the duplicate upgrade path.

### Source rationale, invariant, blind spot, minimal change, regression oracle and cost

**Priority P1; status: confirmed packaging defect, runtime failure predictions unexecuted; fix next.**

`step_backup` fails on the missing ignored helper after potentially expensive gates. `test_a_green_run_gates_then_swaps_in_order` uses `repo=ROOT` and a FakeRunner but does **not** fake `helper.is_file()` (`test_upgrade.py:42–67,104–121`), so the unshipped helper defeats its expected green result. This is stronger than “mocks hide everything”: the real existence check predicts a failed green-path assertion in a clean checkout.

`run_campaign.py:243–244,279–284` reads private cases before handling `--list` or rejecting an unknown label; `test_run_campaign.py:70–86` expects 2/0 without providing those cases. Missing cases instead produce an uncaught FileNotFoundError. Rebaseline writes under an assumed existing campaign directory and reads default-home production state (`run_campaign.py:36,48–74,247–260`). A helper manually supplied to get past backup does not make the remaining workflow self-contained.

**Minimal fix:** make backup self-contained in `scripts/upgrade.py` (one shared published-file inventory), or explicitly retire this upgrader in favor of host-managed updates. Remove campaign rebaselining from the ordinary upgrade path; keep it an explicit maintainer operation. If the public campaign guide remains supported, move generic case definitions/capture code into tracked files and bootstrap *new private* receipts locally; tests should use tracked/synthetic fixtures, never old operator artifacts. Scope: `scripts/upgrade.py`, `tests/test_upgrade.py`, `scripts/run_campaign.py`, `tests/test_run_campaign.py`, relevant README/maintenance/stress guide.

**Oracle:** a fresh exported tracked tree with empty `.test-tools/` can execute offline contract tests and produce a real temporary backup/receipt; missing optional campaign setup gives an actionable usage error without mutation. `$PY -m pytest tests/test_upgrade.py tests/test_run_campaign.py -q`. No fabricated backup directory or current suite total qualifies as proof.

**Cost:** no dependency/service/hot-path work; small tracked maintenance functions/data and existing private receipts. Do not ship all ignored artifacts, install into system Python, or add a deployment framework. Keeping the old helper only as private historical tooling is acceptable once no shipped feature requires it.


## PKG-3 — Rollback is not restoration of a complete, validated generation

Status: confirmed
Severity: P1
Recommendation: fix next or retire duplicate upgrader

Parent reproduction command: `run_review rollback-scope pytest -q "$OUT/probes/test_review_rollback.py" -k unselected`
Observed result/receipt: The default-profile redirection subclaim is reproduced in followups-complete-LRYoNq. Incomplete generation selection, old inventory, hash/symlink/absence handling remain source-reviewed.

Exact future regression command: `REVIEW_TASKS=256 run_review future-pkg-3 pytest -q "$WT/tests/test_upgrade.py"`
Compatibility/recovery: Any retained rollback must restore a validated complete generation to explicit scoped destinations and preserve unrelated later configuration changes.
Alternative / keep-versus-change decision: Prefer host-managed updates over building a second transactional installer. If retained, a complete manifest/restore protocol is necessary and is not a zero-complexity change.

### Source rationale, invariant, blind spot, minimal change, regression oracle and cost

**Priority P1; status: confirmed inventory mismatch plus source-only recovery gaps; fix next.**

The helper backs up only the older seven-file set, silently skips absent files, and uses second-resolution directory names with `exist_ok=True` (`original-helper:20–21,28–46`). Two same-stamp runs can reuse a directory. Its `deployment.json` has prior hashes, but `step_backup` trusts any JSON `backup` value without checking directory, type, completeness or hashes (`upgrade.py:103–113`). `rollback` reads neither receipt nor hashes, picks the lexically newest directory even if incomplete, copies whatever exists, and reports success if **anything** was restored (`upgrade.py:202–229`). The test explicitly endorses choosing the incomplete newest directory (`test_upgrade.py:163–169`).

An old `__init__.py` can be restored alongside newer `hostio.py`/`engine.py`/`cli.py`; newly introduced files are never removed. Missing old files/absence are not recorded. A new destination or source symlink is followed by `is_file`/`copy2`, not rejected by a generation validator. Default-home helper paths ignore Context's plugin/backup/launcher overrides; rollback additionally ignores profile/XDG config/unit locations. It can overwrite unrelated later edits to configuration/service files even though the plugin upgrade never changed them.

**Minimal fix:** one owned-file inventory, collision-free backup directory, receipt containing source revision, target identity, presence/absence and hashes, validated completely before mutation. Thread all actual targets through Context. Restore only owned paths to the prior generation, including removal of introduced owned files. Reject incomplete/corrupt receipts before creating/copying destinations; support a deliberately chosen old backup only through validated explicit recovery. Avoid silently falling back to an arbitrary older generation.

For configuration, choose a narrow contract: a **plugin-code** upgrade need not snapshot the live vault, SQLite WAL or engine database. Do not auto-restore untouched global settings. If settings migration/rollback is offered, include the canonical native config and exact scoped paths, with conflict checks against intervening edits; do not call the present YAML-only snapshot a full operational rollback.

**Oracle:** temporary old/new generations with changed and introduced modules round-trip byte-for-byte; absent paths return to absent; corrupt hash/incomplete/latest/same-timestamp/foreign-profile receipts fail before a write; unrelated files/settings remain unchanged. `$PY -m pytest tests/test_upgrade.py -q` after adding these filesystem cases. Existing mocked green receipt and two-file rollback fixture do not test them.

**Cost:** expand the existing receipt schema (not a new database/service); hashes and bounded file copies only during maintenance, zero recall-path cost. Documented partial backup is an acceptable archived helper contract, not an acceptable default recovery guarantee. Old receipts must be rejected clearly or explicitly converted; never silently treated as complete.

## PKG-4 — Post-copy failures are not compensated or accurately reported

Status: source-only gap
Severity: P1
Recommendation: fix next or retire duplicate upgrader

Parent reproduction command: `No parent reproduction; use the source locations and proposed regression oracle below.`
Observed result/receipt: No new parent runtime reproduction. This remains source-only evidence, not an executed failure.

Exact future regression command: `REVIEW_TASKS=256 run_review future-pkg-4 pytest -q "$WT/tests/test_upgrade.py"`
Compatibility/recovery: Do not bless a new production baseline before actual health verification; distinguish tested, copied, activated and healthy generations and preserve rollback on partial copy failure.
Alternative / keep-versus-change decision: Retiring this duplication is smaller than creating another updater, locking scheme and rollback state machine.

### Source rationale, invariant, blind spot, minimal change, regression oracle and cost

**Priority P1; status: source-only gap; fix next.**

`changed_files` skips a missing source (`upgrade.py:116–125`); a missing required module can therefore yield “already current.” `step_deploy` directly overwrites files one at a time with `copy2`, with no staging, generation lock or recovery journal (`128–138`). A second-copy error or process death leaves a mixed generation. `run_upgrade` catches only GateFailure, not those filesystem errors (`182–199`); later rebaseline/doctor failures return a failed gate after deployment remains changed. Rebaseline also runs **before** doctor and can bless an unhealthy generation as the campaign baseline (`141–168`).

`step_verify` checks a clean tree and prints HEAD once; it does not verify a selected release pin, bind tested bytes to a receipt, or recheck after gates (`68–77`). Ignored helper contents are outside that check. `step_deploy` also leaves host installation provenance/sidecar metadata untouched when changing a host-managed plugin: a recorded catalog pin must not be presented as proof of these manually copied bytes. The host's own install path records revision/pin metadata and publishes the selected candidate (`host hermes_cli/plugins_cmd_install.py:315–395`).

**Minimal fix:** either retire the duplicate mutation path, or preflight all required regular files, stage/hash the candidate, record tested revision/content and intended targets, and make errors return explicit `changed`/recovery information. Provide deterministic compensation using PKG-3's receipt for caught failures and a documented interrupted-operation recovery path. A per-file `os.replace` is useful but is **not** an atomic multi-file generation; do not advertise global atomicity without proving it. Move optional baseline promotion after successful health verification. Do not blindly restore external configuration changed concurrently. For catalog-managed installations, prefer host-managed updates rather than inventing parallel metadata semantics.

**Oracle:** inject failure before/after each copy, between deploy/doctor/baseline, and at receipt writes. Assert either the exact prior generation or a truthful recoverable changed-state result—never “untouched” while bytes changed. Test a missing source, source edit after verification, and a pre-existing target symlink. `$PY -m pytest tests/test_upgrade.py -q` with filesystem fault cases. No current test injects these failures.

**Cost:** bounded staging/copies and an extended existing receipt; no background service/dependency/hot-path work. Narrowing the documentation to “pre-deploy gate failure leaves plugin code unchanged; later failure may require rollback” is an immediate correction, not a substitute for complete rollback. A broad deployment framework or another persistent database is rejected.


## PKG-5 — Success, skip and dry-run semantics are misleading

Status: confirmed
Severity: P1
Recommendation: fix next

Parent reproduction command: `run_review dry-rollback pytest -q "$OUT/probes/test_review_rollback.py" -k dry_run`
Observed result/receipt: followups-complete-LRYoNq: --dry-run --rollback changes plugin/config fixture bytes. Skip-as-success, --only bypass and native-count heuristics remain source-reviewed.

Exact future regression command: `REVIEW_TASKS=256 run_review future-pkg-5 pytest -q "$WT/tests/test_upgrade.py"`
Compatibility/recovery: Reject contradictory dry-run rollback or implement a genuinely non-mutating plan. Preserve explicit not-checked/skipped distinctions in machine output.
Alternative / keep-versus-change decision: A caveat alone is not adequate for a flag promising no writes.

### Source rationale, invariant, blind spot, minimal change, regression oracle and cost

**Priority P1; status: source-only gap; fix next.**

- `--dry-run --rollback` calls `rollback` without honoring dry-run (`upgrade.py:232–248`); it can copy files. This is not tested.
- `--only deploy` intentionally bypasses verify/tests/backup and reports success (`250–258`, `test_upgrade.py:182–188`). It is not a gated upgrade.
- Missing `hermes` produces a successful `doctor` step with “skipped”; missing campaign controller likewise produces successful rebaseline (`141–159,197–199`). A dry-run marks unexecuted mutating steps `ok: true` with `skipped: dry-run`; consumers must not treat this as completed health/deployment.
- The native all-skipped guard is a string heuristic. Some passing tests plus arbitrary skips pass; there is no required-node/coverage contract. Empty successful output also passes (`88–100`). `green_runner`'s `386 passed`, `9 passed, 1 skipped`, fake backup path and “healthy” are **fixture strings**, not this review's results (`test_upgrade.py:58–67`).

**Minimal fix:** mutually reject dry-run rollback or implement a genuinely non-writing rollback plan; restrict `--only` to diagnostic/read-only operations or require an unmistakable explicit unsafe maintenance mode. Required health-check absence must fail; optional checks should be `not_checked`, never healthy. Consume structured test outcomes and require the release's intended native cases, with explicit accepted skips; do not freeze an old magic pass count.

**Oracle:** the combined dry-run/rollback flags leave a populated fixture unchanged; default mutation cannot bypass prerequisites; all skipped, empty output, selected subsets, missing doctor and accepted documented skips produce distinct truthful results. `$PY -m pytest tests/test_upgrade.py -q` after adding these cases. Current tests cover ordinary dry-run and endorse the unsafe `--only deploy`, not these boundaries.

**Cost:** CLI/result fields and test receipt parsing, no service/database/recall cost. Optional diagnostics may remain opt-in; default successful upgrade cannot silently mean “not checked.”


## PKG-6 — Setup, rebuild, configuration and doctor claims outrun source

Status: confirmed for linked reproduced subclaims
Severity: P1/P2 by linked finding
Recommendation: fix claims with their underlying contracts

Parent reproduction command: `No parent reproduction; use the source locations and proposed regression oracle below.`
Observed result/receipt: Rebuild argv, empty-JSON precedence and malformed doctor state are reproduced in R2, INT-03 and INT-04. Remaining setup/version claims are source-reviewed; this is not an additional independent bug count.

Exact future regression command: `REVIEW_TASKS=256 run_review future-pkg-6 pytest -q "$WT/tests/test_engine.py" "$WT/tests/test_cli.py" "$WT/tests/test_provider.py" "$WT/tests/test_host_contract.py"`
Compatibility/recovery: Keep historical evidence dated; do not transform source analysis or mocked setup output into an end-to-end installation claim.
Alternative / keep-versus-change decision: Correct documentation alongside narrow fixes; do not paper over functional contradictions.

### Source rationale, invariant, blind spot, minimal change, regression oracle and cost

**Priority P1 for rebuild/configuration correctness; P2 for diagnostics; status: source-only gaps, coordinate with parent's runtime findings.**

- Setup calls **`ensure_engine`**, not `install_engine` (`engine.py:274–311`). It may still persist provider settings/activation when the engine is missing. The README runs setup before explicit install (`README.md:23–30`); missing-engine setup cannot write a generated `zg_bin`, and explicit install only lays out the runtime (`engine.py:227–249`, `cli.py:217–231`). The provider still defaults to PATH `zg` (`__init__.py:185–189`). **Do not simply reverse these commands:** the current host exposes provider CLI commands only for the active provider (`host plugins/memory/__init__.py:491–529`). The bootstrap needs an explicit not-ready state followed by installation and configuration/readiness completion; making explicit install persist its generated launcher, or making setup perform an explicitly authorized install, are narrower fixes than inventing a new installer. A second setup after install is a candidate workaround, not an exercised recipe here. Setup also replaces an existing `zg_bin` when it generates one (`300–303`), so “preserving every value” is false.
- Explicit npm install checks `found == requested version` (`227–249`), but `ensure_engine` only requires the entry file; it reads/reports the package version without matching a pin (`175–224`). `maintenance.md:60–62` incorrectly attributes version-match refusal to ensure. The default npm pin is an installation selection, not continuous attestation or proof against later tampering.
- `reindex` creates a durable marker (`cli.py:240–260`); initialization deletes it before job success and queues a normal forced scheduling request (`__init__.py:249–255,346–357`). Both identity mismatch and marker paths use `_maybe_reindex(force=True)`, while the eventual argv has **no `--rebuild` and no new embedding selection** (`308–325,873–892`; embedding is added only by initial `_build_index`, `828–830`). “Forces a full rebuild on embedding/binary change” is not supported by this path. No native behavior was exercised here; the parent should prove the exact engine contract rather than infer it from the Python `force` parameter.
- Provider JSON is authoritative even for `{}` and rejects wrong top-level types (`__init__.py:122–134`); CLI falls back to YAML for empty/invalid JSON (`cli.py:174–195`). Doctor can inspect a different vault/config than the provider. Its malformed map handler leaves `mirror_pending=None`, which passes the mirror predicate; valid JSON of the wrong shape can raise (`cli.py:104–113,146–150`). `identity` means absence of a delivery-failed marker, not verification of the engine-state sidecar. `tasks` also passes on an unknown ceiling (`151–154`). Eight checks are not a broad integrity/readiness/recall guarantee.

**Minimal fix:** correct the bootstrap/readiness claim without reversing commands that the host has not exposed; choose explicit engine-version policy without breaking deliberate `--version` overrides; share small strict configuration/state readers without importing/initializing the whole provider in the CLI. Propagate an actual native full-rebuild request/model only where required and retain recovery intent until acknowledged success. Use explicit unhealthy/unknown states for malformed data and missing evidence. Parent owns the detailed runtime patch recommendation, not this report.

**Coverage boundary:** `tests/test_provider.py:51–75` asserts marker removal and a mocked `_maybe_reindex(force=True)` call, not native argv, model replacement or successful index publication. Its inspected collection imports (`1–36`) do not add a HOME guard to the fixtures cited in PKG-1.

**Oracle:** missing/wrong-version engine setup cannot claim ready activation; custom `zg_bin` preservation is intentional and tested; provider/CLI agree for `{}`, invalid JSON and wrong types; malformed mirror state is unhealthy without traceback; reindex/embedding-change native receipt proves actual rebuild/new model and survives interruption. Future commands: `$PY -m pytest tests/test_engine.py tests/test_cli.py tests/test_durable_recovery.py -q`, followed by the parent's isolated native rebuild probe. No passing result is asserted here.

**Cost:** focused existing functions/tests and existing marker/state, no new service/model/database or broad `__init__.py` rewrite. Cheap CLI/schema loading and explicit local model selection should remain. Documentation-only correction is acceptable for an intentionally weaker feature, not as proof of the advertised rebuild.


## PKG-7 — Catalog eligibility and validation evidence are stale

Status: confirmed source/reference mismatch
Severity: P3
Recommendation: correct maintenance/catalog documentation

Parent reproduction command: `No parent reproduction; use the source locations and proposed regression oracle below.`
Observed result/receipt: No new parent runtime reproduction. This remains source-only evidence, not an executed failure.

Exact future regression command: `REVIEW_TASKS=256 run_review future-pkg-7 pytest -q "$WT/tests/test_engine.py"`
Compatibility/recovery: The removed plugin pin-age gate must not be conflated with Hermes dependency quarantine. Historical validation remains historical; a submission still requires fresh applicable validation.
Alternative / keep-versus-change decision: No catalog submission, release or version change is authorized by this review.

### Source rationale, invariant, blind spot, minimal change, regression oracle and cost

**Priority P3; status: confirmed current-reference mismatch; documentation fix next, no submission.**

`docs/maintenance.md:147–150` and `docs/catalog-submission.md:25,70–80` still require a two-week-old pin. Current reference `plugin-catalog/README.md:8–66` contains no such commit-age admission rule. Its **14-day quarantine concerns Hermes's own dependencies**, explicitly not a plugin pin-age gate (`57–66`). Current public-source submission docs list owner/public/released/validation/no-self-updating conditions (`website/docs/user-guide/features/plugin-catalog.md:264–291`). Remove the age gate rather than merely update its eligibility date.

CI still has structural and pinned-source jobs, but pinned validation now uses `hermes plugins validate --install-deps`, the install scanner, and a self-updater check (`.github/workflows/plugin-catalog-ci.yml:37–44,111–141`; `plugin_validate.py:563–577`). Historical “10/10, zero warnings” and “identical to CI” do not establish today's gate. The manifest probe records tools/hooks/middleware, while other real context methods—including memory-provider registration—are accepted as no-ops (`plugin_validate.py:185–245,373–423`). A green capability probe is **not** evidence that the provider's returned tools or lifecycle hooks were exercised.

**Minimal fix:** retain the dated 2026-09-11 check as history; add an explicitly current source-revision checklist. Correct `platforms: []` in the old status row, which disagrees with the same document's linux/macos statement and current entry. Keep name `hermes-zvec-memory`, installed/provider name `zvec-memory`, `subdir: zvec-memory`, and the valid full pin. Consider `category: memory` (default is desktop) and a quoted `version: "0.2.0"` label for that pin (`host README:70–110`). Do not claim current ownership/publicity/blocklist/CI success from old authenticated receipts. Do not invent a minimum Hermes version; establish one through tests before declaring it.

The repo-level standalone upgrader is **not shipped in the selected plugin subdirectory**. Explicit npm engine dependency installation is not replacement of the plugin's own files. Do not mislabel either as a proven catalog self-updater violation.

**Oracle:** static link/checklist review against the recorded host revision; future admission validates the exact selected pin with both current gates and separately exercises actual memory-provider capabilities. Local pin format/tag/manifest checks above are the only new pin evidence here.

**Cost:** documentation and optional cosmetic metadata only; zero runtime cost. Keep historical results, not historical admission rules presented as current. No pin bump, release or publication is recommended until unresolved rollout findings are addressed.

## PKG-8 — Installed subdirectory omits the MIT notice

Status: confirmed source inventory gap
Severity: P2
Recommendation: include existing MIT notice in the shipped subtree

Parent reproduction command: `No parent reproduction; use the source locations and proposed regression oracle below.`
Observed result/receipt: No new parent runtime reproduction. This remains source-only evidence, not an executed failure.

Exact future regression command: `REVIEW_TASKS=256 run_review future-pkg-8 pytest -q "$WT/tests/test_upgrade.py"`
Compatibility/recovery: Copy the existing notice without relicensing or changing copyright, and keep any retained publish inventory aligned.
Alternative / keep-versus-change decision: One notice copy/check is sufficient; no packaging framework is needed. This review does not independently establish the engine's license attribution.

### Source rationale, invariant, blind spot, minimal change, regression oracle and cost

**Priority P2 distribution issue; status: source-only packaging gap; fix next.**

Root `LICENSE:12–13` requires retaining the copyright/permission notice in copies/substantial portions. `zvec-memory/` and `DEFAULT_PUBLISHED_FILES` contain no LICENSE. Both the documented `cp -R zvec-memory ...` and catalog `subdir: zvec-memory` select that subtree. The inspected host installer resolves and publishes the subdirectory, not the repository root (`plugins_cmd_install.py:315–327,383–395`). No install was performed here and the delegated publication implementation was not audited; the source package itself is plainly missing the notice it should carry.

**Minimal fix/oracle:** include the exact MIT notice in the shipped provider subtree, keeping root LICENSE authoritative, and include it in the shared maintenance inventory. Check the exported/install candidate contains the notice and that it matches root LICENSE; add the check to packaging tests (`$PY -m pytest tests/test_upgrade.py -q`). Do not change copyright or relicense anything. Engine Apache-2.0 attribution is an external claim; this provider's MIT file does not verify the engine's license. Add a version-pinned upstream license citation rather than treating that attribution as newly verified here.

**Cost:** one notice copy/check, no dependency/service/state/hot-path cost. A repository-root URL alone is not the conservative distribution choice when only a subtree is installed.


## H01 — Installed snapshot provenance is incorrectly required to be Git provenance

Status: confirmed
Severity: P2
Recommendation: fix before interpreting installed-host campaigns

Parent reproduction command: `run_review host-provenance stress --lane load --mode native --workers 1 --records 20 --shutdown-policy drain`
Observed result/receipt: Six baseline failures disappear in the separately labelled 78-test reference-host control. native-load-installed, faults-installed and corpus-100-installed stop before callbacks. The installed daemon worker completes 301.79 active seconds and a restart, then the coordinator fails solely at host Git provenance. recall-quality-installed-nZMY99 also stops at non-Git host provenance, before any quality samples; its numeric threshold failures are therefore not poor-recall evidence.

Exact future regression command: `REVIEW_TASKS=256 run_review future-h01 pytest -q "$WT/tests/test_stress_memory.py" "$WT/tests/test_soak_memory.py" "$WT/tests/test_measure_recall.py"`
Compatibility/recovery: Record a real Git SHA only for Git sources; use an explicit installed-snapshot identifier plus content fingerprints otherwise. Move provenance collection before workload launch.
Alternative / keep-versus-change decision: Do not fabricate a host SHA or substitute the reference checkout for the installed source. Reference-host controls remain separately labelled.

### Source rationale, invariant, blind spot, minimal change, regression oracle and cost

- **Status / severity:** source-only gap / **P2**, unavailable or misleading benchmark diagnostics. Address before native load is interpreted.
- **Invariant / impact:** the instrument must identify the host actually loaded without inventing a Git identity. Stress obtains `host_sha` before either lane (`scripts/stress_memory.py:689–711`); soak does so after `supervise` has completed (`scripts/soak_memory.py:727–739`). With the configured non-Git installed snapshot, stress never reaches the requested lane and soak can finish its work then be relabeled `preflight_exception:CalledProcessError`. The plan's non-Git observation, rather than a new host filesystem inspection, is the premise here.
- **Existing test blind spot:** stress CLI and mocked campaign tests use the ambient/default host and do not model this installed snapshot (`tests/test_stress_memory.py:36–88,131–146`); the soak runtime-main tests likewise do not establish non-Git host provenance. These are not fresh installed-host receipts.
- **Smallest proposed change:** in the two script `main` provenance blocks, collect host identity before workload launch; retain a genuine Git SHA when available and otherwise record an explicit installed-snapshot identity plus a content fingerprint of the loaded host contract. Do not use the reference checkout's SHA for the installed source, and do not silently change `HERMES_AGENT_DIR`. No production file change.
- **Future oracle / command:** stub Git's host lookup to fail for a temporary host containing a fixture contract; assert the selected host is unchanged, the fingerprint is recorded, and lane execution is not mislabeled or skipped solely for lacking Git. Add the equivalent late-soak test with `supervise` stubbed, no native process. `run_review h01 pytest -q "$WT/tests/test_stress_memory.py::test_installed_snapshot_provenance_without_git" "$WT/tests/test_soak_memory.py::test_installed_snapshot_provenance_before_workload"`.
- **Cost:** no dependencies/services/new durable format/plugin state; additive fields in existing private JSON, one cold-path file fingerprint. **Alternative:** keep and explicitly mark installed-host lanes blocked; acceptable for this no-fix review, not as evidence that those lanes ran. **Compatibility/rollback:** retain legacy SHA fields when valid; reverting the script change affects only measurement metadata. **Recommendation:** fix next after approval; parent must retain the original blocked attempt.


## H02 — Recall scoring accepts arbitrary uncited text when an echo header is absent

Status: confirmed for the tested boundary
Severity: P1 instrumentation
Recommendation: fix next

Parent reproduction command: `run_review recall-oracle pytest -q "$OUT/probes/test_review_instrument_oracles.py" -k uncited_query_echo`
Observed result/receipt: instrument-oracles: the uncited query-echo negative fixture fails because result_evidence returns the answer-bearing echo. This is synthetic parser input, not an observed engine response.

Exact future regression command: `REVIEW_TASKS=256 run_review future-h02 pytest -q "$WT/tests/test_measure_recall.py"`
Compatibility/recovery: Accept the observed native citation grammar, including metadata, while rejecting uncited input. Do not remove expected-value or source assertions to obtain green tests.
Alternative / keep-versus-change decision: Simple citation-aware parsing is enough; no evaluation service or model judge is proposed.

### Source rationale, invariant, blind spot, minimal change, regression oracle and cost

- **Status / severity:** source-only gap / **P1**, incorrect recall/rollout evidence.
- **Invariant / impact:** score actual retrieved hit bodies, not query text or arbitrary output. `scripts/measure_recall.py:109–113` strips input only when the literal `query groups (` is present; otherwise it returns the whole response. `run_set:126–141` then scores `expected in result_evidence(body)`. A response containing only an echoed answer-bearing query without that header can therefore count as a hit. This is a proposed parser-negative fixture, not an observed native-engine response.
- **Existing test blind spot:** `tests/test_measure_recall.py:50–55` covers only the known-header echo. The lifecycle fixture returns all fact values without citations (`91–92`), and `147–153` explicitly accepts uncited `known fact`, preserving the permissive fallback. By contrast, stress requires a ranked hit/citation (`scripts/stress_memory.py:277–290`; tests `149–170,835–848`).
- **Smallest proposed change:** make `result_evidence` require a recognized ranked-hit body/citation, retaining the actual supported native citation variants. Update recall doubles to emit realistic cited output rather than weakening the parser to satisfy them. No provider change.
- **Future oracle / command:** uncited answer text, bare query echo, alternate-header echo, and zero-hit output must not score; supported cited bodies must score; retrieval visibility remains independently capped. `run_review h02 pytest -q "$WT/tests/test_measure_recall.py::test_uncited_echo_never_scores" "$WT/tests/test_measure_recall.py::test_supported_cited_bodies_score"`.
- **Cost:** no dependencies/services/durable-format/plugin-state change; one small parsing check per benchmark response only. **Alternative:** document the fallback as a trusted-format assumption; rejected for a recall-quality gate. **Compatibility/rollback:** older free-text fixtures/results may stop scoring, intentionally; preserve their raw private evidence. **Recommendation:** fix next before publishing new recall scores.


## H03 — A supported-looking partial load/regression receipt can still export green

Status: confirmed for the tested boundary
Severity: P1 instrumentation
Recommendation: fix next

Parent reproduction command: `run_review export-oracle pytest -q "$OUT/probes/test_review_instrument_oracles.py" -k green_counts`
Observed result/receipt: instrument-oracles: summarize returns passed=true with equal declared positive callback counts but zero observed samples. The review exporter separately reconciles samples, JUnit totals and failures.

Exact future regression command: `REVIEW_TASKS=256 run_review future-h03 pytest -q "$WT/tests/test_report_stress.py"`
Compatibility/recovery: Reject incomplete successful schemas while retaining partial failed attempts; reconcile denominators with observed samples instead of top-level flags.
Alternative / keep-versus-change decision: Strengthen existing typed adapters; do not replace JSON receipts with a database or report service.

### Source rationale, invariant, blind spot, minimal change, regression oracle and cost

- **Status / severity:** source-only gap / **P1**, incorrect rollout evidence.
- **Invariant / impact:** a successful public verdict must require observed coverage, not only a top-level flag and equal declared counts. `scripts/report_stress.py:300–319` accepts the ordinary stress schema with `workers=[]`; `143–178` does not reconcile successful counts with actual samples. `error_counts:125–133` ignores regression iteration errors/counts, while `162–166` only checks an explicit nested `passed/pass=False`. `aggregate:365–371` then derives the aggregate verdict from these rows.
- **Source witness, not execution:** a load receipt with known lane/mode, `passed=true`, positive equal planned/successful callback counts, elapsed time, and an empty workers list satisfies those structural/count branches despite no samples. A regression envelope can similarly omit completed iteration/JUnit coverage. Authentic stress normally supplies those checks itself; the gap is the exporter's advertised rejection of incomplete or inconsistent successful input, not a claim that the real harness generated this witness.
- **Existing test blind spot:** `tests/test_report_stress.py:80–91,225–232` reject missing keys/shortfalls, not equal counts with missing samples or absent regression coverage. The generic `report()` fixture (`20–25`) omits much of the actual lane contract. Soak's stronger observed-metrics test (`155–160`) is worth preserving.
- **Smallest proposed change:** add lane-specific successful-receipt consistency checks in `adapt_report`/`summarize`: callback samples reconcile to counts; regression completed/planned iterations and nonempty, unskipped, failure-free JUnit counters reconcile. Keep partial failed envelopes as failures. Do not expand arbitrary field pass-through.
- **Future oracle / command:** feed the proposed inconsistent load and regression fixtures through `aggregate`; require rejection or a failed verdict, and keep partial failure exports visible. `run_review h03 pytest -q "$WT/tests/test_report_stress.py::test_success_counts_require_samples" "$WT/tests/test_report_stress.py::test_successful_regression_requires_complete_junit"`.
- **Cost:** no dependency/service/plugin state; validation of existing receipt data, linear in samples/iterations already traversed; no new durable format required. **Alternative:** trust every raw `passed` flag; rejected for a shareable evidence validator. **Compatibility/rollback:** incomplete historical successes must be marked unsupported/incomplete, not upgraded; version an adapter only if an authentic older schema requires it. **Recommendation:** fix next before relying on exported pass status.

## H04 — A clean checkout cannot reproduce campaign setup or its list test

Status: confirmed
Severity: P2 instrumentation
Recommendation: fix next

Parent reproduction command: `No parent reproduction; use the source locations and proposed regression oracle below.`
Observed result/receipt: Baseline test_run_campaign.py failures show the missing private planned.json dependency. The capture helper is also outside the tracked 51-file source inventory.

Exact future regression command: `REVIEW_TASKS=256 run_review future-h04 pytest -q "$WT/tests/test_run_campaign.py"`
Compatibility/recovery: Track small non-sensitive helpers/case definitions or a deterministic bootstrap; keep production snapshots and credentials private.
Alternative / keep-versus-change decision: Treat a clean-checkout provision test as the contract, not a manually prepared maintainer machine.

### Source rationale, invariant, blind spot, minimal change, regression oracle and cost

- **Status / severity:** source-only gap / **P2**, unavailable reproducibility.
- **Invariant / impact:** code and non-private workload definitions required to execute the instrument must be tracked or explicitly provisioned. `scripts/run_campaign.py:26,33,97–99,121–123,226,243–244` depends on `.test-tools/campaign/capture_limits.py`, `planned.json`, a baseline, and an existing manifest. The inspected capture helper is ignored; `tests/test_run_campaign.py:81–86` directly executes `--list` against ignored `planned.json`. Copying the helper for this review does not make the commit self-sufficient. `.venv`, native packages, model files, and the chosen host are additional external prerequisites, not source-only/native evidence.
- **Existing test blind spot:** command-assembly fixtures and local list execution presume a prepared working tree; there is no clean-checkout provisioning assertion (`tests/test_run_campaign.py:36–95`).
- **Smallest proposed change:** track the small capture helper and non-sensitive case definitions outside ignored output directories, or provide a tracked explicit bootstrap for them. Initialize only a new run manifest at a caller-selected private output directory. Keep real production snapshots/tokens/models/receipts untracked. Make the list test use tracked data or a temporary injected case path.
- **Future oracle / command:** a temporary clean source fixture with no `.test-tools/campaign` must list the documented cases without consulting production, and construct a command whose capture helper exists in tracked source. `run_review h04 pytest -q "$WT/tests/test_run_campaign.py::test_list_and_capture_dependency_from_clean_source"`.
- **Cost:** no new dependency/service/plugin state; moves small operational source/case definitions under version control, retaining the existing JSON format. **Alternative:** keep the private prepared environment but explicitly list every prerequisite; accepted for this review only. **Compatibility/rollback:** existing private artifacts remain untouched; old output-directory layout can be supported as an explicit option. **Recommendation:** fix next for reproducibility, not by committing private artifacts.


## H05 — Resource receipts record requested limits and counters, not effective limits

Status: confirmed; corrected in review-only instrument
Severity: P2 instrumentation
Recommendation: port the small evidence correction if the harness is retained

Parent reproduction command: `run_review effective-limits pytest -q "$OUT/probes/test_review_followups.py" -k effective_kernel_limits`
Observed result/receipt: followups-installed-red-V3qPHJ fails the effective-limit receipt check; followups-installed-controlled-8eGFeF and followups-complete-LRYoNq pass after the ignored review helper records memory.max, pids.max and cpu.max. Later kernel receipts confirm 2 GiB, two CPU quota units and 256 tasks. Original receipts remain unchanged.

Exact future regression command: `REVIEW_TASKS=256 run_review future-h05 pytest -q "$WT/tests/test_run_campaign.py" "$WT/tests/test_report_stress.py"`
Compatibility/recovery: Requested and effective values are distinct; retain old receipts as lacking that evidence. Final cleanup checks exact cgroup absence/populated state.
Alternative / keep-versus-change decision: Read existing kernel counters; no monitoring daemon. Swap-limit and finer time-correlation evidence are still separate gaps.

### Source rationale, invariant, blind spot, minimal change, regression oracle and cost

- **Status / severity:** source-only gap / **P2**, ambiguous capacity attribution.
- **Invariant / impact:** an applied resource envelope must be read from the workload's real cgroup. `scripts/run_campaign.py:115–135` records command constants as limits. The supplied `.test-tools/campaign/capture_limits.py:21–26` captures peaks/events/CPU, correctly labeled as including `ExecStopPost`, but does not read `memory.max`, `memory.swap.max`, `pids.max`, or `cpu.max`. `review-env.sh:47–50,68–71` requests limits and checks a derived cgroup path/file, not effective limit values. A nonempty counter dictionary alone passes the campaign's evidence-presence gate (`run_campaign.py:220–224`). `group_empty:108–112` checks direct members only, not recursive `cgroup.events populated`.
- **Existing test blind spot:** `tests/test_run_campaign.py:36–67` establishes argv and requested metadata only. Export tests preserve supplied numbers (`tests/test_report_stress.py:325–334`); they do not prove those numbers came from the kernel. Historical task ceilings in comments are not this review's measurements.
- **Smallest proposed change:** extend the existing capture receipt to include effective limit files and the actual unit `ControlGroup`; distinguish requested/applied values and fail the required-evidence gate on missing/mismatched values. Read recursive populated state for final emptiness when the cgroup remains. Preserve helper-inclusive peak semantics and raw task-denial counters; do not infer a particular failed allocation from a run-wide event total.
- **Future oracle / command:** use a fixture cgroup directory with limits deliberately different from requested values; missing/mismatched limits must be visibly failed/unverified, while counters remain present. A populated descendant must not count as an empty tree. `run_review h05 pytest -q "$WT/tests/test_run_campaign.py::test_applied_limits_not_requested_defaults" "$WT/tests/test_run_campaign.py::test_descendant_cgroup_population_prevents_cleanup_success"`. Parent separately owns real applied-resource receipts.
- **Cost:** no dependency/service/new durable format/plugin state; a few bounded cold/end-of-run file reads and additive private receipt fields. **Alternative:** retain requested-only limits and label applied limits unknown; acceptable only without capacity claims. **Compatibility/rollback:** old receipts remain historical/unknown-applied, never backfilled with guesses. **Recommendation:** fix next or supply an explicitly separate parent receipt before resource attribution.


## H06 — Post-spawn ownership failure and cleanup errors can strand owned children

Status: source-only gap
Severity: P2
Recommendation: add focused failure tests before expanding supervision

Parent reproduction command: `No parent reproduction; use the source locations and proposed regression oracle below.`
Observed result/receipt: No new parent runtime reproduction. This remains source-only evidence, not an executed failure.

Exact future regression command: `REVIEW_TASKS=256 run_review future-h06 pytest -q "$WT/tests/test_stress_memory.py" "$WT/tests/test_soak_memory.py" "$WT/tests/test_process_safety.py"`
Compatibility/recovery: Preserve exact process ownership, unreaped identity rules and outer cgroup containment; handle failures immediately after spawn and during cleanup without abandoning remaining children.
Alternative / keep-versus-change decision: Do not add another process manager. Existing bounded ownership primitives should be extended only where a regression demonstrates the gap.

### Source rationale, invariant, blind spot, minimal change, regression oracle and cost

- **Status / severity:** source-only gap / **P2**, degraded cleanup/diagnostics. The parent cgroup remains mandatory containment, so this is not evidence of a production process being targeted.
- **Invariant / impact:** every successful spawn needs exception-safe ownership, including identity acquisition. `scripts/stress_memory.py:477–490` calls `track_child(Popen(...))` before entering cleanup's `try`; `517–518` similarly appends only after tracking succeeds. A tracking exception leaves that live Popen outside the owned collection. In soak, `supervise:600–603,623–639` can have a child but no `ProcessTree`; cleanup then waits without signaling it. Separately, stress `cleanup_owned:451–473` catches only process disappearance: another signaling/FD/wait error can abort cleanup before other children/FDs are handled; `load_campaign:528–532` also stops on the first cleanup exception.
- **Existing test blind spot:** stress already tests real scan failure, disappearing identities, interrupted grace, final-scan-before-reap, descendant reaping and numeric-group avoidance (`tests/test_stress_memory.py:223–331,536–564`). Those do not inject failure immediately after Popen, or a non-ESRCH signal failure while other owned children remain. These source tests are strengths, not executed results here.
- **Smallest proposed change:** put immediate post-spawn tracking inside a guard retaining the direct unreaped Popen; on tracking failure, use that still-owned direct-child handle for bounded termination/reaping. Attempt cleanup of all other tracked identities even if one fails, recording unresolved cleanup explicitly. Do not add name-based discovery, numeric group fallback after reaping, a generic supervisor framework, or a claim that sampled descendants replace cgroups.
- **Future oracle / command:** fixture-child startup with injected identity-acquisition failure must reap the direct child; inject one signaling failure and require other owned children/FDs still get cleanup attempts; an unrelated same-UID sentinel must survive. `run_review h06 pytest -q "$WT/tests/test_stress_memory.py::test_post_spawn_tracking_failure_is_owned" "$WT/tests/test_stress_memory.py::test_cleanup_continues_after_signal_error" "$WT/tests/test_soak_memory.py::test_tree_creation_failure_reaps_worker"`.
- **Cost:** no dependency/service/durable format/persistent plugin state; small exception-safe ownership changes only around test subprocess lifetime, no memory-provider hot path. **Alternative:** rely on the outer unit's cleanup and label local ownership incomplete; acceptable containment for approved review runs, not a standalone cleanup guarantee. **Compatibility/rollback:** keep existing pidfd/start-time semantics and bounded deadlines; unresolved kernel cleanup stays failed. **Recommendation:** fix next with targeted fault tests, no broader process rewrite.


## H07 — Shutdown is gated, but its duration is not independently measured

Status: source-only gap
Severity: P2 measurement
Recommendation: record shutdown duration separately

Parent reproduction command: `No parent reproduction; use the source locations and proposed regression oracle below.`
Observed result/receipt: No new parent runtime reproduction. This remains source-only evidence, not an executed failure.

Exact future regression command: `REVIEW_TASKS=256 run_review future-h07 pytest -q "$WT/tests/test_stress_memory.py" "$WT/tests/test_report_stress.py"`
Compatibility/recovery: Keep admission, drain, shutdown and end-to-end timing distinct; immediate-exit and graceful-drain workloads remain different profiles.
Alternative / keep-versus-change decision: One timer/failure field is sufficient; do not infer shutdown time by subtracting unrelated aggregate durations.

### Source rationale, invariant, blind spot, minimal change, regression oracle and cost

- **Status / severity:** source-only gap / **P2**, missing shutdown-cost evidence.
- **Invariant / impact:** admission, drain, shutdown, and total elapsed time must remain distinct. Stress has separate admission/drain fields (`scripts/stress_memory.py:210–214,255–269`), but `close_provider:156–165` times neither `shutdown()` nor its failure. Its elapsed timer includes shutdown, so shutdown cost cannot be recovered from the named phases alone. `scripts/report_stress.py:182–184` exports only worker admission/drain summaries; registering `shutdown_seconds` in `CUSTOM_METRICS:79–84` does not produce a measurement.
- **Existing test blind spot:** `tests/test_stress_memory.py:567–629` checks when shutdown starts and models its grace, but never asserts a recorded shutdown duration. `tests/test_report_stress.py:299–309` covers admission/drain only.
- **Smallest proposed change:** time `close_provider` around the actual shutdown attempt in `finally`, preserving both duration and failure, and add the narrowly typed export field. If daemon/provider shutdown is separately reported for soak, keep those categories distinct rather than subtracting them from an aggregate elapsed time. Do not change shutdown policy or grace.
- **Future oracle / command:** a fixture clock with a deliberately slow or throwing shutdown must retain the shutdown duration and failure while leaving admission/drain unchanged. `run_review h07 pytest -q "$WT/tests/test_stress_memory.py::test_shutdown_duration_survives_failure" "$WT/tests/test_report_stress.py::test_shutdown_timings_remain_distinct"`.
- **Cost:** no dependencies/services/new durable format/plugin state; two clock reads per shutdown and an additive receipt/export metric. **Alternative:** mark shutdown latency unmeasured; accepted for existing receipts, not as completed task-23 timing coverage. **Compatibility/rollback:** old rows report missing, not zero; no provider behavior change. **Recommendation:** fix next if shutdown cost is to be reported; otherwise explicitly defer that measurement.

## H08 — Campaign supervisor failures can bypass attempt persistence

Status: source-only gap
Severity: P2 instrumentation
Recommendation: fix next

Parent reproduction command: `No parent reproduction; use the source locations and proposed regression oracle below.`
Observed result/receipt: No new parent runtime reproduction. This remains source-only evidence, not an executed failure.

Exact future regression command: `REVIEW_TASKS=256 run_review future-h08 pytest -q "$WT/tests/test_run_campaign.py"`
Compatibility/recovery: Persist started/failed attempt identity before launch and finalize it even when cleanup/parsing fails; retain incomplete failure coverage.
Alternative / keep-versus-change decision: Keep atomic JSON/manifest files rather than add durable infrastructure.

### Source rationale, invariant, blind spot, minimal change, regression oracle and cost

- **Status / severity:** source-only gap / **P2**, failed/unfinished attempt accounting.
- **Invariant / impact:** preserve every started attempt even when supervision, cleanup, or diagnostics fail. `scripts/run_campaign.py:186–208` catches command timeout but not arbitrary launch failure; a timeout from the cleanup stop call escapes its `finally`. Log parsing, report-containment checks, and limits JSON parsing (`209–220`) occur before the first atomic attempt write (`225`); malformed limits can therefore leave a private log without a failed attempt in the manifest. Manifest initialization/read failure (`226–234`) can omit even a persisted attempt from exported coverage.
- **Existing test blind spot:** `tests/test_run_campaign.py:36–133` covers assembly, budget, snapshots, list and manifest-entry transformation, not the real `execute` finalization branches with injected failures.
- **Smallest proposed change:** persist a private started/failed envelope before launch and finalize it in an outer `finally`; catch and record each cleanup/read/parse failure without skipping remaining safety checks. Reconcile the manifest to the same attempt identity, never silently discard or retry it. Retain current exact-unit ownership and stop-on-failure behavior.
- **Future oracle / command:** stub launch/stop and fixture files; injected Popen/run `OSError`, stop timeout, corrupt limits JSON, and missing manifest must retain an attributable failed/unfinished attempt without calling production APIs. `run_review h08 pytest -q "$WT/tests/test_run_campaign.py::test_supervisor_failures_always_persist_attempt"`.
- **Cost:** no dependencies/services/plugin state; a started/final transition in existing private JSON receipts plus bounded error handling, no new storage engine or job scheduler. **Alternative:** manually reconcile orphan private logs for this review; acceptable only if listed as unfinished/failed rather than missing from totals. **Compatibility/rollback:** retain old receipt shape where possible; preserve both original and finalized attempt evidence. **Recommendation:** defer controller execution in this review as already planned; fix before reusing it for authoritative campaigns.


## H09 — Closed child environments are not a closed direct-worker contract

Status: source-only gap
Severity: P2 instrumentation
Recommendation: fix direct-worker environment contract

Parent reproduction command: `No parent reproduction; use the source locations and proposed regression oracle below.`
Observed result/receipt: No new parent runtime reproduction. This remains source-only evidence, not an executed failure.

Exact future regression command: `REVIEW_TASKS=256 run_review future-h09 pytest -q "$WT/tests/test_soak_memory.py" "$WT/tests/test_stress_memory.py"`
Compatibility/recovery: Reconstruct an allowlisted environment before imports for independently invoked workers. The executed daemon proof used a private user/network namespace with only its own loopback server.
Alternative / keep-versus-change decision: Retain the existing standalone worker model; no credential broker or network service is needed.

### Source rationale, invariant, blind spot, minimal change, regression oracle and cost

- **Status / severity:** source-only gap / **P2**, isolation portability and diagnostic reliability; the supplied namespace is still the required egress boundary.
- **Invariant / impact:** internal entry points must not rely on ambient credentials/loader state merely because HOME looks correct. Soak's internal branch checks the two home strings and existence of an owner marker, then calls `worker` without rebuilding `os.environ` (`scripts/soak_memory.py:691–699`); the fresh dictionary is passed to daemon subprocesses, while provider imports use the current worker process (`344–358`). A direct invocation with valid-looking homes can retain additional ambient variables. Contrast stress's marker/symlink validation and environment replacement (`scripts/stress_memory.py:665–680`; direct-invocation test `tests/test_stress_memory.py:481–493`). Both scripts' explicit allowlists also supersede some parent flags: soak omits offline/TMPDIR flags (`110–123`), and stress's nested pytest environment is not the parent wrapper's full environment.
- **Existing test blind spot:** environment-factory tests establish returned allowlists, not an independently invoked soak worker with poisoned ambient extras. The parent `env -i` and private namespace mitigate the intended review path; this finding does not assert that the approved wrapper leaked credentials.
- **Smallest proposed change:** validate the actual soak owner marker/run containment, reconstruct the worker environment before provider loading, and deliberately include only required safe offline/temp/test flags in the child allowlists. Prefer a closed original spawn; an in-process clear does not protect against loader effects before interpreter startup. Do not forward arbitrary `NODE_OPTIONS` or environment contents; keeping loopback transport working still requires the namespace, not a blanket Node network-denial preload.
- **Future oracle / command:** a direct internal-worker fixture with owned homes but poisoned credential/server variables must reject or remove the extras before `provider_factory`; missing/malformed markers must fail before import. Assert the chosen temp root stays inside the run and offline policy is explicit. `run_review h09 pytest -q "$WT/tests/test_soak_memory.py::test_direct_worker_rebuilds_environment_before_import" "$WT/tests/test_soak_memory.py::test_internal_owner_marker_is_validated"`.
- **Cost:** no dependencies/services/new durable format/plugin state; small launch/validation changes and a run-local temp directory, no per-memory-operation work. **Alternative:** document internal worker execution as valid only from the reviewed coordinator and require the external namespace; accepted temporarily for this parent's controlled execution, not as an independent entry-point guarantee. **Compatibility/rollback:** local-cache misses should fail explicitly when offline is required; do not silently permit downloads. **Recommendation:** fix next only within the harness isolation boundary; no product change.


## SERV-01 — Operational hold: on-disk service definition and loaded state

Status: confirmed observation; cause unknown.
Severity: P1 operational hold before deployment.
Recommendation: separate diagnosis before any production reload/restart or deployment.

The watched unit file contains only `[Service]` and `ExecStart=/usr/bin/true`. Read-only systemd inspection reports ActiveState=active, SubState=running, NeedDaemonReload=yes and TasksMax=1024. This anomaly predates the isolated review and its fingerprints remained unchanged. Do not attribute it to a past test merely because a test fixture uses similar bytes. No service reload, restart or repair was performed. A daemon-reload alone is not a restart, but blindly adopting the disk stub would change the definition used by subsequent starts.

Invariant/impact: deployed artifacts, manager configuration and the actual running engine must agree before rollout. Exact private snapshot evidence is retained in the initial/approved production snapshots and final-production.json. Reproduction is read-only: `systemctl --user show hermes-zvec-memory.service -p ActiveState -p SubState -p NeedDaemonReload -p TasksMax`, plus the unit file read. The smallest next action is diagnosis of the intended unit and loaded command, not a speculative repair in this review. Acceptance requires an explicitly approved restored definition, correct loaded/runtime identity and fresh health verification. Complexity: no new component or persistent format. Keeping it unchanged is accepted only during this non-deploying review; deployment stays gated.
