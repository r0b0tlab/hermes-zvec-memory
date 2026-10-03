# zvec-memory provider

**0.3.0 reduced core.** Qualified on the exact Linux/Hermes/Node/engine/model
configuration documented by the repository's `docs/release-0.3.0-validation.md`.
A published, remotely verified tag identifies the release; publication does not
install or activate this code in production. Historical broader-feature notes
are superseded by `docs/reduced-core-contract.md`.

Local-first Hermes memory over a Markdown vault indexed by zg. This standalone
provider does not modify Hermes core. Built-in MEMORY.md and USER.md continue to
work alongside it.

Runtime configuration: `$HERMES_HOME/zvec-memory/config.json`. If absent, the
legacy `plugins.zvec-memory` YAML block is read without modification. JSON is
authoritative once present. `vault` defaults to `$HERMES_HOME/zvec-memory`;
`zg_bin` can point to an absolute user-owned zg launcher. The only supported
embedding is `local/potion-retrieval-32m`; model switching refuses before setup
or provider side effects. Auto-extraction is disabled by default.

The supported target is one pinned Linux/Hermes source tuple, Node 26.8.1,
zg 0.2.2 and local/potion-retrieval-32m. Node >=22 remains an admission floor,
not a broader support claim. Other platforms are deferred. The vault and all
ancestors must remain stationary while active; ordinary path/symlink, privacy,
integrity and child-cleanup requirements remain mandatory.

The provider tools are `memory_search` and `memory_store`. Recall is bounded and
best-effort; explicit store confirms the source file write, not immediate native
index visibility. Session records capture their originating ID before enqueue.

Mirror ownership uses `.mirror-map.json`, `.mirror-staging/`, and the process lock
`.mirror.lock`; critical notifications use `.mirror-inbox.sqlite3`. Do not delete
these independently to reset an error. Pending notifications/recovery gate
recall. A `.mirror-delivery-failed.json` marker requires reconciliation with the
canonical built-in memories after restoring storage health. Removing a mirrored
fact does not erase historical session logs or backups.

Install/setup details, test commands, benchmark methodology, and recovery caveats
are in the repository README:
https://github.com/r0b0tlab/hermes-zvec-memory

Desktop/F9 setup, automatic migration, provider backup/restore, model switching
and multiwriter capacity are unsupported. Declared/legacy desktop configuration
is disabled; the generic writer, backup declaration and legacy adoption API
explicitly refuse. Only the actual CLI post_setup hook writes supported setup
configuration. The vault and its ancestors must remain stationary while active.

Install through the host plugin manager at the exact verified release commit,
under separately approved maintenance authority. Do not replace live code under
writers. CLI setup requires the already provisioned pinned engine/model, verifies
real artifacts, writes private JSON and persists host activation; it does not
fetch or start/reload a service. Fresh network bootstrap is not qualified.

After setup, use a fresh session and check actual store/search citations. The
generated launcher can run directly without a daemon. An unloaded managed
service yields genuine unhealthy doctor diagnostics; a requested reindex is not
complete until the native consumer acknowledges it. Actual isolated CLI setup,
configured-launcher store/search, request consumption and fresh-session recall
were exercised. This is not a claim that a real production service was loaded,
healthy, changed or deployed.
