<!-- Reduced-core scope supersedes the historical recipes below. -->
> For v0.3.0, [migration/upgrade restrictions](migration-0.3.0.md) and the
> [reduced-core contract](reduced-core-contract.md) are authoritative. Provider
> backup/restore, automatic migration and deployment are not qualified. The
> retained historical archive/restore recipes below are NOT current v0.3.0
> operator instructions or evidence that those features passed. Production
> service adoption/reload/restart always requires separate maintenance authority.

# Maintaining and upgrading the provider

> **0.3.0 candidate: UNRELEASED / SOURCE-ONLY / NOT_READY.** Full compatibility
> and release acceptance are HOLD. Nothing here authorizes production service,
> plugin/configuration or other-profile changes. Use
> [migration-0.3.0.md](migration-0.3.0.md) for the current evidence-preserving
> migration/rollback boundary and
> [validation](release-0.3.0-validation.md) for unresolved gates. Operational
> examples require separate future maintenance authorization.

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

The launcher, unit and runtime manifest are a **complete generated artifact
set**. Managed setup admits matching existing members and creates missing members
only after whole-set preflight. It refuses operator edits, symlinks, wrong modes
or differing content before mutation; it does not overwrite artifacts or write
`.new` conflict repairs. Preserve the generation and obtain separate maintenance
authority rather than editing files to hide a conflict.

## Daily checks

```sh
hermes zvec-memory doctor          # exit 0 when healthy
hermes zvec-memory status --json   # same checks, machine readable
hermes zvec-memory reindex         # rebuild the index on the next session
```

`doctor` requires all nine checks, with no missing/duplicate check treated as
healthy: `config`, `vault`, `engine`, `index`, `inbox`, `mirror`, `identity`,
`service` and `tasks`. Managed service diagnostics require known target/artifact
and loaded-unit state; a missing/unreadable managed service cannot be replaced
with the caller's unrelated task ceiling. Direct targets use the caller cgroup,
with explicit unlimited distinguished from unknown; finite ceilings must meet
512. Index admission uses `status VAULT --check-ready`; pending/corrupt durable
state and incomplete health observations fail closed. CLI action results report
request/admission separately from health and completion.

`reindex` writes `.reindex-request.json` into the vault; the next provider
session in that vault consumes it and forces a rebuild.

## Installing the engine

The runtime is pinned to one npm package version (`engine.PINNED_VERSION`) and is
fetched by one explicit command — setup hooks never reach the network:

```sh
hermes zvec-memory engine install                # pinned 0.2.2 only
```

An alternate `--version` requires separate explicit engine-migration authority;
plugin candidate0.3.0 does not authorize changing native package/model identity.
Service actions below require a reviewed coherent generation and separate
maintenance authority; SERV-01 is excluded/not certified by this work. Only
then, if approved, restart the service so it picks up the new launcher:

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

The 0.3.0 candidate is unreleased and has not been deployed by this work;
complete host/setup/recovery/native/resource/ownership/release gates remain HOLD.
SERV-01 production repair is excluded and not certified.

## Quiescent backup and private cold restore (F9 / INT-08)

**Stock host import is not privacy-safe by default (F9-SEC-01).** In the two
checked host revisions, an archived 0600 ordinary file restored to an absent
path is created with `0666 & ~umask`: a common 022 mask produces 0644 inbox and
fact files. The host does not restore their archived mode. Existing files keep
their old modes, so **077 does not repair an existing 0644 target**. Provider
initialization is too late to prevent exposure; do not use it as a permission
repair. This procedure is an explicit operator precondition, not a host fix or
an approval of default import. Do not chmod after import to conceal exposure.

### Snapshot boundary and retained generations

