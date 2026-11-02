# Linux host-scoped recovery evidence (opt-in, private)

`scripts/host_recovery.py` captures **only explicitly selected paths** and verifies
an isolated restore. It is not a provider backup, deployment, service manager,
reconciliation, or proof of a coherent live cutover. It complements, and does not
change, `recovery_snapshot.py` and its older format.

## Operator responsibility and limits

* Before any real capture, externally stop admission, drain and fence **all**
  relevant writers (including desktop/gateway sessions, services, automatic
  restart, cron, delayed notifications and SQLite owners), keep all selected
  paths and ancestors stationary, and keep the fence in effect until capture
  finishes. `--writers-quiesced` is only an assertion; this tool neither enforces
  nor verifies a fence. File stability checks cannot establish a cross-store
  atomic generation. No live consistency claim follows from a successful command.
* Explicitly inventory every authority and dependency before choosing scopes:
  built-in memory, vault and hidden index/inbox, state DB and sidecars, installed
  plugin, runtime and launcher, service units/drop-ins, Hermes/provider config,
  relevant profiles, skills, wiki, and **each external symlink target**. Nothing
  is auto-discovered. A manifest proves only its selected inventory, not that the
  operator selected every necessary host artifact or every SQLite database.
  Supply each database with `--sqlite` including databases with unusual names.
* Inputs and outputs are sensitive, unencrypted, and may contain credentials. Use
  an encrypted private filesystem as appropriate. The output and fresh restore
  have user-owned 0700 parents and private roots; captured original modes are
  restored inside that private boundary. A different-UID owner or restrictive
  mode can make isolated verification fail; chown, ACL or xattr restoration may
  require capabilities or filesystem support. Such failure is not waived.
* For each regular file, the manifest records size and SHA-256; for directories,
  files and links it records numeric UID/GID, mode, nanosecond atime/mtime and
  every xattr visible to the invoking process, including visible POSIX ACL xattrs.
  Link text is exact and never replaced by target bytes. Hard links, devices,
  sockets, FIFOs and other special entries are refused, not silently degraded.
  Unsupported/failed xattr enumeration/read/write, metadata application, or exact
  readback refuses. Filesystems or privilege boundaries can hide privileged
  namespaces such as `trusted.*` from an unprivileged process even when listing
  succeeds: **absence of hidden attributes is not attested**. Linux ctime, birth
  time, inode numbers, sparse layout, filesystem flags, mount metadata, active
  locks and running service state are not preserved or attested. This is not a
  complete filesystem image. Assess these limits before using it for rollback.
* Reads of regular files and directories use Linux `O_NOATIME` and fail if it is
  unavailable or forbidden. Linux `readlink` can update a source symlink's atime;
  the manifest retains the pre-read time, but capture does not reset the source
  link (which would itself change ctime). Following link chains during scope
  validation can similarly update link atimes. Thus capture is not strictly
  source-metadata-nonmutating. Validate whether this is acceptable beforehand.
* Symlink targets must resolve successfully within some explicitly selected
  non-symlink file or directory; targets are never recursively copied merely
  because they are linked. The original text is restored, **including absolute
  links to the live host**. Never dereference or execute links in the restored
  tree; they may point outside the restore. Restore reconstructs labeled scopes,
  not a working replacement of original absolute host paths. Resolve/rewrite only
  through a separate reviewed procedure, not as part of verification.
* This is for trusted, stable local inputs, trusted operator and trusted private
  snapshot/restore parents. It is not a sandbox against a hostile same-UID actor
  or concurrent rename/path substitution. Manifest hashes are integrity checks,
  not signatures, source authentication, proof of exclusive ownership, or an
  independent witness. Record the printed digest separately in trusted storage.
* Capture requires exact source-size copies and verifies the completed on-disk
  payload, backup integrity and inventory before reporting the digest. It fsyncs
  payload and backup files before their directories, then the manifest, snapshot
  directory and destination parent before reporting success. This relies on the
  local filesystem and storage honoring fsync; it is not a power-loss test or a
  guarantee for filesystems/devices that do not honor those semantics. Isolated
  restore is verified but is not itself a durably synced installation.

## SQLite policy

Raw main files and sidecars retain their selected names and exact bytes in the
labeled restore. Do not open a restored raw database with its raw sidecars: SQLite
could replay or mutate them. For each `--sqlite LABEL/path`, a *separate* backup
is made from copies of the captured main/WAL/SHM using the offline conservative
validator in `recovery_snapshot.py`. Existing sidecars outside the same labeled
scope are refused; select the containing directory. Rollback journals, incomplete
WAL/SHM pairs or invalid/corrupt indexes refuse after raw evidence was captured.
Each intermediate main/WAL/SHM copy is checked against the captured byte receipt.
Two independent, validated scratch inputs produce SQLite backups. For each, a
streaming comparator independently reconstructs the committed SQLite pager image
from the copied main file plus the latest validated WAL frame for each page. It
compares every resulting page with the closed, sidecar-free backup, excluding
only the volatile page-one change-counter and version-valid-for words. The
backup destination schema cookie is seeded so it must match the source cookie;
it is not excluded. Full backup bytes must also agree between lanes, a stronger
and more conservative condition than SQL-row equality. Raw evidence remains
separate and every main/WAL/SHM copy is checked against its admitted receipt;
main and WAL scratch copies are checked again after use. This covers stored
schema, rows, storage types, WITHOUT ROWID, indexes and internal pages without
sorting SQL values or materializing blobs. It can refuse benign page-layout
changes. SQLite never opens original evidence. The agreed backup digest is
carried through final verification; the backup is integrity-checked and restored
under `.host-recovery-backups/`, never adjacent to raw WAL.

