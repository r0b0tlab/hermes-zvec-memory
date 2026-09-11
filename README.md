# hermes-zvec-memory

A local-first [Hermes Agent](https://github.com/NousResearch/hermes-agent)
memory provider backed by [zvec-grep](https://github.com/zvec-ai/zvec-grep).
Markdown facts and session notes are the source of truth; `zg` supplies hybrid
BM25/vector retrieval with cited file locations. Built-in MEMORY.md/USER.md
remain separate and available.

## Requirements

- Hermes with the memory-provider plugin interface.
- Node.js >=22 and `@zvec/zvec-grep@0.2.2` (the native engine version tested here).
- A local embedding model, default `local/potion-retrieval-32m`.
- POSIX filesystem locking (Linux tested; macOS not yet exercised). Windows is
  explicitly unsupported by the process-safe transaction layer.

Installing a plugin is not activating it. Only one external provider can be
active per Hermes profile. Do not modify Hermes core to install this provider.

## Installation

```sh
hermes plugins install https://github.com/<your-account>/hermes-zvec-memory#zvec-memory
hermes memory setup zvec-memory      # installs the engine runtime and activates it
hermes zvec-memory engine install    # the one explicit step that fetches the pinned engine
hermes zvec-memory doctor
```

`hermes memory setup` lays out the engine launcher and the systemd unit, writes
the provider's `config.json` and activates the provider. Start a fresh session
afterwards: the current conversation does not hot-swap its provider or tool
schemas. Only one external provider can be active per Hermes profile, and
installing a plugin is not activating it.

From a checkout, the same three commands work after copying `zvec-memory/` into
`$HERMES_HOME/plugins/`:

```sh
HERMES_TARGET="${HERMES_HOME:-$HOME/.hermes}"
test ! -e "$HERMES_TARGET/plugins/zvec-memory" &&
  cp -R zvec-memory "$HERMES_TARGET/plugins/zvec-memory"
hermes memory setup zvec-memory
```

Upgrading is one gated command — see `docs/maintenance.md`:

```sh
cd ~/hermes-zvec-memory && python scripts/upgrade.py --dry-run && python scripts/upgrade.py
```

Engine requirements for a manual install: Node.js >=22 and
`@zvec/zvec-grep@0.2.2`, a local embedding model (default
`local/potion-retrieval-32m`), and POSIX filesystem locking (Linux tested; macOS
not yet exercised; Windows unsupported by the process-safe transaction layer).
On systems where sharp attempts an unwanted build against global libvips, use
`SHARP_IGNORE_GLOBAL_LIBVIPS=1` for the npm install. Never install Python
dependencies into the operating system's Python.

Configure a stable absolute `zg_bin` path if zg is not on the Hermes process's
PATH. A test checkout is not a suitable permanent production dependency path.
Then use `hermes memory setup`, choose zvec-memory, and start a fresh session.
The current conversation does not hot-swap its provider or tool schemas.

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
The declared desktop panel and provider setup use the same JSON store.

If JSON is absent, legacy `plugins.zvec-memory` in config.yaml is read without
modifying it. The first provider `save_config` preserves legacy values while
writing JSON. Once JSON exists it is authoritative, even when empty: removed
JSON keys do not reappear from legacy YAML. Malformed configuration raises
instead of silently resetting user choices. Partial saves preserve other keys.
Use Hermes's config CLI for host settings such as `memory.provider`; never
hand-edit the host YAML as part of plugin installation.

`recall_limit` is bounded to 1–50. `context_chars` caps recall text including its
heading/truncation marker. `preview` is `none`, `short`, or `full`.
Changing embedding requires an explicit rebuild with the selected local model:

```sh
zg index --rebuild --embedding local/potion-retrieval-32m /absolute/vault
```

Do not use a remote model, credential, endpoint, or remote authorization grant
unless the user explicitly agrees to send vault/query text off-machine.
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
legacy ownership is not guessed. Existing add-only legacy mirror files require
an explicit operator-approved adoption through `migrate_legacy_mirrors(entries)`
with exact path, target, and original content, followed by validation.

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
checking the cited file content. `backup_paths()` resolves custom vaults without
initialization; Hermes itself decides which external locations are eligible for
backup. A backup is not proven until its contents are inspected.

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

These use synthetic data and temporary homes. Host-contract tests exercise the
real loader, manager, declared config writer, and cold backup discovery without
mocking the provider interface.

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