Stop admission from **every** host/provider process sharing the vault, not just
one session. Flush host work, drain and shut down both provider workers, close
every SQLite inbox connection/reader, release vault ownership, and quiesce all
native indexing owners before archiving. The mirror lock alone does not stop
independent inbox admission. Snapshot the `.mirror-inbox.sqlite3`,
`.mirror-map.json`, facts, pending `.mirror-staging` files, and other selected
provider state **together**, including a journal-committed interrupted
replacement. Preserve notification IDs/watermark and source bytes; keep the
original inbox filename. Online/cross-file live backup is not certified.

Use the real `hermes backup` archive in a private operator-selected location;
retain its hash, manifest and source generation alongside previous recovery
archives until a separately reviewed replacement is verified. Host backup
excludes its `backups/` history, so keep that retained history separately. A
path returned by `backup_paths()` alone is not a recovery proof. The host
excludes provider vaults outside HOME; those need a separately reviewed backup
procedure, not an assertion that this command preserved them.

### Fresh, private, empty-target recipe

Rehearse in an isolated, quiescent environment before replacing any live state.
Use the **same configured logical paths** as the archived generation. The tests
restore internal and outside-HERMES_HOME/inside-HOME vaults at their original
paths under a controlled synthetic HOME. They do not prove arbitrary relocation:
`_external/` members return under HOME and an absolute vault path in archived
configuration is **not** rewritten by changing HERMES_HOME. Refuse an external
restore unless its original configured path is isolated and available; never
let a rehearsal reach the live vault. No native readiness/retrieval, online
restore, service restart or production deployment is certified here.

Before import, require new/empty, owner-private targets and private trusted
ancestors. Refuse populated targets, symlinks, unexpected owners, ACL grants,
shared mounts or unsupported permission semantics. Keep the old generation
separate; this is not an in-place upgrade or permission repair. Archives omit
empty directories: provision private empty `facts`, `sessions`, `.mirror-staging`
and `.zvec-grep` directories in the vault and `backups` plus `backups/config`
in the profile before import. The host excludes its backup history from the
archive, and cold configuration loading can create those directories under the
recovery process's ambient mask. Pre-provision both levels explicitly; a mode
on only the final child does not make an automatically created parent private.

The real selected-host import command registrar's help was exercised in the
contained tests: `hermes import ZIPFILE --force` (`-f` alias). Full CLI startup
was not exercised by that help probe. Check `hermes import --help` for your
selected installation in the isolated environment. There is no tested
`--no-services` flag: the checked importers can install/start a gateway after
import. The contained proof uses their **actual scope guard**: a non-default
HERMES_HOME and an existing default-install marker under the synthetic HOME;
any attempted subprocess/service call fails the test. Require that same guard
and inspect the selected host before using the recipe. **Do not import into the
default home or an environment without that guard.** Archives containing other
profiles can also create wrapper scripts; those are outside this recipe.

The following shell fragment is for the already isolated environment only.
`restore_home`, `vault`, `archive` and `sandbox_home` must be explicit,
operator-verified absolute paths, with `restore_home` non-default, `vault`
matching the archived configuration, and targets absent before this fragment.
The existing synthetic default-install marker is outside the empty target;
do not create or modify a marker in a live profile. `hermes` must resolve to the
selected, unchanged host executable. The subshell changes only its process mask.

```sh
(
    set -eu
    umask 077                       # BEFORE any real host import / CLI bootstrap
    test "$restore_home" != "$sandbox_home/.hermes"
    test -f "$sandbox_home/.hermes/config.yaml"  # pre-existing isolated guard
    test ! -e "$restore_home" && test ! -L "$restore_home"
    # For an external vault it must also be absent, under this isolated HOME.
    test ! -e "$vault" && test ! -L "$vault"
    mkdir -m 700 -- "$restore_home"
    mkdir -m 700 -- "$restore_home/backups" "$restore_home/backups/config"
    mkdir -m 700 -- "$vault"
    mkdir -m 700 -- "$vault/facts" "$vault/sessions" \
        "$vault/.mirror-staging" "$vault/.zvec-grep"
    HOME="$sandbox_home" HERMES_HOME="$restore_home" \
        hermes import "$archive" --force
)
```

