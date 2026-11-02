#!/usr/bin/env python3
"""Private, explicit Linux host evidence capture; never a live cutover tool."""
import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import stat
import sys
import tempfile

from recovery_snapshot import Refusal, checked_copy, private_parent, require_disjoint, sqlite_connect, sqlite_copy


def digest(path):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for data in iter(lambda: f.read(1024 * 1024), b''):
            h.update(data)
    return h.hexdigest()


def sync_path(path, *, directory=False):
    flags = os.O_RDONLY | os.O_NOFOLLOW
    if directory:
        flags |= os.O_DIRECTORY
    fd = os.open(path, flags)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def xattrs(path):
    # Fail on ENOTSUP, EACCES, unknown namespace or any partial read.
    return {name: base64.b64encode(os.getxattr(path, name, follow_symlinks=False)).decode('ascii')
            for name in sorted(os.listxattr(path, follow_symlinks=False))}


def metadata(path):
    s = os.lstat(path)
    return {'uid': s.st_uid, 'gid': s.st_gid, 'mode': stat.S_IMODE(s.st_mode),
            'atime_ns': s.st_atime_ns, 'mtime_ns': s.st_mtime_ns, 'xattrs': xattrs(path)}


def stable(s, t):
    return (s.st_dev, s.st_ino, s.st_mode, s.st_uid, s.st_gid, s.st_size,
            s.st_mtime_ns, s.st_ctime_ns, s.st_nlink) == (t.st_dev, t.st_ino, t.st_mode, t.st_uid, t.st_gid, t.st_size, t.st_mtime_ns, t.st_ctime_ns, t.st_nlink)


def nofollow_ancestors(path):
    if not path.is_absolute() or str(path) != os.path.normpath(str(path)):
        raise Refusal('source paths must be normalized absolute paths')
    for parent in reversed(path.parents):
        if not stat.S_ISDIR(os.lstat(parent).st_mode):
            raise Refusal(f'symlink or non-directory ancestor: {parent}')


def walk(path, rel, records, payload, roots):
    s = os.lstat(path)
    m = metadata(path)
    if stat.S_ISDIR(s.st_mode):
        kind = 'dir'
    elif stat.S_ISREG(s.st_mode):
        if s.st_nlink != 1:
            raise Refusal(f'hard links unsupported: {path}')
        kind = 'file'
    elif stat.S_ISLNK(s.st_mode):
        kind = 'symlink'
    else:
        raise Refusal(f'unsupported filesystem entry: {path}')
    entry = {'path': rel, 'type': kind, 'metadata': m}
    records.append(entry)
    if kind == 'symlink':
        entry['target'] = os.readlink(path)
        # Never implicitly copy a target. It must be in an explicitly selected tree.
        target = path.resolve(strict=True)
        if not any(target == r or r in target.parents for r in roots):
            raise Refusal(f'symlink target outside selected scopes: {path}')
    elif kind == 'file':
        flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NOATIME
        fd = os.open(path, flags)
        with os.fdopen(fd, 'rb') as src:
            if not stable(s, os.fstat(src.fileno())):
                raise Refusal(f'source changed: {path}')
            blob = payload / f'{len(records):08d}'
            h = hashlib.sha256()
            with blob.open('xb') as dst:
                os.chmod(blob, 0o600)
                remaining = s.st_size
                while remaining:
                    block = src.read(min(1024 * 1024, remaining))
                    if not block:
                        raise Refusal(f'short source read: {path}')
                    if len(block) > remaining or dst.write(block) != len(block):
                        raise Refusal(f'short payload write: {path}')
                    h.update(block)
                    remaining -= len(block)
                if src.read(1):
                    raise Refusal(f'source exceeds expected size: {path}')
                dst.flush()
                if os.fstat(dst.fileno()).st_size != s.st_size:
                    raise Refusal(f'payload size mismatch: {path}')
                os.fsync(dst.fileno())
            if not stable(s, os.fstat(src.fileno())):
                raise Refusal(f'source changed: {path}')
            entry.update(blob=blob.name, size=s.st_size, sha256=h.hexdigest())
    else:
        # O_NOATIME avoids changing directory timestamps while enumerating.
        fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_NOATIME)
        try:
            if not stable(s, os.fstat(fd)):
                raise Refusal(f'source changed: {path}')
            with os.scandir(fd) as scan:
                names = sorted(e.name for e in scan)
            for name in names:
                walk(path / name, rel + '/' + name, records, payload, roots)
            if not stable(s, os.fstat(fd)):
                raise Refusal(f'source directory changed: {path}')
        finally:
            os.close(fd)
    after = metadata(path)
    # Linux readlink may update the source link's atime; preserve the pre-read
    # value in the record, without writing back to (and changing ctime on) source.
    if kind == 'symlink':
        after['atime_ns'] = m['atime_ns']
    if not stable(s, os.lstat(path)) or after != m:
        raise Refusal(f'source metadata changed: {path}')


