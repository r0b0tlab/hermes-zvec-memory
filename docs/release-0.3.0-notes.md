# Zvec Memory 0.3.0 — reduced-core release notes

This release retains the local memory core on one pinned configuration and
explicitly removes broader features from its supported contract. The published
annotated `v0.3.0` tag, its peeled commit and GitHub release object identify what
shipped; a local qualification or harness SHA is not publication. Resolve the
release commit with `git rev-list -n1 v0.3.0` after fetching that exact tag.
Production deployment, service repair and catalog submission are excluded.

## Supported target

- Linux 7.2.5-3-omarchy, POSIX file locking.
- Hermes source `7239625ae1b786827c952a8ace41fa6fd7f2eec5`.
- Node.js 26.8.1; native engine `@zvec/zvec-grep@0.2.2`.
- Local embedding `local/potion-retrieval-32m` (512-dimensional cosine).
- Vault and every ancestor directory remain stationary while active.

The historical Node >=22 admission floor is not qualification of that range.
A newer live Hermes checkout, another platform/model or a fresh network
bootstrap is not certified by these pinned-source isolated results.

## Retained behavior

memory_store persists private Markdown before acknowledging storage;
memory_search and prefetch provide bounded, source-cited retrieval. Built-in
memory remains separate. Committed add/replace/remove notifications use exact
ownership, durable pending work and ordinary journal/restart recovery. Native
index publication acknowledges only the captured successful generation; failed
or superseding work stays pending and stale recall is fenced.

The real CLI setup hook validates existing SDK/executable and complete artifacts,
writes canonical private JSON, and persists host selection through the actual
host writer with readback. Existing-SDK verification, repeated setup, truthful
status/doctor diagnostics and durable CLI reindex admission were exercised.
The actual admitted request was then consumed through the generated launcher by
the activated provider, followed by real store/search and a fresh-manager recall.
No SDK responses, activation writer, native manifest or readiness were forged.

## Unsupported entries

Desktop/F9 configuration, automatic migration, legacy mirror adoption, provider
backup/restore, embedding/model switching, broader platforms, bulk/multiwriter
capacity and the bespoke cold-bootstrap campaign are deferred. Unsupported
settings/version requests refuse before provider/layout/process side effects.
Generic configuration writing and backup/adoption APIs explicitly refuse;
`migrate-config` returns 2. Backup/restore CLI spellings were never implemented.

Strict atomic protection against noncooperating external directory renames is
not promised. Stop and drain owners before relocation. Ordinary path/symlink
integrity, privacy, durability, pending-work recovery and child cleanup remain
required. Removing mirrored facts is not erasure of old session logs, snapshots
or external backups.

## Executed evidence and limits

Integrated core offline qualification passed 2022 cases, retaining all 1891
previously passing neighbors. Original whole-scope results remain FAIL:
1891 passed, 133 failed and 46 errors. Every original nonpass has an explicit
disposition: 120 have executed supported-core safety/recovery successors,
57 are individually outside the approved scope, and 2 unchanged original cases
passed after charged prerequisite preparation. No mixed safety file is ignored.

Native evidence includes 26 functional cases, actual CLI dispatch and activated
native composition, and a 120-second restart/churn/removal soak: 9 cycles,
1 restart, 66 valid cited query observations, final mirror count 0 and no owned
leftover processes. The historical baseline had 8 cycles; this is not a
statistical speedup comparison. Historical 8x200 bulk convergence failed and is
not relabeled PASS. No long-term stability, leak freedom or broad capacity claim.

Runs retained the 2 GiB memory ceiling, zero swap, 256-task ceiling, 200% CPU quota,
closed network namespace and 300-second outer limit. CPU throttling was nonzero;
no claim of unconstrained performance is made. CLI doctor intentionally returned
unhealthy for an unloaded isolated managed service, not fake production health.

See [validation](release-0.3.0-validation.md),
[contract](reduced-core-contract.md) and [upgrade restrictions](migration-0.3.0.md).
Pre-reduction notes remain byte-preserved under `history/`; they are historical,
not active recipes, updated PASS claims or a reset of the original deadline.
