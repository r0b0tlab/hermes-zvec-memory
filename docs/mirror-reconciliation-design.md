# Owned built-in mirror reconciliation

## Experimental offline live-target primitive — controller integration REQUIRED

`scripts/live_mirror_reconcile.py` can prepare and then journal a reconciliation
against its input vault; unlike the detached command below, **commit changes that
vault**. This is a primitive, not a complete maintenance controller. It does not
stop, fence, census, restart, or check readiness of any service. In particular,
the separately developed code-only controller has no reconciliation hook and
restarts services on success: invoking this script *after* that controller exits
is unsafe. Integrate and test the primitive at the stopped-and-censused point of
an independent controller before any production use. Independent review of the
combined exact revision, complete restorable backup, runtime and reopen process
is still required. No hostile same-user/root or concurrent/manual writers,
foreign hosts, reboot, or filesystem failures are fenced by this code.

Within the **same uninterrupted outage**, `prepare` takes a fresh private snapshot,
checks source identity and every captured vault file against it, and emits a plan
containing private memory. An explicitly confirmed plan is required for `commit`;
it rechecks source, full captured file inventory and inbox bytes before opening
the target inbox, then compares source, map, owned facts, all fact bytes, empty
inbox and watermark against the snapshot. It archives old owned material, stages
new entry facts, and uses the existing provider journal and refresh obligation.
It verifies the resulting journal, owned and retained fact bytes, and source bytes
before returning. It does not rename the vault or replace unknown files. Opening
the target inbox may itself create or checkpoint SQLite sidecars even on refusal.

The snapshot preserves copied bytes, including hidden files, but is **not a full
restorable backup**: it omits transient lock/SHM, changes copied file modes, and
excludes state.db, plugin/config/runtime and other profile data. Capture and verify
an independent complete backup before the operation. In an error, keep admission
closed and inspect the durable journal and staging: a post-replace error is
ambiguous. The operator may recover forward via the native journal, or restore a
verified complete pre-cutover backup while all writers remain fenced; this tool does
neither automatically. Never roll back an old backup after reopening: fence and
preserve any newer generation first. Recall remains withheld until a real refresh
completes; fresh-session search and service readiness are separate gates. Failed
or interrupted commands must not trigger automatic restart.

Commands are deliberately not production recipes; `--offline-controller-active`
is an operator attestation, **not** evidence of an outage:

```sh
python scripts/live_mirror_reconcile.py prepare --profile /stopped/profile --vault /stopped/vault --snapshot /private/new-snapshot --offline-controller-active
python scripts/live_mirror_reconcile.py commit --snapshot /private/new-snapshot --confirm-plan-sha256 PLAN_HASH --offline-controller-active
```

## Detached offline operation

The provider still refuses online legacy migration. A separate experimental tool
can reconcile a **detached copy**, not a live vault. It cannot enforce or prove
that all Hermes source writers, delayed notification producers, or other vault
writers are stopped. Its snapshot is coherent only if the operator stops and
fences all of them throughout the copy; `--writers-stopped` is an attestation,
not a lock or evidence. Do not use the output for a live cutover without an
independently established fence and verified, restorable full-system backup.
Do not manually delete a delivery-failure marker or acknowledge a notification.

## Detached tool

Run with the Hermes host Python environment; set `HERMES_AGENT_DIR` to the host
source directory if it is not importable. Paths must be absolute and unaliased.

```sh
python scripts/offline_mirror_reconcile.py snapshot --profile /stopped/profile --vault /stopped/vault --destination /private/new-snapshot --writers-stopped
python scripts/offline_mirror_reconcile.py plan --snapshot /private/new-snapshot
python scripts/offline_mirror_reconcile.py apply --snapshot /private/new-snapshot --confirm-plan-sha256 PLAN_HASH --writers-stopped
```

Snapshot copies the profile's two built-in memory files and the vault to a new
private directory. It copies the inbox database and WAL while quiesced, then uses
SQLite backup and quick_check **on the copy**; it does not open the input DB.
The original database and WAL bytes are retained in `.original-inbox` in the
detached workspace, outside the vault's indexed scope.
Inputs and generated plans contain private memory; protect them accordingly.
Snapshot streams and independently verifies all vault files, including native
index and unknown files, except the transient root `.mirror.lock` and inbox
`-shm`. It permits at most 100,000 vault entries (files plus directories),
16 GiB of vault file bytes and 8 GiB per file, with 1 MiB copy blocks. The
private `.snapshot-inventory.json` records each copied source's byte size,
SHA-256 and pre-copy filesystem identity; its own maximum is 64 MiB, and the
marker binds its hash. Source identity is rechecked after copying. The inbox
database and WAL in this inventory describe the raw copy, retained under
`.original-inbox`; the operational inbox is then normalized using SQLite backup.
Allow additional space for that raw inbox, its SQLite backup, and receipts.
These checks detect ordinary copy drift; they do not replace the writer fence
or defend against a malicious same-user writer. Parsed facts, maps, and source
files retain their separate 64 MiB and 10,000-record limits. Snapshot rejects
symlinks and hard-linked files, and can leave a partial destination on failure.
Never mistake the copy for a complete backup of the profile, state.db or WAL,
plugins, skills, configuration or other system data.