def parse_scope(values):
    scopes = {}
    for value in values:
        label, sep, path = value.partition('=')
        if not sep or not re.fullmatch(r'[A-Za-z0-9_-]+', label) or label in scopes:
            raise Refusal('scope must be unique LABEL=/absolute/path')
        p = Path(path)
        nofollow_ancestors(p)
        os.lstat(p)
        scopes[label] = p
    if not scopes:
        raise Refusal('at least one explicit scope required')
    return scopes


def safe_rel(value):
    if not isinstance(value, str) or not value or value.startswith('/') or any(p in ('', '.', '..') for p in value.split('/')):
        raise Refusal('invalid relative path')
    return value


def sqlite_backups(snapshot, records, selections, scopes):
    bypath = {e['path']: e for e in records}
    result = []
    for rel in selections:
        safe_rel(rel)
        if rel not in bypath or bypath[rel]['type'] != 'file' or rel in {r['source'] for r in result}:
            raise Refusal(f'invalid SQLite selection: {rel}')
        label, _, subpath = rel.partition('/')
        if label not in scopes:
            raise Refusal('SQLite selection outside scopes')
        source = scopes[label] / subpath if subpath else scopes[label]
        for suffix in ('-wal', '-shm', '-journal'):
            try:
                os.lstat(str(source) + suffix)
            except FileNotFoundError:
                continue
            if rel + suffix not in bypath:
                raise Refusal(f'SQLite sidecar outside selected scopes: {source}{suffix}')
        with tempfile.TemporaryDirectory(prefix='.sqlite-', dir=snapshot) as tmp:
            base = Path(tmp) / 'raw.db'
            admitted = {}
            for suffix in ('', '-wal', '-shm', '-journal'):
                item = bypath.get(rel + suffix)
                if item:
                    if item['type'] != 'file':
                        raise Refusal('SQLite sidecar is not regular')
                    payload = snapshot / 'payload' / item['blob']
                    expected = (item['size'], item['sha256'])
                    checked_copy(payload, str(base) + suffix, expected)
                    if suffix != '-journal':
                        admitted[suffix] = (payload, expected)
            output = snapshot / 'backups' / f'{len(result):08d}.db'
            size, sha = sqlite_copy(base, base, output, snapshot, admitted=admitted)
            sync_path(output)
            result.append({'source': rel, 'file': output.name, 'size': size, 'sha256': sha})
    return result


def create(args):
    if sys.platform != 'linux':
        raise Refusal('Linux is required')
    scopes = parse_scope(args.scope)
    if not args.destination.is_absolute():
        raise Refusal('destination must be absolute')
    nofollow_ancestors(args.destination)
    private_parent(args.destination.parent)
    require_disjoint(scopes, args.destination)
    roots = [p.resolve(strict=True) for p in scopes.values() if not p.is_symlink()]
    args.destination.mkdir(mode=0o700)
    os.chmod(args.destination, 0o700)
    (args.destination / 'payload').mkdir(mode=0o700)
    (args.destination / 'backups').mkdir(mode=0o700)
    records = []
    for label, path in sorted(scopes.items()):
        walk(path, label, records, args.destination / 'payload', roots)
    backups = sqlite_backups(args.destination, records, args.sqlite, scopes)
    # Persist each populated directory only after its files have been synced.
    sync_path(args.destination / 'payload', directory=True)
    sync_path(args.destination / 'backups', directory=True)
    uname = os.uname()
    manifest = {'format': 'linux-host-recovery-1', 'sources': {k: str(v) for k, v in sorted(scopes.items())},
                'host': {'hostname': uname.nodename, 'kernel': uname.release, 'architecture': uname.machine,
                         'capture_uid': os.getuid(), 'capture_gid': os.getgid()},
                'scope_entry_counts': {label: sum(e['path'] == label or e['path'].startswith(label + '/') for e in records)
                                       for label in scopes},
                'consistency': 'operator assertion only; no writer fence or cross-store atomicity',
                'metadata_coverage': 'visible Linux xattrs including ACLs; hidden privileged namespaces not attested',
                'entries': records, 'sqlite_backups': backups}
    path = args.destination / 'manifest.json'
    created = False
    complete = False
    try:
        with path.open('x', encoding='utf-8') as f:
            created = True
            os.chmod(path, 0o600)
            json.dump(manifest, f, sort_keys=True, ensure_ascii=True, indent=2)
            f.write('\n')
            f.flush()
            os.fsync(f.fileno())
        sha = digest(path)
        # Verify the complete on-disk snapshot, not merely the bytes read from
        # the source, before publishing its attestation.
        verify(args.destination, sha)
        sync_path(args.destination, directory=True)
        sync_path(args.destination.parent, directory=True)
        complete = True
    finally:
        if created and not complete:
            # Preserve the primary failure. A failed removal or cleanup fsync
            # can leave an untrusted manifest; only a printed digest attests it.
            try:
                path.unlink()
                sync_path(args.destination, directory=True)
            except OSError:
                pass
    print('manifest_sha256=' + sha)


