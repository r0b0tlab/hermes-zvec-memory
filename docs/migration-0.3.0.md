# Upgrade restrictions for reduced-core 0.3.0

Publication does not authorize installing, activating, changing a service or
repairing production. This release was qualified in isolated source-bound homes,
not deployed into the real default profile or any other real profile.

## No automatic migration or qualified backup/restore

`hermes zvec-memory migrate-config` refuses with exit 2 before home/config/engine
access. Legacy mirror adoption and provider backup declaration raise explicit
NotImplementedError. Backup/restore CLI subcommands are not implemented. The
host's generic profile archive may contain provider files, but this release does
not certify a coherent provider backup/restore or an arbitrary relocation recipe.
Historical archive/restore instructions are retained as history only.

Present `$HERMES_HOME/zvec-memory/config.json` is authoritative, including `{}`.
Only absence permits read-only legacy YAML fallback. Malformed, unreadable or
unsupported present settings refuse; do not delete them to activate defaults.
Only `local/potion-retrieval-32m` and engine 0.2.2 are supported. Switching to
another model/version, including a local alternative, is not an upgrade path.

## Separately authorized operator maintenance boundary

1. Confirm the exact remotely verified release commit, pinned host/Node/engine/
   cached-model configuration, and the default-profile managed-setup restriction.
   Do not substitute a newer live host for the tested source tuple.
2. Obtain authority for the exact affected profile, stopped writers and any
   requested service actions. Stop admission, drain host/provider work, close
   workers and SQLite readers/connections, and quiesce native operations before
   replacing code. This document is not an implemented all-writer enrollment
   mechanism or a warranty that online replacement is safe.
3. Arrange a separately proven recovery procedure for the complete durable
   generation. This reduced release does not supply or certify backup/restore,
   migration or automatic rollback. Preserve pending obligations and failed
   receipts; a partial file copy is not a coherent recovery guarantee.
4. Use the host plugin manager at the exact approved 40-hex release revision.
   Never use retired `scripts/upgrade.py` or replace code beneath live owners.
   Keep native engine/model unchanged. Setup requires a pre-provisioned pinned
   engine; a fresh network download/bootstrap was not qualified here.
5. Run actual CLI setup. It verifies the engine and launcher/unit/manifest,
   refuses conflicting artifacts rather than silently overwriting them, reads
   private JSON back and publishes host activation. A successful setup does not
   claim recall readiness or start/reload a service. Partial JSON publication
   after a failed host write is not multi-file atomic activation.
6. Start a fresh Hermes session and verify real store/search with resolving
   source citations, then verify persistence in another fresh session. Reindex
   reports requested-not-completed until a later native consumer acknowledges
   the same intent. Generated launchers can run directly without a daemon;
   unloaded-service doctor diagnostics must not be called healthy service proof.
7. If a daemon/service change is separately authorized, reconcile disk and
   loaded definition, interpreter/runtime identity, private state and actual
   readiness/authentication separately. No production repair/reload/restart was
   carried out or certified by release qualification.

The vault and all ancestors must remain stationary while active. Draining owners
before relocation is mandatory; external concurrent-rename atomic safety is not
promised. Do not independently delete inbox/map/journal/staging/identity/request
artifacts to silence fail-closed state. Fact removal is not full historical erasure.

See [contract](reduced-core-contract.md) and [validation](release-0.3.0-validation.md).
The prior broader-scope migration/restore candidate document is preserved at
`history/pre-reduced-core-release-docs--migration-0.3.0.md`, not silently certified.
