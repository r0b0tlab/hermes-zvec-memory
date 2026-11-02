# Offline recovery snapshots (standalone, opt-in)

`scripts/recovery_snapshot.py` creates a **raw scoped snapshot**, not a Hermes
provider backup, live restore, migration, deployment, or service-health proof. It
never discovers or reads the live home, credentials, vault, or runtime by default:
every input path must be supplied. The snapshot does **not** encrypt or sanitize
anything. The explicitly selected config, built-ins, vault, plugin, wiki and skills
can themselves contain secrets or private data; `--include-sensitive-data` is a
required acknowledgment, not a redaction switch. Do not select a broad home or
other directory unless every included file is intended. Manifest paths and names
are sensitive too. Use encrypted storage if confidentiality at rest is required.

## Preconditions and scope

* Stop **all** writers to every selected source and keep them stopped through the
  entire snapshot; `--writers-quiesced` is an operator assertion, not a lock or
  detection mechanism. SQLite backup captures a transactionally consistent
  individual DB (including committed WAL pages), but no cross-store atomic point
  in time is provided. A source inventory is captured before copying; the
  operator must keep the tree stationary and unchanged. SQLite never opens a
  source database or source sidecar: backup reads only a private temporary copy
  of captured evidence. Do not rename an active vault or its ancestors. Restart
  writers only after the command exits.
* Use explicit absolute source paths and an existing **user-owned mode 0700**
  destination parent. The new destination must not exist or overlap any source.
  Output dirs/files are private (0700/0600); the snapshot may still be readable by
  root or other processes of the same user. Sources may overlap each other, e.g.
  provider `config.json` inside the vault: both logical copies are included.
* Choose the exact built-in memory directory, zvec vault, installed plugin
  directory, Hermes config, zvec provider config and Hermes `state.db`. Both
  built-in `MEMORY.md` and `USER.md`, and plugin `__init__.py` and `plugin.yaml`,
  must be present as regular files. Optionally add one wiki
  directory and/or one skills directory. There is no automatic inclusion of
  environment files, token stores, other profile config, engine binaries, model
  caches, or services. Explicitly selected directories are copied recursively,
  including hidden files; they are not secret-filtered. Symlinks and special
  files encountered in selected trees are refused, including inside indices.
  The entire `.zvec-grep` tree is included, including identity and unknown files;
  its SQLite-named databases and their sidecars follow the same rules as the rest
  of the tree. SQLite `*.db`, `*.sqlite`, and `*.sqlite3` files within selected
  trees and the explicit state DB are backed up using SQLite's backup API. Their
  raw main-file bytes are first copied under `raw-database-evidence/`, using the
  logical source path. Files named like their associated
  `-wal`, `-shm`, and `-journal` are byte-copied **before any SQLite opens** to
  `sidecar-evidence/<logical source path>` and the manifest records each original
  logical path as `source_path` for both kinds of evidence. These raw evidence
  copies are never modified by SQLite. If a journal exists, or a WAL and SHM do
  not form a complete, structurally valid committed pair, creation refuses after
  saving the raw evidence; this deliberately includes orphan, stale, truncated,
  corrupt, and ambiguous sidecars, including arbitrary name collisions. WAL
  checksums and WAL-index structure are checked before copying the WAL into a
  private work directory; SQLite builds its own SHM there. This conservative
  policy may reject recoverable databases. It cannot authenticate a sidecar's
  provenance or prove cross-store consistency; the operator must supply the
  correct stationary set. Do not move sidecar evidence beside either
  database copy: SQLite may replay or otherwise interpret it. Unpaired
  sidecar-named files stay in their original logical locations. Other names are
  copied as ordinary files; handle
  SQLite databases with nonstandard extensions separately. This is not a
  byte-preserving filesystem image: permissions, ownership, timestamps, links,
  locks and ACLs are not retained.
* Ordinary regular lock files (including an index `LOCK`) are copied as bytes;
  OS lock ownership is not copied. This is safe only under the stated quiescence
  assumption, and restoring the bytes does not restore lock state. Non-regular
  lock entries are refused. A rollback journal is always refused, even if empty;
  inspect preserved evidence and resolve it separately rather than allowing
  SQLite to recover it. SQLite backup lock errors fail immediately, without polling.
* This tool assumes trusted paths and a private, stable filesystem with no
  concurrent same-user path substitution. It does not defend against a hostile
  same-UID process renaming ancestors or changing files during verification,
  creation or restore. It is not designed to consume untrusted snapshots; the
  digest is an integrity check, not a signature or proof of source authenticity.

## Capture and verify a fresh isolated restore

Substitute **explicit paths you have authority to read**; the example paths are
placeholders, not discovered defaults. The optional directories are omitted here.

```sh
python3 scripts/recovery_snapshot.py create \
  --builtins /path/to/builtin-memory --vault /path/to/zvec-vault \
  --plugin /path/to/installed/zvec-memory --hermes-config /path/to/config.yaml \
  --provider-config /path/to/zvec-config.json \
  --state-db /path/to/state.db --destination /private-parent/snapshot-001 \
  --writers-quiesced --include-sensitive-data
```

Record the printed `manifest_sha256=...` **separately in a trusted location**.
The manifest lists source paths, every saved directory and file, per-file SHA-256
and size, the backup method, and exclusions. If creation fails, the exclusively
created destination may remain incomplete; preserve or dispose of it manually
after inspection. A manifest's mere existence does not prove a capture completed.

Restore only to a new, isolated, non-live location under a private parent:

```sh
python3 scripts/recovery_snapshot.py restore \
  --snapshot /private-parent/snapshot-001 \
  --destination /private-parent/restore-test-001 \
  --manifest-sha256 THE_RECORDED_SHA256
```

Restore refuses pre-existing destinations, source/output overlap, bad manifest
hashes, missing/extra entries, symlinks and checksum mismatches; it also checks
SQLite `integrity_check` before and after copying, refuses sidecars adjacent to
a backup database, and rehashes restored files. Evidence remains separately
named in the restored directory, not placed alongside SQLite backups.
A failure after creating the destination can leave partial output. Never point
this command at a live profile, vault or service; it does not install the result
or reconcile absolute paths. Inspect the restored files and config before any
separately authorized recovery. The included native index is not certified as
current or usable; verify readiness or arrange a separately authorized rebuild
before relying on search. No rebuild is triggered by this tool. Format 1
snapshots remain restorable but omitted the index and associated sidecars;
those missing bytes cannot be recovered from such snapshots.

## Standalone fixture tests

`tests/test_recovery_snapshot.py` exercises the independent CLI with synthetic
sources only, including committed WAL data while idle connections remain open,
unchanged source bytes on success and refusal, and ambiguous sidecar collisions.
It does not need the provider, real Hermes checkout, engine, or repo autouse
fixtures. In a suitable isolated test environment with pytest installed:

```sh
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python3 -m pytest --noconftest \
  -p no:cacheprovider -q tests/test_recovery_snapshot.py
```

Choose a private isolated `HOME` and `TMPDIR` for the test runner. This is a
standalone test lane, not a substitute for the full repository suite or a live
provider restore/reindex test.
