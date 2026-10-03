# Migration to 0.3.0 — unreleased candidate, NOT_READY

**SOURCE-ONLY. Do not install or activate this candidate.** The procedures below
are explicit operator requirements for a future independently reviewed,
authorized maintenance window. They are not commands executed by this packet or
permission to change production. Full supported-host compatibility, ownership,
native/resource/model/measurement and final release gates remain HOLD; see
[validation](release-0.3.0-validation.md).

## What changes from 0.2.1

The plugin manifest becomes `0.3.0`; the pinned native engine remains
`@zvec/zvec-grep@0.2.2`. Node.js >=22 is the declared minimum and runtime
admission requirement, not verified support across that range. The retained
ownership pilot reviewed only the exact Linux Node 26.8.1 / util-linux / UID1000 /
inode boundary; it does not qualify generic Node>=22 or macOS support, which
remain HOLD. The full authorized release scope is unchanged, not narrowed to
that pilot. Do not upgrade the engine or embedding as a side effect of installing
plugin code. Historical release data, fixtures and catalog pins keep their
original identifiers.

`$HERMES_HOME/zvec-memory/config.json` is authoritative whenever present,
including an empty object. Only absence permits fallback to the legacy
`plugins.zvec-memory` YAML block. Invalid JSON, non-object JSON or unreadable
present configuration stops the operation; do not delete it to recover legacy
values or save defaults over it. Zero, false, unknown fields and unrelated host
settings must be retained by a lossless migration. The actual CLI action is
`hermes zvec-memory migrate-config --json`; successful admission reports
`current` or `migrated`, not engine readiness, recall readiness or activation.
It does not fix the host's generic desktop writer. Until the compatible host
writer and cold-host gates are proved, a complete desktop-protection release
claim is blocked; do not silently substitute a CLI-only support promise.

Exact committed `previous_content` controls destructive mirror identity.
Ambiguous legacy substring notifications may remain pending and recall may
fail closed. Invalid/future mirror journals, wrong watermarks, symlink/alias
paths, interrupted mutations, delivery failures and newer rebuild requests
must remain evidence. Do not remove `.mirror-inbox.sqlite3`, `.mirror-map.json`,
`.mirror-staging`, `.mirror-delivery-failed.json`, `.reindex-request.json`,
`.index-refresh.json` or identity sidecars simply to make doctor green.
Reindex records intent; it is not evidence that a rebuild completed. A model
change requires explicit successful native rebuild and identity proof; an
approved second cached model is currently missing. No incidental download,
remote model, credential, endpoint or silent substitution is approved here.

## Future maintenance sequence — blocked until release and deployment gates

All-writer/build enrollment and the migration protocol are operator requirements,
not implemented or qualified enforcement. The checklist below is not a proven
safe live-upgrade warranty: actual caller enrollment, quiescence and coherent
recovery qualification remain HOLD. A lock or a request to stop writers does not
establish that every writer/build has enrolled or stopped.

1. Select the exact independently reviewed, gated plugin revision through the
   host plugin manager. There is no released 0.3.0 SHA to substitute now. Verify
   its manifest, installed subtree/LICENSE, native package, Node executable,
   model/cache identity and compatible host bytes. Publication is not deployment.
2. Obtain separate explicit authority for the precise default-profile maintenance
   window, stopped writers, artifact changes and service actions. Managed engine
   setup is default `~/.hermes` only; it refuses named/custom homes and customized
   `zg_bin` selection instead of writing global artifacts for another profile.
   Do not modify any other real profile or use an isolated rehearsal as consent
   for production writes.
3. Quiesce **all** processes sharing the vault: stop admission, flush pending host
   work, drain/shut down both provider workers, close SQLite readers/connections,
   release vault locks and quiesce native indexing. A mirror lock alone does not
   stop inbox admission. Preserve failed or pending work; do not treat its absence
   from a partial backup as successful completion.
4. Preserve a coherent recovery generation covering plugin/install metadata,
   host and provider configuration, launcher/unit/manifest, native package/model
   identity and complete durable vault state, including inbox and sidecars,
   ownership map, interrupted journal/staging, facts/sessions and rebuild/refresh
   obligations. Keep checksums/manifest and older recovery generations separately.
   Backup path discovery alone is not proof of a coherent or recoverable archive.
