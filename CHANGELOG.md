# Changelog

All notable changes to this plugin. Manifest versions identify source revisions;
only remotely verified published tags identify releases. Unreleased sections
below are not publication or deployment claims. Catalog pins retain the exact
historical revision they describe until separately approved updates.

## 0.3.0 — UNRELEASED / SOURCE-ONLY / NOT_READY

Packaging successor of accepted local composition `343392d`; not committed,
tagged, published, installed or activated by this work. Native engine remains
`@zvec/zvec-grep@0.2.2`; this is the plugin candidate version only.

- Accepted source includes authoritative mirror identity, generation-safe
  rebuild/refresh intent, cold authoritative settings, runtime/worker recovery,
  complete CLI health/selection diagnostics, conflict-safe default-only managed
  setup and private quiescent restore boundaries.
- Tracked measurement/attempt/retrieval/export controls and installed MIT notice
  are included. The standalone updater/rollback remains retired.
- Retained 2036-pass offline results per host are scoped base history, not fresh
  candidate certification. Full supported-host compatibility, real ownership,
  native/model/resource, remaining measurements and the reduced same-handle
  soak pair remain HOLD. No speedup, long-term stability or narrower release is
  claimed. SERV-01 production repair remains excluded/not certified.

See [candidate notes](docs/release-0.3.0-notes.md),
[gate reconciliation](docs/release-0.3.0-validation.md) and
[migration/rollback boundaries](docs/migration-0.3.0.md).

## 0.2.1 — 2026-09-26

Fixes from the 2026-09-26 maintenance audit; no change to recall or persistence
behaviour.

- **`hermes zvec-memory reindex` now actually requests a rebuild.** It writes
  `.reindex-request.json` into the vault and the provider consumes it at the
  next `initialize()`, forcing the rebuild, instead of only printing that a
  rebuild was requested. The CLI and provider share the file name and tests
  lock both constants together.
- **`doctor`'s `tasks` check reads the service unit, not the caller.** It now
  reads the effective `TasksMax` of `hermes-zvec-memory.service` via
  `systemctl --user show` (trusted only with `LoadState=loaded`) and falls back
  to the caller's cgroup when systemd has no such unit. Previously the check
  could pass on an unrelated shell scope while the unit's real ceiling was
  misconfigured.
- **Test environment:** `requirements-test.txt` pins `ruamel.yaml==0.18.17`,
  required by the host's current `hermes_yaml` config policy, and the
  host-contract coldness test no longer treats the host's own `skills/`
  bootstrap as a plugin side effect.

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
