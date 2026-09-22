# Changelog

All notable changes to this plugin. Versions match `zvec-memory/plugin.yaml`;
the release tags point at the commits the plugin-catalog entry pins.

## Unreleased

- **The engine launcher resolves `node` at layout time.** `ensure_engine` no
  longer bakes a hardcoded `/usr/bin/node` into the generated launcher and
  manifest: `node_bin()` resolves the same way the installer already resolved
  npm — `HERMES_ZVEC_NODE` override, then `node` on PATH, then well-known
  install locations (Homebrew `/opt/homebrew/bin`, `/usr/local/bin`) for
  minimal-PATH contexts — all realpath-collapsed so a symlinked shim and its
  target yield identical bytes — then `/usr/bin/node` as the last resort.
  Machines without a system nodejs (such as Hermes' bundled Node under
  `~/.hermes/node`) no longer receive a launcher that cannot start.
- The stress-lane network-guard probe resolves `node` from PATH instead of
  assuming `/usr/bin/node`.
- **`upgrade.py --rollback` restores into `Context` paths.** The unit and
  profile-config targets are now `Context.unit_file` / `Context.config_file`
  fields (same defaults as before) instead of hardcoded `Path.home()` values,
  so a test or alternate profile cannot reach the developer's real unit or
  `config.yaml` through a rollback.
- The isolated test environment pins `httpx[socks]==0.28.1` (plus its new
  transitive pins) so the host-contract tests can import the Hermes modules
  behind `HERMES_AGENT_DIR`.

## 0.2.0 — 2026-09-11

First tagged release. It covers everything since the repository's initial
history on `main`: the native provider core, the persistence and recovery work,
the stress campaign that produced the measured envelope, and the maintenance
work that makes the provider installable, checkable and upgradable.

### Maintenance and operability

- **`hermes zvec-memory doctor|status|reindex|engine install`** — eight checks
  (config, vault, engine, index, inbox, mirror, identity, task ceiling) that fail
  closed; `--json` for machines; `engine install` is the single explicit step that
  fetches the pinned engine package.
- **The engine runtime installs itself.** `ZvecMemoryProvider.post_setup` (the
  `hermes memory setup` hook) generates the launcher and the systemd unit,
  writes the provider's `config.json` without disturbing user values, and owns
  activation. The generated launcher is byte-identical to the hand-verified one.
- **`scripts/upgrade.py`** — one gated command: verify clean tree → offline suite
  → native lane against the installed launcher → receipted backup → deploy →
  re-baseline → doctor, refusing to mutate after a failed gate, treating an
  identical install as a no-op, planning every mutation under `--dry-run`, and
  restoring the newest backup with `--rollback`.
- **The runtime path no longer imports Hermes internals.** `hermes_cli`,
  `tools.registry`, `utils` and `agent.context_compressor` imports are replaced by
  a vendored `zvec-memory/hostio.py` pinned to the host by an equivalence suite
  and a guarded import probe.
- **Durable formats are versioned.** An engine-identity sidecar adopts an existing
  index once and forces a rebuild when the engine binary or embedding changes;
  `.mirror-map.json` carries a `schema_version` that refuses a newer layout
  instead of misreading it.
- `docs/maintenance.md` documents ownership, the upgrade and rollback paths, the
  versioned formats, the recorded limits and troubleshooting.

### Provider behaviour

- Bounded, context-preserving FIFO worker for automatic work; index work coalesced
  independently of persistence; duplicate queued recall coalesced; empty recall
  successes cached; prefetch bounded and cache-generation invalidated on vault
  change; explicit search triggers a deferred index refresh.
- Index inputs restricted to fact and session Markdown; native index contention
  retried with bounded backoff and serialized across same-vault handles.
- Context budget enforced including heading and truncation marker; native JSON
  configuration used as the authority; malformed memory-tool input rejected;
  fact files allocated privately and collision-free; symlinked source directories
  rejected.
- Extraction fails closed when its safety filters cannot load.

### Persistence and recovery

- Process-safe mirror transactions with a journaled, reversible mirror:
  interrupted publication and deletion recover; legacy mirrors require an
  explicit ownership migration; the durable recall gate is honoured across
  provider handles; in-flight recall is discarded across mirror changes.
- Durable notification inbox with guarded WAL, short jittered writer
  reservations, and tolerance of a peer holding the journal-conversion lock.
- Cold backup paths resolve against the profile home.

### Tests, tooling and measurement

- Isolated finite memory stress runner, same-handle daemon soak, fault lanes, and
  privacy-allowlisted share reports with kernel cgroup metrics preserved.
- Campaign controller (`scripts/run_campaign.py`) with an explicit task budget,
  defaulting to the viable profile, gating on memory-relevant production
  invariants instead of config bytes, and re-baselining with a preserved history.
- Host-contract tests that exercise the real loader, manager and config writer;
  native lanes run only under explicit opt-in.
- Measured envelope, recorded limits and the two correctness gaps the campaign
  found are documented in `docs/stress-results.md`.

## 0.1.0 — 2026-09-03

Initial `zvec-memory` provider: local-first memory over a `zg`-indexed Markdown
vault with hybrid BM25/vector recall and cited file locations.