def scan_tree(path, rel=''):
    result = set()
    with os.scandir(path) as scan:
        entries = list(scan)
    for entry in entries:
        r = rel + '/' + entry.name if rel else entry.name
        result.add(r)
        if entry.is_dir(follow_symlinks=False):
            result |= scan_tree(Path(path) / entry.name, r)
    return result


def verify(snapshot, expected):
    if not re.fullmatch(r'[0-9a-f]{64}', expected):
        raise Refusal('invalid manifest digest')
    nofollow_ancestors(snapshot)
    if not stat.S_ISDIR(os.lstat(snapshot).st_mode):
        raise Refusal('snapshot is not a directory')
    for name in ('payload', 'backups'):
        if not stat.S_ISDIR(os.lstat(snapshot / name).st_mode):
            raise Refusal('snapshot component is not a directory')
    if not stat.S_ISREG(os.lstat(snapshot / 'manifest.json').st_mode):
        raise Refusal('manifest is not a regular file')
    if digest(snapshot / 'manifest.json') != expected:
        raise Refusal('manifest digest mismatch')
    manifest = json.loads((snapshot / 'manifest.json').read_text())
    if manifest.get('format') != 'linux-host-recovery-1':
        raise Refusal('unsupported manifest')
    entries = manifest['entries']
    seen, blobs, types = set(), set(), {}
    for e in entries:
        rel = safe_rel(e['path'])
        if rel in seen or ('/' in rel and types.get(rel.rsplit('/', 1)[0]) != 'dir'):
            raise Refusal('duplicate or missing directory parent')
        seen.add(rel)
        types[rel] = e['type']
        if e['type'] == 'file':
            name = e['blob']
            if not re.fullmatch(r'[0-9]{8}', name) or name in blobs:
                raise Refusal('invalid blob')
            blobs.add(name)
            p = snapshot / 'payload' / name
            if not stat.S_ISREG(os.lstat(p).st_mode) or p.stat().st_size != e['size'] or digest(p) != e['sha256']:
                raise Refusal('payload mismatch')
        elif e['type'] == 'symlink':
            if not isinstance(e['target'], str) or '\x00' in e['target']:
                raise Refusal('invalid link')
        elif e['type'] != 'dir':
            raise Refusal('unsupported entry')
    sources, counts = manifest['sources'], manifest['scope_entry_counts']
    if not isinstance(sources, dict) or not sources or set(sources) != set(counts):
        raise Refusal('invalid scope inventory')
    for label in sources:
        if not re.fullmatch(r'[A-Za-z0-9_-]+', label) or label not in seen:
            raise Refusal('invalid scope root')
        actual_count = sum(p == label or p.startswith(label + '/') for p in seen)
        if type(counts[label]) is not int or counts[label] != actual_count:
            raise Refusal('scope count mismatch')
    if any(p.split('/', 1)[0] not in sources for p in seen):
        raise Refusal('entry outside scope inventory')
    backups = manifest['sqlite_backups']
    names, backup_sources = set(), set()
    for b in backups:
        name = b['file']
        if (not re.fullmatch(r'[0-9]{8}\.db', name) or name in names
                or types.get(b['source']) != 'file' or b['source'] in backup_sources):
            raise Refusal('invalid backup')
        names.add(name)
        backup_sources.add(b['source'])
        path = snapshot / 'backups' / name
        if not stat.S_ISREG(os.lstat(path).st_mode) or path.stat().st_size != b['size'] or digest(path) != b['sha256']:
            raise Refusal('backup mismatch')
        from contextlib import closing
        from urllib.parse import quote
        with closing(sqlite_connect('file:' + quote(str(path), safe='/') + '?mode=ro&immutable=1', uri=True)) as db:
            if db.execute('pragma integrity_check').fetchall() != [('ok',)]:
                raise Refusal('SQLite integrity check failed')
    expected_paths = {'manifest.json', 'payload', 'backups'} | {'payload/' + x for x in blobs} | {'backups/' + x for x in names}
    if scan_tree(snapshot) != expected_paths:
        raise Refusal('missing or unmanifested snapshot entry')
    return manifest


