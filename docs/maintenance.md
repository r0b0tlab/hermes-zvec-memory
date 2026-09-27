# Maintaining and upgrading the provider

Everything below assumes this checkout (`~/hermes-zvec-memory`) is the source of
truth and the installed copy lives in `$HERMES_HOME/plugins/zvec-memory`.

## What owns what

| Thing | Path | Owner |
| --- | --- | --- |
| Provider code | `$HERMES_HOME/plugins/zvec-memory/*.py` | this repo (`zvec-memory/`) |
| Provider settings | `$HERMES_HOME/zvec-memory/config.json` | `engine.post_setup` / `save_config` |
| Vault (facts, sessions, index, inbox, mirror map) | `$HERMES_HOME/zvec-memory/` | the provider at runtime |
| Engine runtime | `~/.local/share/hermes-zvec-memory/runtime/` | `hermes zvec-memory engine install` |
| Engine launcher | `~/.local/share/hermes-zvec-memory/zg-default` | `engine.ensure_engine` (generated) |
| Engine state (index home, token) | `$HERMES_HOME/zvec-runtime/` | the engine |
| systemd unit | `~/.config/systemd/user/hermes-zvec-memory.service` | `engine.ensure_engine` (generated) |
| Recovery generation | operator-selected private archive | explicit approved, quiescent maintenance |

The launcher and the unit are **generated**. Do not hand-edit them: the next
`ensure_engine` run rewrites the launcher when it differs, and a hand-edited unit
is never overwritten — it is written next to the original as
`hermes-zvec-memory.service.new` and reported as `unit_status: conflict`.

## Daily checks

```sh
hermes zvec-memory doctor          # exit 0 when healthy
hermes zvec-memory status --json   # same checks, machine readable
hermes zvec-memory reindex         # rebuild the index on the next session
```

`doctor` checks eight things and fails closed on any of them: `config`, `vault`,
`engine` (launcher runs and reports a version), `index` (`--check-ready`),
`inbox` (journal mode `wal`, nothing pending), `mirror` (no pending creates or
deletes, no refresh required), `identity` (no delivery-failure marker) and
`tasks` (the service unit's effective `TasksMax` when systemd reports it loaded,
otherwise the caller's cgroup `pids.max`, is at least 512).

`reindex` writes `.reindex-request.json` into the vault; the next provider
session in that vault consumes it and forces a rebuild.

## Installing the engine

The runtime is pinned to one npm package version (`engine.PINNED_VERSION`) and is
fetched by one explicit command — setup hooks never reach the network:

```sh
hermes zvec-memory engine install                # pinned version
hermes zvec-memory engine install --version 0.2.3
```

Then restart the service so it picks up the new launcher:

```sh
systemctl --user daemon-reload
systemctl --user restart hermes-zvec-memory.service
hermes zvec-memory doctor
```

`ensure_engine` refuses to write anything when the runtime is missing or the
package version inside it does not match, so a half-installed engine cannot
produce a launcher that points at nothing. The npm step runs with
`SHARP_IGNORE_GLOBAL_LIBVIPS=1` so the prebuilt binaries are used instead of a
source build against a global libvips.

## Upgrading the plugin

`scripts/upgrade.py` is retired, including `--dry-run`, `--rollback`, `--only`,
and `--backup`. It returns 2 without touching files or invoking subprocesses.
Do not use ignored deployment helpers or restore a partial historical updater
backup over an active profile.

Use host-managed installation/update of an explicitly reviewed revision. Before
maintenance, verify the candidate in isolation and obtain approval for the exact
profile, runtime generation and service changes. Quiesce relevant writers before
claiming a coherent backup of configuration, plugin, launcher/unit/manifest,
runtime identity and durable data. Test that recovery generation in isolation;
a directory listed by `backup_paths()` alone is not proof of recoverability.

Publication and deployment are separate gates. A running service does not prove
its disk unit is safe to reload. Preserve and diagnose divergent definitions;
do not reload a known-bad unit just to dismiss a warning. Never automatically
refresh a production baseline as part of an update. A fresh session must store a
fact, and a second session must recall it by paraphrase with a resolving citation.

The 3.0 implementation is in progress; this branch has not been deployed and its
complete setup/recovery/native release gates are not yet certified.

## `hermes memory setup`

`hermes memory setup zvec-memory` calls `ZvecMemoryProvider.post_setup(hermes_home,
config)`. That hook installs the engine runtime, writes the provider's
`config.json` (preserving every value you set), and owns activation: it persists
`memory.provider: zvec-memory` and the `plugins.zvec-memory` block in
`config.yaml`. Re-running it is idempotent — files are only rewritten when their
content changes.

## Versioned durable formats

| Format | Constant | Behaviour when it changes |
| --- | --- | --- |
| Engine/embedding identity | `ENGINE_STATE_SCHEMA`, `.zvec-grep/.zvec-memory-state.json` | A recorded identity that no longer matches forces a full rebuild. An index with no recorded identity is adopted once (no surprise rebuild on upgrade). |
| Mirror map | `MIRROR_MAP_SCHEMA`, `.mirror-map.json` | A map written by a newer plugin is refused (`newer`) instead of being misread; a map without the field is treated as schema 1. |
| Mirror inbox | SQLite WAL | Opened read-only first; journal conversion is retried for 0.4 s when a peer holds the lock. |

## Recorded limits

- `TasksMax=1024` in the generated unit. Below ~150 the native engine aborts
  (`terminate called without an active exception`) instead of degrading; the
  measured peak during warm churn is 138–150 tasks.
- The stress harness defaults to `--tasks 256`; `128` is a proven non-viable
  budget for the native lanes (see `docs/stress-results.md`).
- `MemoryMax` is left at the user manager's default; `MemoryHigh=4G` is the knob
  to throttle before an OOM if the vault grows a lot.

## Troubleshooting

| Symptom | Check | Fix |
| --- | --- | --- |
| Recall goes dark, engine exits | `journalctl --user -u hermes-zvec-memory.service -n 50` for the abort signature | the service hit a task/thread ceiling — confirm `TasksMax=1024` and `systemctl --user restart` |
| `doctor` says `index not ready` | `hermes zvec-memory reindex`, then a fresh session | the engine rebuilt or the vault moved |
| `doctor` says `engine` FAIL | run the launcher by hand: `~/.local/share/hermes-zvec-memory/zg-default --version` | re-run `hermes zvec-memory engine install` |
| `unit_status: conflict` | `diff ~/.config/systemd/user/hermes-zvec-memory.service{.new,}` | keep your edit and re-apply ours, or accept ours |
| Provider not active | `hermes memory status` | `hermes memory setup zvec-memory` |

## Publishing to the plugin catalog

`plugin-catalog-entry.yaml` is the exact file the catalog PR adds. Its schema,
the admission rules and the two CI gates it must pass were checked against
`plugin-catalog/README.md`, the published plugin-catalog docs page and
`.github/workflows/plugin-catalog-ci.yml` in hermes-agent; the per-requirement
result, the verification commands and the submission steps are in
[`docs/catalog-submission.md`](catalog-submission.md).

The one requirement that is time-based: the pinned SHA must be **at least two
weeks old** at pin time. The current pin (`v0.2.0`, `36fdba7…`) becomes eligible
on 2026-09-25. Submit the PR on or after that date, and re-run both gates before
doing so.
