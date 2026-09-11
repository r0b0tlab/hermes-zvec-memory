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
| Backups + receipts | `$HERMES_HOME/backups/zvec-upgrade-*` | `.test-tools/deploy_plugin.py` |

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
`tasks` (cgroup `pids.max` is at least 512).

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

```sh
cd ~/hermes-zvec-memory
git pull                       # or check out the revision you want
python scripts/upgrade.py --dry-run
python scripts/upgrade.py
```

The command runs these steps in order and stops at the first failure, leaving the
installed plugin untouched:

1. `verify` — the working tree must be clean; records `HEAD`.
2. `offline-suite` — `pytest tests/ -q -m 'not integration'` (expect ~400 passed).
3. `native-smoke` — `pytest tests/ -q -m integration` against the installed
   launcher (expect `9 passed, 1 skipped`).
4. `backup` — `.test-tools/deploy_plugin.py` writes `$HERMES_HOME/backups/zvec-upgrade-<stamp>/`
   with the previous plugin, `config.yaml`, the unit and the launcher, plus
   `deployment.json` and `ROLLBACK.txt`.
5. `deploy` — copies `zvec-memory/*.py`, `plugin.yaml`, `README.md` into the
   installed directory; prints `already current` when the bytes are identical.
6. `rebaseline` — re-records the production baseline through the campaign
   controller.
7. `doctor` — must exit 0.

Roll back with:

```sh
python scripts/upgrade.py --rollback                      # newest backup
python scripts/upgrade.py --rollback --backup ~/.hermes/backups/zvec-upgrade-2026-09-11_103922
```

A running Hermes session keeps its old provider instance until it ends; start a
fresh session after an upgrade or rollback.

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