`plan` identifies exact current source entries, the complete facts inventory,
map-owned facts, source/map/fact bytes and file identities, inbox watermark,
additions and retirements. It refuses
pending inbox, delivery, refresh, journal or staging work, ambiguous duplicates,
stale owned entries, path aliases and unrecognized ownership. It does not adopt
unowned lookalikes. `apply` recomputes the entire plan under the vault lock and
requires its exact hash; it archives old source, map, owned fact bytes, inbox and
plan under the copy's `.reconcile-archive`, stages replacements outside recall,
and commits through the existing mirror recovery journal. Ownership requires the
complete provider-written fact format, including category, mirror tag, timestamp
shape and exact body. Whole-file and whitespace-normalized single-entry records
are replaced by exact authoritative entries, then retired only after that commit;
their bytes are archived. Recall remains
withheld until a provider session completes a real index refresh. An interrupted
journal publication is recoverable by normal provider startup; uncommitted
orphan staging requires manual investigation. If refresh is pending, a new plan
is refused rather than treating it as successful idempotence.

The snapshot marker prevents accidental use on an ordinary live directory; it is
not a security boundary against a same-user actor able to alter files, nor does
the vault lock fence external source/notification producers. The tool does not
acknowledge pending notifications or clear failure markers, perform cutover,
restore state.db, or prove native index completeness. Reconciliation of changed
or vanished legacy entries and any pending notification remains unsupported.

## Problem and boundaries

Hermes parses entries by splitting on the exact `\n§\n` delimiter and stripping
nonempty parts. Its current manager sends `previous_content` for the exact
committed entry and forwards patches as whole-entry replacements. A map-owned
record may instead contain several entries from an older whole-file seed. Its
path ownership does not confer separate ownership of those entries. Explicit
facts, history, unowned lookalikes, and unsupported legacy adoption are outside
the operation; no substring match may authorize deletion.

The guard preserves a matching owned whole-file record, leaves its notification
pending and withholds recall. It does not recover missing entries or cover all
possible mismatches: malformed, altered, or unrecognized legacy content needs
inspection rather than inferred ownership.

## Full online/recovery requirements not yet implemented

* Require an explicit, offline, quiesced profile and vault: stop and fence all
  built-in writers and notification producers for the entire snapshot, commit,
  and verification. The Hermes store lock alone is insufficient: its write can
  commit and release the source lock before the manager appends the notification.
  A delayed pre-snapshot notification could then be applied to a post-snapshot
  projection. The provider vault lock does not fence these producers either.
* Preserve a consistent, private, restorable snapshot of the built-in files,
  map, inbox including WAL state, owned facts, and journal before changes. Verify
  ownership, exact paths, file identity and byte hashes; reject aliases, shared
  paths, duplicates, uncertain ownership, unreadable input, and drift. Do not
  infer deletion authority from content or filename alone.
* Bind the proposal to exact source bytes, inventory, map bytes, inbox watermark
  and pending payloads, with finite input limits. Show entry-level additions,
  retirements and ambiguities; require explicit confirmation of the exact plan.
  Compare each preexisting map-owned multi-entry record against the source and
  pending history. If a source entry has changed or vanished and its history
  cannot prove the relationship, refuse rather than guess.
* Implement journaled, recoverable publication: stage exact entry facts outside
  the index; durably retain pre-change legacy bytes outside the indexed scope;
  commit a complete map and recovery obligations before any indexed retirement;
  never overwrite a fact. Treat a post-rename exception as an ambiguous commit
  and inspect the durable journal before deciding whether to roll back or finish.
  Leave a durable refresh obligation and withhold recall until indexing succeeds.
* Reconcile the pending inbox only when the fenced source snapshot and exact
  notification history prove which events are already represented. Acknowledge
  only those identities, with a durable watermark; ambiguous events remain
  pending. Never clear an independent delivery-failure marker without separately
  proving the missing notification history is covered.
* Prove against isolated restored data, using the actual Hermes store and manager:
  exact source preservation and counts, add/patch/remove after reconciliation,
  unrelated facts/history, ambiguous and duplicate ownership, rollback or
  recovery at every fault boundary, concurrent contenders, stale proposal
  refusal, repeated idempotency, process restart, and refresh-gated recall.
  Prove that quiescence/fencing is enforced, not just documented. A fixture
  result is not evidence for a live profile or native search index.
