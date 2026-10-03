# hermes-zvec-memory

> **0.3.0 reduced core.** Qualified on the exact Linux/Hermes/Node/engine/model
> tuple in [validation](docs/release-0.3.0-validation.md), with a mandatory
> stationary-vault restriction. This is not the historical full-feature release.
> Publication is a separate remote-readback gate; resolve the published tag
> before installation. Production deployment and service changes were not made.
> [Contract](docs/reduced-core-contract.md), [release notes](docs/release-0.3.0-notes.md)
> and [upgrade restrictions](docs/migration-0.3.0.md) define the active scope.
> Earlier candidate documents are preserved under `docs/history/`, not active
> installation or recovery instructions.

A local-first [Hermes Agent](https://github.com/NousResearch/hermes-agent)
memory provider backed by [zvec-grep](https://github.com/zvec-ai/zvec-grep).
Markdown facts and session notes are the source of truth; `zg` supplies hybrid
BM25/vector retrieval with cited file locations. Built-in MEMORY.md/USER.md
remain separate and available.

## Requirements

- The one pinned Linux/Hermes source tuple bound by final release validation.
- Node.js 26.8.1 and `@zvec/zvec-grep@0.2.2`.
- Only `local/potion-retrieval-32m`; model switching is refused.
- POSIX filesystem locking. Other platforms and broader Node versions are not
  qualified by this reduced release.
- The vault and all ancestor directories must remain stationary while active.

Installing a plugin is not activating it. Only one external provider can be
active per Hermes profile. Do not modify Hermes core to install this provider.

## Installation and setup boundary

Publication is not production deployment. Use an approved maintenance window;
never replace plugin code while any process owns the vault. Managed setup is
supported only for the default `~/.hermes` profile.

From a Git checkout with the verified `v0.3.0` tag, resolve its exact commit and
install through the host manager (not a live directory copy):

```sh
RELEASE_SHA="$(git rev-list -n1 v0.3.0)"
hermes plugins install 'https://github.com/r0b0tlab/hermes-zvec-memory#zvec-memory' --ref "$RELEASE_SHA"
hermes memory setup zvec-memory
hermes zvec-memory status --json
hermes zvec-memory doctor --json
```

The qualified setup prerequisite is an already provisioned, exact
`@zvec/zvec-grep@0.2.2` runtime in a stable user-owned prefix and the cached
`local/potion-retrieval-32m` model. Setup verifies the real package/executable,
creates and reads back its launcher/unit/manifest, writes private canonical
JSON and publishes `memory.provider`. It does not fetch dependencies or start,
reload or restart a service. A fresh network/bootstrap install is not covered
by the isolated qualification. Never use a test checkout as a production SDK.

The explicit fetching command is `hermes zvec-memory engine install`; custom
provider CLI discovery requires the provider already be selected. Do not assume
this command is exposed before initial selection/setup. Existing-SDK engine
verification was exercised; a fresh npm download was not.

Start a fresh Hermes session after setup. The generated launcher can run the
local engine directly when no daemon is present. If a managed daemon is wanted,
loading/starting its unit is a separately authorized operator service action,
not something performed by this release. An unloaded service legitimately makes
`doctor` unhealthy; `status` is informational and returns 0. Neither result
certifies production service health. Reindex returns durable **requested, not
completed** intent; verify a later session consumes it and cites real sources.

The standalone `scripts/upgrade.py` is retired: all invocation forms return 2
without running subprocesses or changing files. Use the host plugin manager at
an exact approved revision. Automatic migration, provider backup/restore and
historical standalone rollback recipes are unsupported; see
[upgrade restrictions](docs/migration-0.3.0.md).

## Configuration

Canonical file: `$HERMES_HOME/zvec-memory/config.json`.

```json
{
  "vault": "$HERMES_HOME/zvec-memory",
  "zg_bin": "zg",
  "embedding": "local/potion-retrieval-32m",
  "recall_limit": 5,
  "context_chars": 2000,
  "preview": "short",
  "auto_extract": false,
  "reindex_min_seconds": 600,
  "prefetch_cache_seconds": 120
}
```

Omit `vault` for the profile-local default. Relative vault paths are resolved
against the active Hermes home; both `$HERMES_HOME` and `${HERMES_HOME}` expand.
Only CLI setup is supported. Desktop/F9 configuration is not declared, and the
legacy generic configuration writer explicitly refuses before publication.

If JSON is absent, legacy `plugins.zvec-memory` in config.yaml is read without
modifying it. CLI setup preserves supported settings while writing JSON; there
is no automatic migration during provider startup. Once JSON exists it is
authoritative, even when empty. Malformed or unsupported selections refuse
instead of silently resetting user choices. `migrate-config` refuses with exit 2.
Use Hermes's config CLI for host settings such as `memory.provider`; never
hand-edit the host YAML as part of plugin installation.

`recall_limit` is bounded to 1–50. `context_chars` caps recall text including its
heading/truncation marker. `preview` is `none`, `short`, or `full`.
Changing embedding is unsupported in this release, including another local
model. Explicit null, empty, false, or remote selections also refuse. Omitting
the setting selects `local/potion-retrieval-32m`.
Model downloads require network; local retrieval itself needs no cloud account.

## Persistence and privacy

- `facts/`: explicitly stored facts and tracked built-in memory mirrors.
- `sessions/`: daily text logs; each record captures its originating session ID
  before asynchronous work. User/assistant portions are truncated to 1500
  characters each. This is not an archival transcript/checkpoint guarantee.
- `.mirror-map.json` and `.mirror-staging/`: mirror ownership/recovery state;
  these are not recall documents. Indexing is restricted to Markdown in facts
  and sessions, not the provider's JSON configuration.
- `.mirror-inbox.sqlite3`: durable FIFO for critical built-in notifications,
  using Python's stdlib SQLite. This is a delivery queue, not a replacement
  retrieval database. Pending notifications are replayed after restart.
- `.mirror.lock`: POSIX transaction lock shared by separate Hermes processes.
- `.mirror-delivery-failed.json`: fail-closed marker if notification persistence
  fails. Restore storage health and reconcile the built-in memories with their
  owned mirror records before removing this marker; do not blindly delete it
  to silence an error. Snapshot the inbox/map/facts together before repair.
- `.zvec-grep/`: rebuildable native index.

Facts use exclusive private file creation instead of content-derived colliding
names. Symlinked writer directories are rejected. Explicit memory_store returns
`stored` only after the file write succeeds; index visibility is asynchronous.
Automatic persistence uses a bounded FIFO and logs rejected work or incomplete
shutdown rather than claiming it was flushed.

Built-in add/replace/remove notifications affect only mapped mirror-owned facts.
A recovery journal protects interrupted mirror publication/deletion; recall is
withheld while required native index cleanup remains incomplete. Ambiguous
legacy ownership is not guessed. Legacy mirror adoption is deferred;
`migrate_legacy_mirrors(entries)` explicitly refuses before vault access.

Removing a mirrored fact is NOT full erasure. Old text may remain in session
history, backups, or snapshots. Do not promise a privacy deletion across those
stores. Automatic preference extraction is off by default, uses only user
messages, and refuses extraction if its compaction-safety filters are missing.
Pre-compress behavior remains best-effort API v1, not a durable API v2 checkpoint.

## Recall and indexing

The provider offers `memory_search` (hybrid, fts, vector, globs, limit) and
`memory_store` (content, category, tags). Queries are passed as explicit argv
values, including strings beginning with a dash; no shell evaluation is used.
Prefetch has a short timeout and fails open to no context; explicit tool errors
remain visible. Empty successful results are cached, errors are retried, and
writes/index changes invalidate stale prefetch results.

Index refresh is coalesced and debounced; failed runs retain dirty state.
After bounded immediate contention retries, later automatic prefetch or explicit
search reschedules dirty work with a one-second cooldown. Retry is demand-driven,
not a persistent periodic timer, and is suppressed during shutdown. The first
search following a
write may not see it yet: wait for a successful refresh before asserting that
new/deleted data is visible/absent. Native lock/lease contention is not success.

For lower warm-query latency, a local zg daemon keeps models loaded:

```sh
zg server on --listen 127.0.0.1:17999 --token-file /absolute/private/server.token
zg server status --check-ready
```

Create a private random token first and configure clients with the matching
`ZVEC_GREP_SERVER_TOKEN_FILE`. Use a dedicated `ZVEC_GREP_HOME` and loopback
address for isolation. An unauthenticated daemon is not the recommended default.
Supervise it with the OS user service manager for restart/login persistence.
Never start a second daemon on an occupied port or stop someone else's daemon.
Without a daemon, `--refresh background` falls back to no refresh in direct zg;
the provider's own index scheduling remains necessary.

## Validation

```sh
hermes memory status
hermes zvec-memory doctor          # exit 0 when healthy; --json for machines
zg status /absolute/vault --check-ready
```

Everyday maintenance (install, upgrade, rollback, versioned formats, recorded
limits and troubleshooting) is documented in [`docs/maintenance.md`](docs/maintenance.md).

In a fresh Hermes session, store a harmless fact and retrieve it by paraphrase,
checking the cited file content. Provider backup/restore is unsupported;
`backup_paths()` explicitly refuses without resolving or initializing a vault.
The host collector tolerates that refusal. This does not prevent the host's
generic profile archive from including profile-local files; such an archive is
not a qualified provider snapshot or restoration procedure.

Offline tests require a Hermes checkout on `HERMES_AGENT_DIR` and a separate
Python environment with the pinned test dependencies:

```sh
uv venv --python 3.11 .venv
uv pip install --python .venv/bin/python -r requirements-test.txt
HERMES_AGENT_DIR=/absolute/hermes-agent .venv/bin/python -m pytest tests/ -m 'not integration' -q
```

Native tests require explicit opt-in and a reusable local model cache:

```sh
HERMES_AGENT_DIR=/absolute/hermes-agent \
ZVEC_RUN_NATIVE=1 \
ZVEC_TEST_BIN=/absolute/zg \
ZVEC_TEST_MODEL_CACHE=/absolute/test-model-cache \
.venv/bin/python -m pytest tests/test_zg_native.py tests/test_provider_native.py -q
```

These use synthetic data and temporary homes. Historical full-scope tests are
preserved, including desktop/config, migration, backup, and model-switching
oracles. They are not all applicable to this core. Final validation must list
their explicit dispositions and execute the applicable core plus successor
CLI/setup/refusal coverage; an excluded or unexecuted case is not PASS.

## Measurement, not speed claims

```sh
HERMES_AGENT_DIR=/absolute/hermes-agent .venv/bin/python scripts/measure_recall.py \
  --zg-bin /absolute/zg --model-cache /absolute/test-model-cache \
  --output benchmark-results/recall.json
```

The script seeds before initialization, records exact versions/SHAs, fails on
native errors, and separately reports retrieved hit@5, visible hit@5, context
size, repeated-identical-query cache latency, and distinct-next-turn latency.
It cleans its temporary vault after workers stop. JSON output includes full
synthetic query evidence and failures.

Hermes queues the just-completed query, not a predicted next query. A repeated
cache hit is not evidence that all subsequent questions become faster. Initial
same-hardware comparison retained 10/10 near-wording and 8/10 paraphrase hybrid
recall in both baseline and hardened providers; no general speedup was shown.
See [review evidence](docs/review-results.md) for tested versions, limitations,
and actual deployment/benchmark results rather than extrapolating old numbers.

## License

MIT. Retrieval engine zvec-grep is Apache-2.0. This provider stays a standalone
plugin and does not patch Hermes core.