5. Rehearse cold restore at the same configured logical paths in a private isolated
   environment using the exact boundary below. Verify bytes, notification IDs,
   modes, ownership, pending obligations and exactly-once recovery against that
   generation. Rehearsal evidence is not production or native recall certification.
6. Only after those gates, install/update plugin code via the host manager at the
   approved exact SHA. Run lossless configuration migration in the quiescent
   boundary if needed. Scope-check and stage the entire managed artifact generation
   before any permitted mutation. `ensure_engine` accepts matching members and
   creates missing members only; edits, symlinks, wrong modes or differing members
   are conflicts, not a request to rewrite files or publish `.new` repairs.
7. Explicit engine installation is the only fetching path; setup verifies an
   existing runtime. A half-installed runtime, executable/package mismatch,
   invalid host destination, artifact conflict or activation persistence failure
   must remain visible. Setup's `recall_readiness: not_checked` is not a pass.
   Provider JSON may already exist if host selection persistence fails; do not
   label that multi-file atomic activation or retry by silently resetting config.
8. Reconcile complete disk/loaded service identity under separately approved
   service authority. Do not reload/restart opportunistically. SERV-01 repair is
   excluded/not certified by this packet. Finally verify installation hashes,
   real readiness, unauthenticated engine rejection and complete doctor/status
   coverage. Store an authorized synthetic fact in one fresh session and recall
   its paraphrase with a resolving citation in a second fresh session. A current
   conversation holding the old provider is not an upgrade test.

## Cold restore boundary and privacy

The accepted archive tests cover quiescent internal and external-under-HOME vaults
at their original logical paths under synthetic isolation. They do not certify
online snapshots, outside-HOME vault backup, arbitrary relocation, live restore,
service startup or native retrieval. An absolute archived vault path is not
rewritten merely by changing HERMES_HOME.

The selected host's ordinary importer does not restore archived private file
modes: umask 022 may create 0644 files; umask 077 does **not** repair an existing
0644 target. Require absent/new-empty owner-private destinations and trusted
ancestors before import. Refuse populated targets, symlinks, unexpected owners,
ACL grants/shared mounts or unsupported permission semantics. Pre-provision
private 0700 profile `backups/` and `backups/config/`, and vault `facts/`,
`sessions/`, `.mirror-staging/`, `.zvec-grep/`; archives omit empty directories.
Set process umask 077 **before** host CLI/bootstrap/import, not afterwards.

The inspected import action is `hermes import ZIPFILE --force` (`-f` alias);
there is no tested `--no-services` switch. Actual host scope protection must be
verified before using it: non-default HERMES_HOME and an existing default-install
marker under synthetic HOME, with service/subprocess attempts refused. Never
rehearse import into the live default home, unavailable external logical paths
or another real profile. Reject incomplete/skipped import and verify bytes and
permissions before provider activation. Use the full isolated recipe and
read-only preactivation verifier in [maintenance](maintenance.md#quiescent-backup-and-private-cold-restore-f9--int-08).
That linked procedure is a retained scoped proof, not a permission repair or
proof that stock default import is privacy-safe.

## Rollback / failed migration

`scripts/upgrade.py` is retired for every invocation, including `--dry-run`,
`--rollback`, `--only` and `--backup`: it returns 2 without subprocesses or file
mutation. No automatic rollback or partial historical updater archive is valid.

On failure, keep admission stopped and retain diagnostics plus the failed new
generation. Under separate exact recovery authority, restore **only the verified
coherent pre-maintenance generation** at its original logical paths, with private
permissions verified before activation. Restore configuration, code/runtime,
managed artifacts and durable obligations together; never mix old code with a
newer unsupported journal/index generation, erase newer queued work, or restore
unrelated YAML/service/profile state. An archive that has not passed isolated
recovery is not an available rollback merely because it exists. If coherence or
identity cannot be established, remain stopped and escalate; do not reload or
invent a fresh production baseline to hide failure. After successful authorized
recovery repeat fresh-session/citation and installed/loaded identity checks,
then separately record any approved baseline refresh while preserving history.

## Current disposition

No migration, install, native rebuild, download, backup/import, service action,
production configuration edit, other-profile action or remote publication was
performed in preparing this documentation. The frozen source packet is for
parent review/integration; recommendations do not confer execution authority.