Do not activate the provider, query/index, run setup or start a gateway yet.
Check import output for incomplete/skipped state and compare the archive-selected
bytes/manifest, journal obligations and original notification IDs against the
retained source generation. The command's return value alone is not proof of
completeness. Then run this **read-only, preactivation POSIX permission check**
with a trusted Python interpreter. It covers the complete profile and vault,
including inbox/sidecars, facts, journal and any pending staging; unexpected
owners, symlinks, special objects and group/other permissions cause failure.
It does not assess ACLs or certify shared/network filesystems; those must have
been excluded before import.

<!-- f9-permission-verifier -->
```python
import os
from pathlib import Path
import stat
import sys

roots = [Path(os.path.abspath(value)) for value in sys.argv[1:]]
if len(roots) != 2:
    raise SystemExit("usage: verify-private.py HERMES_HOME VAULT")
for root in roots:
    if any(path.is_symlink() for path in (root, *root.parents)):
        raise SystemExit("STOP: symlink in restore target path")
    if not root.is_dir():
        raise SystemExit("STOP: missing restore directory")
    for path in (root, *sorted(root.rglob("*"))):
        info = path.lstat()
        if (not (stat.S_ISREG(info.st_mode) or stat.S_ISDIR(info.st_mode))
                or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) & 0o7077):
            raise SystemExit(f"STOP: non-private restore entry: {path}")
vault = roots[1]
for name in (".mirror-inbox.sqlite3", ".mirror-map.json"):
    if not (vault / name).is_file():
        raise SystemExit(f"STOP: missing durable state: {name}")
for name in ("facts", "sessions", ".mirror-staging", ".zvec-grep"):
    if not (vault / name).is_dir():
        raise SystemExit(f"STOP: missing private provider directory: {name}")
print("Preactivation POSIX permissions verified; recovery remains separate")
```
<!-- /f9-permission-verifier -->

Save the snippet outside the target as `verify-private.py` and run
`python3 verify-private.py "$restore_home" "$vault"`. **On failure, stop and
leave the target inactive**; preserve the failed receipt and imported generation
privately, investigate, and retry from a new empty target. Do not silently chmod
it and report a secure import. In an independently controlled recovery process,
only after bytes and privacy pass, cold-load the provider; verify pending
create/delete completion, preserved source facts and notification watermark,
empty staging/inbox, and idempotent second cold recovery before considering a
separate live replacement. Run the same complete-profile-and-vault permission
check after the first and second cold recovery; do not check only the vault.
The retained tests execute the exact published Python check at all three gates
and record both complete trees after each closed recovery. Recovery may use
its original ambient mask because the required empty directories were already
provisioned privately; that is not a general promise for later unrelated writes.

The contained offline-native test matrix preserves the original byte/ID/cold
recovery oracles in the secure 077 lane. It also verifies real 022 imports widen
0600 inbox/facts to 0644 and are rejected **before provider load**, and verifies
077 imports preserve pre-existing 0644 files after the fresh-target precondition
has refused them. These negative controls do not run recovery. The synthetic
HOME remains private, so they demonstrate a host mode defect without claiming
an actual disclosure or probing production permissions. F9 security acceptance
still requires parent replay and fresh independent review; default host restore
privacy and broader release gates remain unapproved.

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

`plugin-catalog-entry.yaml` retains a historical v0.2.0 pin; it is not a
v0.3.0 submission artifact. Current policy requires an exact reviewed release
SHA and **no self-updating code**, not a commit-age waiting period. Hermes's
own dependency quarantine is a separate policy.

Release publication does not authorize catalog submission. After separate
approval, prepare an entry for the verified released commit, align its version,
category and declared capabilities, and rerun the current structural and
pinned-source admission gates in an isolated validation environment. The
current authorities, historical-pin distinction and gate commands are in
[`docs/catalog-submission.md`](catalog-submission.md).
