#!/usr/bin/env python3
"""Explicit, offline file snapshot; not a provider backup or live restore tool."""
import argparse
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import sqlite3
import stat
import struct
import sys
import tempfile
from urllib.parse import quote


class Refusal(Exception):
    pass


def regular(path):
    mode = path.lstat().st_mode
    if not stat.S_ISREG(mode):
        raise Refusal(f"not a regular file: {path}")


def directory(path):
    mode = path.lstat().st_mode
    if not stat.S_ISDIR(mode):
        raise Refusal(f"not a directory: {path}")


def private_parent(path):
    directory(path)
    st = path.stat()
    if st.st_uid != os.getuid() or st.st_mode & 0o077:
        raise Refusal(f"destination parent must be user-owned and private (0700): {path}")


def digest(path):
    h = hashlib.sha256()
    with path.open('rb') as f:
        for block in iter(lambda: f.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def checksum(data, endian, state=(0, 0)):
    a, b = state
    words = struct.iter_unpack(endian + 'II', data)
    for x, y in words:
        a = (a + x + b) & 0xffffffff
        b = (b + y + a) & 0xffffffff
    return a, b


def validate_wal(main, wal, shm):
    """Conservatively admit only a complete WAL with its matching WAL-index.

    SQLite silently ignores corrupt WAL tails and can rebuild arbitrary SHM files.
    Neither behavior is suitable for evidence capture. The WAL-index is verified,
    but never supplied to SQLite; it will build a fresh index in private scratch.
    """
    def bad():
        raise Refusal(f'ambiguous or corrupt SQLite WAL/SHM: {wal}')
    if not wal.is_file() or not shm.is_file(): bad()
    with main.open('rb') as f:
        header = f.read(100)
    if (len(header) != 100 or header[:16] != b'SQLite format 3\x00'
            or header[18:20] != b'\x02\x02'):
        bad()
    db_page = int.from_bytes(header[16:18], 'big')
    if db_page == 1: db_page = 65536
    with wal.open('rb') as f:
        wh = f.read(32)
        if len(wh) != 32: bad()
        magic, version, page, _, salt1, salt2, c1, c2 = struct.unpack('>8I', wh)
        if (magic not in (0x377f0682, 0x377f0683) or version != 3007000
                or page != db_page or page < 512 or page > 65536 or page & (page - 1)):
            bad()
        endian = '<' if magic == 0x377f0682 else '>'
        state = checksum(wh[:24], endian)
        if state != (c1, c2): bad()
        pages, commit = [], 0
        while True:
            frame = f.read(24 + page)
            if not frame: break
            if len(frame) != 24 + page: bad()
            pgno, commit, s1, s2, c1, c2 = struct.unpack('>6I', frame[:24])
            if not pgno or (s1, s2) != (salt1, salt2): bad()
            state = checksum(frame[:8] + frame[24:], endian, state)
            if state != (c1, c2): bad()
            pages.append(pgno)
        if not pages or not commit: bad()
    index = shm.read_bytes()
    if len(index) < 32768 or len(index) % 32768: bad()
    h = index[:48]
    if h != index[48:96]: bad()
    version, _, _, init, big, sz, frames, dbsize, ck1, ck2 = struct.unpack('=IIIBBHIIII', h[:32])
    if (version != 3007000 or init != 1 or big != (magic & 1)
            or (65536 if sz == 1 else sz) != page or frames != len(pages) or dbsize != commit
            or (ck1, ck2) != state or h[32:40] != wh[16:24]
            or checksum(h[:40], '=') != struct.unpack('=II', h[40:48])):
        bad()
    backfill, *marks = struct.unpack('=6I', index[96:120])
    attempted, unused = struct.unpack('=2I', index[128:136])
    if (backfill > frames or attempted > frames or unused or index[120:128] != bytes(8)
            or any(mark != 0xffffffff and mark > frames for mark in marks)):
        bad()
    # Verify both page-number arrays and open-addressed hash tables. A stale,
    # partial or foreign index is ambiguous and is deliberately refused.
    offset = 0
    for block in range(len(index) // 32768):
        capacity = 4062 if block == 0 else 4096
        block_pages = pages[offset:offset + capacity]
        if block and not block_pages: bad()
        start = block * 32768 + (136 if block == 0 else 0)
        array = bytearray(capacity * 4)
        table = bytearray(8192 * 2)
        for i, pgno in enumerate(block_pages, 1):
            struct.pack_into('=I', array, (i - 1) * 4, pgno)
            slot = (pgno * 383) % 8192
            while struct.unpack_from('=H', table, slot * 2)[0]:
                slot = (slot + 1) % 8192
            struct.pack_into('=H', table, slot * 2, i)
        if index[start:start + len(array)] != array or index[start + len(array):start + len(array) + len(table)] != table:
            bad()
        offset += len(block_pages)
    if offset != len(pages): bad()


def sqlite_copy(raw, evidence_base, dest, scratch_parent):
    sidecar = lambda suffix: Path(str(evidence_base) + suffix)
    if sidecar('-journal').exists():
        raise Refusal(f'ambiguous SQLite journal: {sidecar("-journal")}')
    wal, shm = sidecar('-wal'), sidecar('-shm')
    if wal.exists() or shm.exists():
        validate_wal(raw, wal, shm)
    # Only copies of evidence enter SQLite. The original SHM is never opened;
    # SQLite creates its own scratch WAL-index. Cleanup is confined to our dir.
    with tempfile.TemporaryDirectory(prefix='.sqlite-work-', dir=scratch_parent) as work:
        copied = Path(work) / 'source.db'
        shutil.copyfile(raw, copied)
        if wal.exists(): shutil.copyfile(wal, str(copied) + '-wal')
        uri = 'file:' + quote(str(copied.absolute()), safe='/') + '?mode=ro'
        def progress(status, remaining, total):
            if status in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED):
                raise Refusal(f'SQLite database locked: {raw}')
        with closing(sqlite3.connect(uri, uri=True, timeout=0)) as src, closing(sqlite3.connect(dest)) as dst:
            src.backup(dst, pages=-1, progress=progress, sleep=0)
            if dst.execute('PRAGMA integrity_check').fetchall() != [('ok',)]:
                raise Refusal(f'SQLite integrity check failed: {raw}')
    os.chmod(dest, 0o600)


def inventory(root, label):
    """Inventory before copying, rejecting symlinks and non-regular entries."""
    directory(root)
    found, excluded = [], []
    def walk(folder, rel):
        with os.scandir(folder) as scan:
            entries = sorted(scan, key=lambda e: e.name)
        names = {e.name for e in entries}
        for ent in entries:
            p = folder / ent.name
            r = rel / ent.name
            mode = ent.stat(follow_symlinks=False).st_mode
            if stat.S_ISLNK(mode):
                raise Refusal(f'symlink not supported: {p}')
            if stat.S_ISDIR(mode):
                found.append((p, r, 'dir'))
                walk(p, r)
            elif stat.S_ISREG(mode):
                # Names alone cannot distinguish SQLite sidecars from user data.
                suffix = next((s for s in ('-wal', '-shm', '-journal') if ent.name.endswith(s)), None)
                base = ent.name[:-len(suffix)] if suffix else ''
                if suffix and base in names and is_sqlite_name(base):
                    found.append((p, r, 'sidecar'))
                    continue
                found.append((p, r, 'sqlite' if is_sqlite_name(ent.name) else 'file'))
            else:
                raise Refusal(f'unsupported filesystem entry: {p}')
    walk(root, PurePosixPath())
    return found, excluded


def is_sqlite_name(name):
    return name.lower().endswith(('.db', '.sqlite', '.sqlite3'))


def require_disjoint(paths, destination):
    resolved = [(name, path.resolve(strict=True)) for name, path in paths.items()]
    # Sources may overlap: e.g. provider config.json commonly lives in the vault.
    # The output must be completely separate from each selected source.
    out = destination.parent.resolve(strict=True) / destination.name
    for name, p in resolved:
        if p == out or p in out.parents or out in p.parents:
            raise Refusal(f'output overlaps {name}')


def create(args):
    roots = {'builtins': args.builtins, 'vault': args.vault, 'plugin': args.plugin}
    if args.wiki: roots['wiki'] = args.wiki
    if args.skills: roots['skills'] = args.skills
    sources = dict(roots, hermes_config=args.hermes_config,
                   provider_config=args.provider_config, state_db=args.state_db)
    for path in sources.values():
        if not path.is_absolute(): raise Refusal('all source paths must be absolute')
    if not args.destination.is_absolute(): raise Refusal('destination must be absolute')
    private_parent(args.destination.parent)
    require_disjoint(sources, args.destination)
    regular(args.hermes_config)
    regular(args.provider_config)
    regular(args.state_db)
    for filename in ('MEMORY.md', 'USER.md'):
        regular(args.builtins / filename)
    for filename in ('__init__.py', 'plugin.yaml'):
        regular(args.plugin / filename)
    plans, exclusions, sidecars = {}, [], []
    raw_databases = [(args.state_db, PurePosixPath('state.db'))]
    for suffix in ('-wal', '-shm', '-journal'):
        sidecar = Path(str(args.state_db) + suffix)
        try:
            regular(sidecar)
        except FileNotFoundError:
            continue
        sidecars.append((sidecar, PurePosixPath('state.db' + suffix)))
    for name, root in roots.items():
        plans[name], excluded = inventory(root, name)
        exclusions.extend(excluded)
        sidecars.extend((source, PurePosixPath(name) / rel)
                        for source, rel, kind in plans[name] if kind == 'sidecar')
        raw_databases.extend((source, PurePosixPath(name) / rel)
                             for source, rel, kind in plans[name] if kind == 'sqlite')
    # Exclusive mkdir refuses existing destinations; failed attempts remain incomplete.
    args.destination.mkdir(mode=0o700)
    os.chmod(args.destination, 0o700)
    records = []
    def add_file(source, rel, kind, source_path=None):
        dest = args.destination / rel
        if kind == 'sqlite':
            sqlite_copy(args.destination / 'raw-database-evidence' / rel,
                        args.destination / 'sidecar-evidence' / rel,
                        dest, args.destination)
        else:
            regular(source)
            with source.open('rb') as src, dest.open('xb') as dst:
                shutil.copyfileobj(src, dst)
            os.chmod(dest, 0o600)
        record = {'path': rel.as_posix(), 'type': kind, 'size': dest.stat().st_size, 'sha256': digest(dest)}
        if source_path is not None: record['source_path'] = source_path.as_posix()
        records.append(record)
    # Capture raw bytes before SQLite opens; keep main files and sidecars in
    # separate trees, away from each other and the usable online backups.
    evidence_dirs = set()
    for evidence, items in (('sidecar-evidence', sidecars), ('raw-database-evidence', raw_databases)):
        for source, original in items:
            target = PurePosixPath(evidence) / original
            for parent in reversed(list(target.parents)[:-1]):
                if parent not in evidence_dirs:
                    (args.destination / parent).mkdir(mode=0o700)
                    records.append({'path': parent.as_posix(), 'type': 'dir'})
                    evidence_dirs.add(parent)
            add_file(source, target, 'file', original)
    for name, entries in plans.items():
        (args.destination / name).mkdir(mode=0o700)
        records.append({'path': name, 'type': 'dir'})
        for source, rel, kind in entries:
            target = PurePosixPath(name) / rel
            if kind == 'dir':
                directory(source)
                (args.destination / target).mkdir(mode=0o700)
                records.append({'path': target.as_posix(), 'type': 'dir'})
            elif kind != 'sidecar': add_file(source, target, kind)
    add_file(args.hermes_config, PurePosixPath('hermes-config'), 'file')
    add_file(args.provider_config, PurePosixPath('provider-config'), 'file')
    add_file(args.state_db, PurePosixPath('state.db'), 'sqlite')
    manifest = {'format': 2, 'consistency': 'operator-asserted writer quiescence; no cross-store atomicity',
                'sources': {k: str(v) for k, v in sources.items()},
                'entries': sorted(records, key=lambda x: x['path']), 'exclusions': exclusions}
    manifest_path = args.destination / 'manifest.json'
    with manifest_path.open('x', encoding='utf-8') as f:
        json.dump(manifest, f, indent=2, sort_keys=True, ensure_ascii=True)
        f.write('\n')
    os.chmod(manifest_path, 0o600)
    print('manifest_sha256=' + digest(manifest_path))


def verify(snapshot, expected):
    directory(snapshot)
    manifest_path = snapshot / 'manifest.json'
    regular(manifest_path)
    if digest(manifest_path) != expected:
        raise Refusal('manifest SHA-256 mismatch')
    manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
    if manifest.get('format') not in (1, 2) or not isinstance(manifest.get('entries'), list):
        raise Refusal('unsupported manifest')
    entries, seen = manifest['entries'], set()
    for entry in entries:
        rel, kind = entry['path'], entry['type']
        if not isinstance(rel, str) or not rel or '\\' in rel:
            raise Refusal('invalid manifest path')
        p = PurePosixPath(rel)
        if p.is_absolute() or p.as_posix() != rel or any(part in ('.', '..') for part in p.parts) or rel == 'manifest.json' or rel in seen:
            raise Refusal('invalid or duplicate manifest path')
        seen.add(rel)
        target = snapshot / p
        # Inspect each ancestor without following symlinks.
        for parent in list(p.parents)[:-1]: directory(snapshot / parent)
        if kind == 'dir': directory(target)
        elif kind in ('file', 'sqlite'):
            regular(target)
            if target.stat().st_size != entry['size'] or digest(target) != entry['sha256']:
                raise Refusal(f'checksum mismatch: {rel}')
            if kind == 'sqlite':
                uri = 'file:' + quote(str(target.absolute()), safe='/') + '?mode=ro&immutable=1'
                with closing(sqlite3.connect(uri, uri=True)) as db:
                    if db.execute('PRAGMA integrity_check').fetchall() != [('ok',)]:
                        raise Refusal(f'SQLite integrity check failed: {rel}')
        else: raise Refusal('invalid entry type')
    # Do not allow raw sidecars next to an online-backup database, even if a
    # trusted manifest was accidentally constructed with such a layout.
    sqlite_paths = {entry['path'] for entry in entries if entry['type'] == 'sqlite'
                    or (entry['type'] == 'file' and entry['path'].startswith('raw-database-evidence/')
                        and 'source_path' in entry)}
    if any(db + suffix in seen for db in sqlite_paths for suffix in ('-wal', '-shm', '-journal')):
        raise Refusal('SQLite backup has adjacent sidecar; raw evidence must be separate')
    actual, excluded = inventory(snapshot, 'snapshot')
    if excluded or {r.as_posix() for _, r, _ in actual} != seen | {'manifest.json'}:
        raise Refusal('snapshot contains missing or unmanifested entries')
    return entries


def restore(args):
    if not args.snapshot.is_absolute() or not args.destination.is_absolute():
        raise Refusal('paths must be absolute')
    private_parent(args.destination.parent)
    require_disjoint({'snapshot': args.snapshot}, args.destination)
    entries = verify(args.snapshot, args.manifest_sha256)
    args.destination.mkdir(mode=0o700)
    os.chmod(args.destination, 0o700)
    for entry in sorted(entries, key=lambda x: (x['path'].count('/'), x['path'])):
        src, dest = args.snapshot / entry['path'], args.destination / entry['path']
        if entry['type'] == 'dir': dest.mkdir(mode=0o700)
        else:
            with src.open('rb') as inp, dest.open('xb') as out: shutil.copyfileobj(inp, out)
            os.chmod(dest, 0o600)
            if digest(dest) != entry['sha256']: raise Refusal('restored checksum mismatch')
            if entry['type'] == 'sqlite':
                uri = 'file:' + quote(str(dest.absolute()), safe='/') + '?mode=ro&immutable=1'
                with closing(sqlite3.connect(uri, uri=True)) as db:
                    if db.execute('PRAGMA integrity_check').fetchall() != [('ok',)]:
                        raise Refusal('restored SQLite integrity check failed')
    print(f'verified and restored {len(entries)} entries')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    create_p = sub.add_parser('create')
    for option in ('builtins', 'vault', 'plugin', 'hermes-config', 'provider-config', 'state-db', 'destination'):
        create_p.add_argument('--' + option, type=Path, required=True)
    for option in ('wiki', 'skills'): create_p.add_argument('--' + option, type=Path)
    create_p.add_argument('--writers-quiesced', action='store_true', required=True,
                          help='assert all writers are stopped for the entire operation')
    create_p.add_argument('--include-sensitive-data', action='store_true', required=True,
                          help='acknowledge explicit inputs may contain secrets and private data')
    restore_p = sub.add_parser('restore')
    for option in ('snapshot', 'destination'): restore_p.add_argument('--' + option, type=Path, required=True)
    restore_p.add_argument('--manifest-sha256', required=True, help='trusted digest recorded separately at creation')
    args = parser.parse_args()
    try:
        create(args) if args.command == 'create' else restore(args)
    except (Refusal, OSError, sqlite3.Error, ValueError, KeyError, TypeError) as exc:
        print(f'refused: {exc}', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