def apply_metadata(path, m):
    os.chown(path, m['uid'], m['gid'], follow_symlinks=False)
    if not path.is_symlink():
        os.chmod(path, m['mode'], follow_symlinks=False)
    for name, value in m['xattrs'].items():
        os.setxattr(path, name, base64.b64decode(value, validate=True), follow_symlinks=False)
    os.utime(path, ns=(m['atime_ns'], m['mtime_ns']), follow_symlinks=False)


def restore(args):
    if sys.platform != 'linux':
        raise Refusal('Linux is required')
    if not args.snapshot.is_absolute() or not args.destination.is_absolute():
        raise Refusal('paths must be absolute')
    nofollow_ancestors(args.destination)
    private_parent(args.destination.parent)
    require_disjoint({'snapshot': args.snapshot}, args.destination)
    manifest = verify(args.snapshot, args.manifest_sha256)
    args.destination.mkdir(mode=0o700)
    os.chmod(args.destination, 0o700)
    entries = manifest['entries']
    for e in entries:
        dest = args.destination / e['path']
        if e['type'] == 'dir':
            dest.mkdir(mode=0o700)
        elif e['type'] == 'symlink':
            os.symlink(e['target'], dest)
        else:
            with (args.snapshot / 'payload' / e['blob']).open('rb') as inp, dest.open('xb') as out:
                shutil.copyfileobj(inp, out)
    backup_root = args.destination / '.host-recovery-backups'
    backup_root.mkdir(mode=0o700)
    for b in manifest['sqlite_backups']:
        dest = backup_root / b['file']
        with (args.snapshot / 'backups' / b['file']).open('rb') as inp, dest.open('xb') as out:
            shutil.copyfileobj(inp, out)
        os.chmod(dest, 0o600)
        if dest.stat().st_size != b['size'] or digest(dest) != b['sha256']:
            raise Refusal('restored SQLite backup mismatch')
        from contextlib import closing
        from urllib.parse import quote
        with closing(sqlite_connect('file:' + quote(str(dest), safe='/') + '?mode=ro&immutable=1', uri=True)) as db:
            if db.execute('pragma integrity_check').fetchall() != [('ok',)]:
                raise Refusal('restored SQLite integrity check failed')
    actual = scan_tree(args.destination)
    expected = {e['path'] for e in entries} | {'.host-recovery-backups'} | {
        '.host-recovery-backups/' + b['file'] for b in manifest['sqlite_backups']}
    if actual != expected:
        raise Refusal('restored inventory mismatch')
    # Read link text before setting timestamps: readlink itself may update atime.
    for e in entries:
        if e['type'] == 'symlink' and os.readlink(args.destination / e['path']) != e['target']:
            raise Refusal('restored link mismatch')
    # Children before parents: timestamps and modes must survive population.
    for e in reversed(entries):
        apply_metadata(args.destination / e['path'], e['metadata'])
    # Enumerate before final metadata comparison; O_NOATIME prevents perturbation.
    for e in entries:
        path = args.destination / e['path']
        s = os.lstat(path)
        if metadata(path) != e['metadata']:
            raise Refusal(f'restored metadata mismatch: {e["path"]}')
        if e['type'] == 'file':
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NOATIME)
            h = hashlib.sha256()
            with os.fdopen(fd, 'rb') as f:
                for b in iter(lambda: f.read(1024 * 1024), b''):
                    h.update(b)
            if not stat.S_ISREG(s.st_mode) or s.st_size != e['size'] or h.hexdigest() != e['sha256']:
                raise Refusal('restored file mismatch')
        elif e['type'] == 'dir' and not stat.S_ISDIR(s.st_mode):
            raise Refusal('restored directory mismatch')
        elif e['type'] == 'symlink' and not stat.S_ISLNK(s.st_mode):
            raise Refusal('restored link mismatch')
    print(f'verified isolated restore: {len(entries)} entries and {len(manifest["sqlite_backups"])} SQLite backups')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    c = sub.add_parser('create')
    c.add_argument('--scope', action='append', default=[], metavar='LABEL=ABSOLUTE_PATH')
    c.add_argument('--sqlite', action='append', default=[], metavar='LABEL/RELATIVE_DB')
    c.add_argument('--destination', type=Path, required=True)
    c.add_argument('--writers-quiesced', action='store_true', required=True)
    c.add_argument('--include-sensitive-data', action='store_true', required=True)
    r = sub.add_parser('restore')
    r.add_argument('--snapshot', type=Path, required=True)
    r.add_argument('--destination', type=Path, required=True)
    r.add_argument('--manifest-sha256', required=True)
    args = parser.parse_args()
    try:
        create(args) if args.command == 'create' else restore(args)
    except (Refusal, OSError, sqlite3.Error, ValueError, TypeError, KeyError) as e:
        print(f'refused: {e}', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
