# 0.3.1-rc.1 — local production successor (unreleased)

This changed candidate is not published `v0.3.0` (`f28abfc`) and is not production
installation or cutover approval. Engine `@zvec/zvec-grep@0.2.2`, Node 26.8.1 and
`local/potion-retrieval-32m` remain pinned. All inherited reduced-core restrictions,
including stationary vault/ancestors, remain unless explicitly changed below.
No model/engine upgrades, general migration, backup API or desktop setup are added.

## Managed adapter and publication policy

The generated default-profile launcher routes **foreground `server run`** through
plugin-owned `managed-server.mjs` and `managed-policy.mjs`. `server on`, stdio and
unspecified bootstrap refuse instead of silently starting an upstream watcher.
Existing `server status`/`off` and ordinary index/query commands retain the upstream
CLI. The service uses mandatory bearer authentication and loopback transport.

This is a **private, exact-version/source-bound 0.2.2 adapter**, not a public upstream
API or compatibility promise. The shipped constructor's `watchManagerFactory`
seam is type-declared, but its modules are not package-exported public API. Before
SDK import, the entrypoint checks the exact package and eleven immediate module/
type digests. Those pins do not certify every transitive dependency. A future
engine release or changed source requires independent requalification.

A frozen publication-only sentinel replaces watcher construction without editing
the SDK or globally monkeypatching filesystem APIs. It does not register OS
watchers, change callbacks, reconciliation or resume timers. Search admits only
`autoUpdate:false` with `freshness:eventual` (provider `--refresh off`). Plugin
index jobs remain responsible for durable intent and generation publication.
The upstream `watcher_active` field describes sentinel registration, **not proof
of an OS watcher**; actual absence requires inotify/event/generation controls.

This is cooperative same-UID publication, not a security sandbox. A token-holding
operator can deliberately index/drop outside the provider; arbitrary Markdown
edits need explicit plugin reindex. Do not offer this endpoint as a general
background-indexing MCP service or restore wait/background refresh policies.

## Lifecycle boundary

Logger construction now occurs inside the acquired instance-lock cleanup region.
A process-local test loader injects failure after actual native lock acquisition
and verifies release; it changes no SDK/input files. This proves exception-boundary
cleanup, not an observed normal logger failure in production.

Shutdown deliberately preserves pinned upstream **backend close before HTTP close**.
The listener can still accept requests while backend teardown starts, and upstream
`DaemonHttpServer.close()` closes MCP endpoints before awaiting listener close.
Backend `shuttingDown` suppresses watcher creation, not universal HTTP admission.
This candidate does not claim an admission/drain barrier or arbitrary in-flight
request retention. Independent cleanup attempts improve failure handling but do
not turn service/provider shutdown into host quiescence. Production maintenance
requires host-writer closure and the parent's full closed-generation recovery gate.

## Read-only doctor and identity

Doctor validates the complete six-member artifact generation: launcher (0700),
unit (0600), manifest (0600), both modules and upstream Apache license (0600).
It rejects missing/changed/nonregular/symlink members, mode drift, and regular or
dangling `.new` conflicts without repairing them. The explicitly accepted legacy
manifest timestamp is retained, not regenerated. Loaded ExecStart remains checked.

Pending refresh/rebuild intent, impossible journal/inbox history and invalid live
mirror scope fail health. A present malformed/unsupported identity remains intact
and recall closed, including schema 0; it is **not** legacy absence or permission
to rebuild automatically. Missing legacy metadata and valid v0.2.1 bookkeeping
remain separate positives; valid-schema actual engine change rebuild and unknown
version nonthrashing remain covered. Doctor/status never perform repair.

## Acceptance and deployment limits

Combined private synthetic native controls and the unchanged applicable offline
argument vector are recorded in the local handoff. Historical failures and original
test sources are retained before the narrowly authorized fixture/schema/artifact
updates. No new scope exclusions are introduced.

Parent gates remain: independent frozen-source review, actual current Python3.14
host manager/launcher/setup provenance, production writer quiescence, closed full
recovery generation and retention/recall/performance comparison, installation and
service acceptance. No production files/processes/config/services were changed by
this integration. No remote push, release, catalog update or speedup is claimed.