Only the exact two host FTS5 virtual-table definitions, two content-view
SQL definitions and eight expected shadow-table identities/definitions are
admitted. The views must resolve to ordinary main-schema `messages` and `sessions`
tables. Each backup lane checks FTS5 index-to-content consistency with rank-1
integrity checks, rolled back before page comparison. Other virtual/shadow tables,
missing table-list inventory (normally SQLite older than 3.37), attachments and
nonempty temporary schemas are refused. Exact SQL spelling is intentional and
may reject equivalent schemas. See [recovery-snapshot.md](recovery-snapshot.md)
for the trusted-interpreter/SQLite and no-plugins/no-extensions prerequisites.
External application functions, collations and other outside state are not
captured or attested; rank-1 consistency is not source authenticity.

The comparator retains at most one page per side and uses a private on-disk
SQLite map proportional to the number of distinct WAL pages; WAL-index validation
streams 32 KiB blocks. Disk space remains necessary for multiple raw/backup
copies and that map. No universal process-memory bound is asserted for SQLite's
own `integrity_check`, pager, or backup implementation. A synthetic
283,492,352-byte database with a 2,117,712-byte committed WAL passed under a
192 MiB address-space limit; sampled fixture disk peaked at about 1.43 GB. A
separate exact-view FTS5 fixture with a 275,327,272-byte committed WAL passed
under the same limit (40,600 KiB peak RSS; sampled fixture disk 833,149,816
bytes). Those are synthetic, interpreter-specific measurements, not 3.29 GB
production-scale or universal resource qualification. The WAL fixture repeatedly
updates the same ballast pages; it does not represent 256 MiB of distinct FTS
index pages. As before, this assumes trusted SQLite and private, stable working
directories; it is not proof against a compromised SQLite implementation.

The host-spelling correction was exercised using Python 3.11.15 / SQLite 3.53.1
against synthetic databases only. The parent read-only comparison found all
eight expected shadow definitions match the immutable host backup; the four
observed stored SQL strings are independently transcribed in the test fixture.
The host-spelling WAL test passed at 275,327,272 WAL bytes with a 192 MiB
address-space limit, 41,392 KiB peak RSS, and 833,149,816 bytes sampled peak
fixture disk. The 260 ballast updates overwrite the same 1 MiB of pages. A
separate original-one-line fixture with a 283,533,312-byte main database also
passed at that address-space limit (38,504 KiB RSS; 1,417,859,672 bytes sampled
peak fixture disk). These measurements do not establish admission or recovery of
the actual backup after this correction, nor a live-host qualification.

A failed capture can leave private partial evidence and does not print a digest.
The manifest is removed on ordinary finalization failures on a best-effort basis;
an interrupted process, failed cleanup or power loss can leave a manifest that
is not an attestation. Trust only the digest from a successful completed capture,
recorded separately. Inspect and preserve or remove partials only under separate
operator control. A failed restore can leave a partial destination and never prints
a successful verification; do not use it.

## Example (all paths are placeholders)

Use distinct labels and actual reviewed paths; add every extra runtime, unit,
config, skill/wiki and target as needed. Select a containing directory for any
SQLite database with sidecars. Overlapping source scopes are allowed; output must
not overlap any source and must not already exist.

```sh
python3 scripts/host_recovery.py create \
  --scope builtins=/path/to/builtin-memory \
  --scope vault=/path/to/vault \
  --scope hermes=/path/to/hermes-profile \
  --scope runtime=/path/to/runtime \
  --scope service=/path/to/user-unit-directory \
  --scope skills=/path/to/skills --scope wiki=/path/to/wiki \
  --scope link_target=/path/to/explicit-external-target \
  --sqlite hermes/state.db --sqlite vault/.mirror-inbox.sqlite3 \
  --destination /private-parent/host-snapshot-001 \
  --writers-quiesced --include-sensitive-data

python3 scripts/host_recovery.py restore \
  --snapshot /private-parent/host-snapshot-001 \
  --destination /private-parent/isolated-restore-001 \
  --manifest-sha256 THE_SEPARATELY_RECORDED_DIGEST
```

The returned verification covers only the selected snapshot's complete enumerated
entry set, visible metadata, file bytes and separately selected SQLite backups.
It is not an operational restore, index-readiness test, provider health test or
permission to restart writers. After reopening writes, preserve that newer
state before contemplating rollback; never blindly replace it with old evidence.

## Synthetic test lane

With pytest and `setfacl` installed, run with private explicitly isolated
`HOME`, `HERMES_HOME`, all XDG directories, and `TMPDIR`:

```sh
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 PYTHONDONTWRITEBYTECODE=1 \
  python3 -B -m pytest --noconftest -o addopts= -p no:cacheprovider -q \
  tests/test_host_recovery.py tests/test_host_recovery_durability.py \
  tests/test_recovery_snapshot.py tests/test_ordinary_capture_provenance.py \
  tests/test_sqlite_capture_provenance.py tests/test_sqlite_streaming.py \
  -k 'not test_real_large_sqlite_wal_with_address_space_limit and not test_wal_page_sizes_and_multiple_index_blocks'
```

The exclusions are important on disk-constrained hosts: the streaming scale test
creates a >256 MiB database and multiple copies, while the second selection omits
the larger WAL fixture. Other FTS/scale suites also contain >256 MiB tests; inspect
individual test selection and available capacity before running them.

Fixtures include a real named-user ACL and xattr, private runtime/service/config,
links, committed WAL data, restored backup and negative/partial failures. They
include subprocess short-I/O and fsync fault injection, but are synthetic and do
not establish power-loss behavior, production writer quiescence or restore.
