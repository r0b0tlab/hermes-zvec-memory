# zvec-memory provider

Local-first Hermes memory over a Markdown vault indexed by zg. This standalone
provider does not modify Hermes core. Built-in MEMORY.md and USER.md continue to
work alongside it.

Runtime configuration: `$HERMES_HOME/zvec-memory/config.json`. If absent, the
legacy `plugins.zvec-memory` YAML block is read without modification. JSON is
authoritative once present. `vault` defaults to `$HERMES_HOME/zvec-memory`;
`zg_bin` can point to an absolute user-owned zg launcher. Default embedding:
`local/potion-retrieval-32m`. Auto-extraction is disabled by default.

Requires Node >=22, tested zg 0.2.2, Hermes's memory-provider interface, and POSIX
flock. Linux is tested; macOS has not been exercised; Windows is unsupported.

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

Activation uses `hermes memory setup` and takes effect in a fresh session. Check
`hermes memory status`, then the configured engine's `zg status <vault>
--check-ready`. Native readiness alone does not prove a particular fact was
retrieved; check the cited source content.
