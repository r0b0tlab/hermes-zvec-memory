# zvec-memory provider

**0.3.0 candidate — UNRELEASED / NOT_READY / HOLD. Do not install or activate.**
This source-only candidate requires fresh independent review and complete release
gates; no installed or deployed revision is certified here.

Local-first Hermes memory over a Markdown vault indexed by zg. This standalone
provider does not modify Hermes core. Built-in MEMORY.md and USER.md continue to
work alongside it.

Runtime configuration: `$HERMES_HOME/zvec-memory/config.json`. If absent, the
legacy `plugins.zvec-memory` YAML block is read without modification. JSON is
authoritative once present. `vault` defaults to `$HERMES_HOME/zvec-memory`;
`zg_bin` can point to an absolute user-owned zg launcher. Default embedding:
`local/potion-retrieval-32m`. Auto-extraction is disabled by default.

Node >=22 is the declared minimum/runtime admission requirement, not verified
support across that range. The engine stays pinned to zg 0.2.2; the retained
ownership pilot reviewed only the exact Linux Node 26.8.1 boundary. Generic
Node>=22 and macOS qualification remain HOLD; Windows is unsupported. Hermes's
memory-provider interface and POSIX flock are required. These boundaries do not
narrow the full authorized release scope.

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

**Activation remains HOLD for this UNRELEASED / NOT_READY candidate.** The
compatible host's generic desktop configuration writer, cold-host/import closure
and full supported-host gates must be qualified first; retained offline tests do
not clear those requirements or authorize a CLI-only replacement release. Use
host-managed selection of an exact reviewed revision only after release and
separate maintenance/deployment authority; never replace live code under writers.
The repository's `docs/maintenance.md`, `docs/migration-0.3.0.md` and
`docs/release-0.3.0-validation.md` define the gated boundary, not a do-it-now recipe.

After those gates, activation uses `hermes memory setup` and takes effect in a
fresh session. Check
`hermes memory status`, then the configured engine's `zg status <vault>
--check-ready`. Native readiness alone does not prove a particular fact was
retrieved; check the cited source content.
