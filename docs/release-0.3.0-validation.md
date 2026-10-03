# Reduced-core 0.3.0 validation

The active release contract is [reduced-core-contract.md](reduced-core-contract.md),
not the older full-feature candidate table. Historical failures and unexecuted
source packets remain preserved; reducing scope is not relabeling them PASS.
Remote publication, final integration readback and production deployment are
separate boundaries. Deployment, real-profile/service changes and catalog
submission were not performed.

## Source and target identity

Product functionality was frozen and executed from
`2294f1afc559537e07be8e368b257549bf9e5ebb`, based on
`ad80db490dd5464686339be013f19032e1f10239`. Final integration adds byte-identical
already-tested regression sources, the explicit scope selector and release docs;
installed Python functionality/manifest do not change. Final Git release identity
must be read from the remotely verified annotated `v0.3.0` tag. This file cannot
contain the SHA of the commit that contains itself.

Pinned Hermes source: `7239625ae1b786827c952a8ace41fa6fd7f2eec5`, isolated clean
checkout, not the externally updated live installation. Linux 7.2.5-3-omarchy,
Node 26.8.1, zvec-grep 0.2.2, local/potion-retrieval-32m; model manifest is actual
512-dimensional cosine. Dependency-complete test Python was 3.11.16/pytest 8.4.2.
These results do not qualify other host versions/platforms/models or network
bootstrap. Product and test-harness identities are bound separately.

## Actual executed gates

| Gate | Actual result | Boundary |
| --- | --- | --- |
| Reduced early-refusal contract | 27 PASS | Matched original baseline failure controls retained |
| Original full offline suite | 1891 PASS, 133 FAIL, 46 ERROR | Whole-suite FAIL preserved; not renamed PASS |
| Supported-core successor/control slice | 135 PASS from 125 selector strings | Parametrized counts from JUnit, not planned-selector counts |
| Composed isolated qualification | 2015 PASS, 206 deselected | Retained all 1891 original passing neighbors; includes one ephemeral transport witness |
| Integrated release-candidate core suite | 2022 PASS, 207 deselected | Same neighbors/successors + 8 scope-selector tests; transport witness not shipped; 30 native-marker cases and 177 exact individual exclusions |
| Real host/provider lifecycle | 1 PASS | Actual MemoryManager/store/search, exact deletion, unowned preservation, sessions and shutdown; only deferred final backup oracle adapted |
| Actual two-process ordinary durability | 1 PASS | Two real writers with one committed write each; not bulk capacity |
| Native retrieval/privacy/refresh | 7 PASS | Hybrid/FTS/vector, cited sources, scope/Unicode/options, config exclusion, deletion and ordinary refresh locking |
| Native same-model rebuild/restart | 10 PASS | Actual native results/manifests plus faulted publication, supersession and cold recovery |
| Native identity/privacy/pending replay | 7 PASS | Exact ownership, malformed identity refusal, pending fail-closed state and real spawned replay; 2 old pre-B2 comparator parameters excluded |
| Actual framework CLI dispatch | 1 PASS | Real parser/discovery/handler, setup/activation/artifact readback, idempotence, existing SDK, status/doctor and durable reindex |
| Actual CLI-activated native composition | 1 PASS | Same real admitted request acknowledged, configured generated launcher, real store/search and source-cited recall after fresh-manager restart |
| Actual 120-second candidate soak | 1 PASS | 9 cycles, 1 restart, 66 valid query observations, removed corpus, final mirror=0, no owned PIDs |

Preparation-only runs and repeated cases are not added to unique functional
coverage totals. Offline fault fixtures use controlled doubles; native/CLI gates
use the real pinned host, engine/model, generated launcher and actual return
values. Actual CLI invocation qualifies source parser/discovery/handler dispatch,
not whole `main.main()` boot maintenance or the public installed launcher.
Existing-SDK install is not a fresh npm-fetch test.

CLI status returns 0 as informational output. Doctor returned 1 for genuine
unloaded-service diagnostics in isolation; no fake systemctl output or production
healthy-service claim. Reindex initially reported `requested`, `completed:false`;
a later real activated provider consumed that same request and rebuilt the index.

## Exact scope reconciliation

`tests/reduced_core_scope.json` records all 179 original unique nonpasses with
unchanged original-source hashes and exact nodeids: 120 require supported-model
ordinary safety/recovery successors, 57 are explicitly deferred, and 2 unchanged
original cases replay after proper prerequisites. All mandatory mappings resolve
to executed passing successors. No mixed original safety file is ignored; every
original passing neighbor remains selected. The original full-suite failure and
historical 8x200 convergence failure remain FAIL.

`scripts/core_test_scope.py` validates the complete canonical case matrix,
original source hashes and named successor presence, then emits literal pytest
argv as JSON. It never runs tests, installs/warms assets, or relaxes isolation.
Use the JSON as an argument array inside a resource-capped isolated test lane,
not shell evaluation. `${...}` in a nodeid is literal: shell quoting alone does
not stop systemd's argument expansion. The recorded runner successor adds only
`systemd-run --expand-environment=no`, with an actual RED/GREEN argv witness;
all previous caps, snapshots, namespace and cleanup guards are byte-identical.

Native prerequisites must be copied into owned homes inside the charged unit;
no shared cache mutation, synthetic SDK/model stamp, uncharged warming, cache
flush or cap increase is part of acceptance. The dedicated preparation helper
is not shipped as a recurring functional test. Original namespace/interpreter
and real-regression-selector cases were replayed unchanged after preparation.

## Resources, cleanup, review and evidence

Every guarded run used MemoryMax 2147483648, MemorySwapMax 0, TasksMax 256,
CPUQuota 200%, RuntimeMaxSec 300, control-group kill and a closed network namespace.
Production-before/after protected snapshots matched; exact owned units were
collected with MainPID 0 and empty cgroups. No production service action or
policy bypass was used. CPU throttling was nonzero (candidate soak:
nr_throttled=332, throttled_usec=84402291); memory/task/OOM counters and limits
must be reported separately. File-copy page-cache pressure is not throughput,
leak evidence or engine capacity. No long-term/statistical performance claim.

Independent post-execution source/receipt review found no concrete blocker in
frozen product, supported-core adaptation, 2015-case composition, 26 native
cases and scoped soak. It independently checked 202 source/receipt bindings,
62 native retrieval observations/416 numbered source lines and 66 soak
observations/484 numbered lines. Its verdict is scoped, not publication.
New integration/CLI/doc acceptance and exact remote publication are separate
final gates; their final source-bound results accompany the release.

Raw receipt locators under the private evidence root `zvec-memory-3.0/` include:

- `runs/core-release-candidate-integrated-composed-C4mEGc/`
- `runs/core-pinned-source-cli-first-PYiOog/`
- `runs/core-actual-cli-activated-native-composition-F1xoqB/`
- `runs/core-frozen-2294f1a-actual-soak120-3DZ38T/`
- `reduced-core-release/{integrated-release-candidate-offline-parent-verified.json,core-native-functional-26-parent-verified.json,actual-cli-native-composition-parent-verified.json,core-soak120-parent-verified.json}`
- `reduced-core-release/post-execution-review/{REPORT.md,verdict.json}`

These are private evidence locators, not public download URLs. No credentials,
connection strings, raw private facts/configuration or authentication logs are
included in this document. Sanitized release summaries may expose allowlisted
counts, scope, source hashes and resource metrics only. Earlier full-scope gate
reconciliation is preserved verbatim under `history/`, not silently cleared.
