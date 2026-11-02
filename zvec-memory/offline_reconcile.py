"""Conservative reconciliation of a detached, operator-quiesced snapshot only.

This is not an online migration or a writer fence. See docs/mirror-reconciliation-design.md.
"""
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import uuid
from datetime import datetime

from .inbox import MirrorInbox
from .transactions import VaultLock

LIMIT = 64 * 1024 * 1024
MAX_FILES = 10000
# Snapshot budgets are separate from the much smaller parsed fact/map budgets.
SNAPSHOT_MAX_ENTRIES = 100000
SNAPSHOT_MAX_BYTES = 16 * 1024 * 1024 * 1024
SNAPSHOT_MAX_FILE_BYTES = 8 * 1024 * 1024 * 1024
COPY_CHUNK = 1024 * 1024
MARKER = '.offline-reconcile-snapshot.json'


def _digest(data):
    return hashlib.sha256(data).hexdigest()


def _strict_json(data):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError('Duplicate JSON key')
            result[key] = value
        return result

    def constant(value):
        raise ValueError('Non-finite JSON value')

    return json.loads(data.decode('utf-8'), object_pairs_hook=pairs, parse_constant=constant)


def _directory(path):
    path = Path(path)
    if not path.is_absolute() or path.resolve() != path or not path.is_dir():
        raise ValueError('Directory must be absolute, existing, and unaliased')
    return path


def _read(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1 or st.st_size > LIMIT:
            raise ValueError('Input must be a bounded, single-link regular file')
        with os.fdopen(fd, 'rb', closefd=False) as f:
            data = f.read(LIMIT + 1)
        if len(data) > LIMIT:
            raise ValueError('Input too large')
        return data
    finally:
        os.close(fd)


def _identity(path):
    st = path.lstat()
    return [st.st_dev, st.st_ino, st.st_mode, st.st_nlink, st.st_size]


def _sync(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _write_new(path, data):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'wb') as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())


def _copy_file(source, dest, remaining):
    """Copy and independently hash a single-link file with bounded memory.

    This detects ordinary drift, not an adversarial same-UID writer; the caller
    must fence all writers for the complete copy.
    """
    fd = os.open(source, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        before = os.fstat(fd)
        if (not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or
                before.st_size > SNAPSHOT_MAX_FILE_BYTES or before.st_size > remaining):
            raise ValueError('Snapshot file type, links, or byte limit exceeded')
        digest = hashlib.sha256()
        size = 0
        out = os.open(dest, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(out, 'wb') as target:
            while True:
                chunk = os.read(fd, min(COPY_CHUNK, remaining - size + 1))
                if not chunk:
                    break
                size += len(chunk)
                if size > remaining or size > SNAPSHOT_MAX_FILE_BYTES:
                    raise ValueError('Snapshot byte limit exceeded')
                digest.update(chunk)
                target.write(chunk)
            target.flush()
            os.fsync(target.fileno())
        after = os.fstat(fd)
        identity = lambda s: (s.st_dev, s.st_ino, s.st_mode, s.st_nlink, s.st_size, s.st_mtime_ns, s.st_ctime_ns)
        if identity(before) != identity(after) or identity(after) != identity(source.lstat()) or size != before.st_size:
            raise ValueError('Snapshot source changed during copy')
        check = hashlib.sha256()
        with dest.open('rb') as copied:
            for chunk in iter(lambda: copied.read(COPY_CHUNK), b''):
                check.update(chunk)
        if check.digest() != digest.digest() or dest.stat().st_size != size:
            raise ValueError('Snapshot copy verification failed')
        return {'size': size, 'sha256': digest.hexdigest(), 'source_identity': list(identity(before))}
    finally:
        os.close(fd)


def _owned_fact_exact(data, content, target):
    """Recognize the complete _write_fact mirror serialization, not a suffix."""
    category = 'user_pref' if target == 'user' else 'general'
    header = (f"# {content[:80].replace(chr(10), ' ')}\n\n"
              f"category: {category}\ntags: mirror\nstored: ").encode('utf-8')
    tail = ('\n\n' + content + '\n').encode('utf-8')
    if len(data) != len(header) + 15 + len(tail) or not data.startswith(header) or not data.endswith(tail):
        return False
    stamp = data[len(header):len(header) + 15]
    if not re.fullmatch(rb'[0-9]{8}-[0-9]{6}', stamp):
        return False
    try:
        datetime.strptime(stamp.decode('ascii'), '%Y%m%d-%H%M%S')
    except ValueError:
        return False
    return True


def create_snapshot(profile, vault, destination):
    """Copy a stopped profile/vault to a new private workspace; never write inputs."""
    profile, vault = _directory(profile), _directory(vault)
    destination = Path(destination)
    if not destination.is_absolute() or destination.parent.resolve() != destination.parent:
        raise ValueError('Snapshot destination parent must be unaliased')
    if destination.is_relative_to(profile) or destination.is_relative_to(vault):
        raise ValueError('Snapshot must not be inside its inputs')
    destination.mkdir(mode=0o700)  # exclusive; do not clean an interrupted copy
    (destination / 'profile' / 'memories').mkdir(parents=True, mode=0o700)
    (destination / 'vault').mkdir(mode=0o700)
    for name in ('MEMORY.md', 'USER.md'):
        source = profile / 'memories' / name
        if source.exists() or source.is_symlink():
            if source.parent.resolve() != source.parent:
                raise ValueError('Aliased memories directory')
            _write_new(destination / 'profile' / 'memories' / name, _read(source))
    count = total = 0
    receipt_size = 2  # JSON object braces
    copied = {}
    def walk_error(error):
        raise error
    for root, dirs, files in os.walk(vault, followlinks=False, onerror=walk_error):
        root = Path(root)
        for name in dirs:
            src = root / name
            count += 1
            if count > SNAPSHOT_MAX_ENTRIES:
                raise ValueError('Snapshot entry count limit exceeded')
            if not stat.S_ISDIR(src.lstat().st_mode):
                raise ValueError('Non-directory in vault snapshot')
            (destination / 'vault' / src.relative_to(vault)).mkdir(mode=0o700)
        for name in files:
            src = root / name
            rel = src.relative_to(vault)
            count += 1
            if count > SNAPSHOT_MAX_ENTRIES:
                raise ValueError('Snapshot entry count limit exceeded')
            if rel.as_posix() in ('.mirror.lock', '.mirror-inbox.sqlite3-shm'):
                st = src.lstat()
                if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1:
                    raise ValueError('Unsafe transient vault entry')
                continue
            dest = destination / 'vault' / rel
            receipt = _copy_file(src, dest, SNAPSHOT_MAX_BYTES - total)
            total += receipt['size']
            key = rel.as_posix()
            receipt_size += (len(json.dumps(key).encode()) + 2 +
                             len(json.dumps(receipt, sort_keys=True).encode()) +
                             (2 if copied else 0))
            if receipt_size > LIMIT:
                raise ValueError('Snapshot receipt byte limit exceeded')
            copied[key] = receipt
    # Only open the copied database: opening the input WAL can create a -shm
    # file or checkpoint it. Quiescence is essential while copying DB + WAL.
    inbox = destination / 'vault' / '.mirror-inbox.sqlite3'
    wal = inbox.with_name(inbox.name + '-wal')
    if wal.exists() and not inbox.exists():
        raise ValueError('Orphan inbox WAL requires separate investigation')
    if inbox.exists():
        raw_inbox = destination / '.original-inbox'
        raw_inbox.mkdir(mode=0o700)
        _copy_file(inbox, raw_inbox / 'database', SNAPSHOT_MAX_FILE_BYTES)
        if wal.exists():
            _copy_file(wal, raw_inbox / 'wal', SNAPSHOT_MAX_FILE_BYTES)
        backup = inbox.with_name('.mirror-inbox-backup-' + uuid.uuid4().hex + '.sqlite3')
        _write_new(backup, b'')
        source = sqlite3.connect(inbox)
        target = sqlite3.connect(backup)
        try:
            source.backup(target)
            if target.execute('PRAGMA quick_check').fetchone() != ('ok',):
                raise ValueError('Inbox backup failed quick_check')
        finally:
            target.close()
            source.close()
        os.chmod(backup, 0o600)
        with backup.open('rb') as f:
            os.fsync(f.fileno())
        os.replace(backup, inbox)
        for suffix in ('-wal', '-shm'):
            inbox.with_name(inbox.name + suffix).unlink(missing_ok=True)
    receipt = json.dumps(copied, sort_keys=True).encode()
    if len(receipt) > LIMIT:
        raise ValueError('Snapshot receipt byte limit exceeded')
    _write_new(destination / '.snapshot-inventory.json', receipt)
    marker = {'version': 1, 'root': str(destination), 'id': uuid.uuid4().hex,
              'source_profile': str(profile), 'source_vault': str(vault),
              'inventory_sha256': _digest(receipt), 'copied_bytes': total, 'entries': count}
    _write_new(destination / MARKER, json.dumps(marker, sort_keys=True).encode())
    for root, dirs, _ in os.walk(destination, topdown=False):
        _sync(root)
    return marker


def _snapshot(root):
    root = _directory(root)
    if root.stat().st_mode & 0o077:
        raise ValueError('Snapshot root must be private')
    for child in ('vault', 'profile', 'profile/memories'):
        _directory(root / child)
    marker = _strict_json(_read(root / MARKER))
    if marker.get('version') != 1 or marker.get('root') != str(root):
        raise ValueError('Not an admitted detached snapshot')
    if 'inventory_sha256' in marker:
        receipt = _read(root / '.snapshot-inventory.json')
        if _digest(receipt) != marker['inventory_sha256']:
            raise ValueError('Snapshot inventory receipt changed')
    if str(root / 'vault') == marker.get('source_vault') or str(root / 'profile') == marker.get('source_profile'):
        raise ValueError('Snapshot cannot be an input')
    return root, marker


def _fact_inventory(vault):
    root = _directory(vault / 'facts')
    result, total = {}, 0
    for parent, dirs, files in os.walk(root, followlinks=False):
        parent = Path(parent)
        for name in dirs:
            _directory(parent / name)
        for name in files:
            if len(result) >= MAX_FILES:
                raise ValueError('Fact inventory count limit exceeded')
            path = parent / name
            data = _read(path)
            total += len(data)
            if total > LIMIT:
                raise ValueError('Fact inventory byte limit exceeded')
            result[str(path.relative_to(vault))] = {'sha256': _digest(data), 'identity': _identity(path)}
    return result


def _inventory(provider, root, marker):
    vault = root / 'vault'
    for name in ('.mirror-delivery-failed.json', '.index-refresh.json', '.reindex-request.json'):
        if (vault / name).exists() or (vault / name).is_symlink():
            raise ValueError('Pending delivery or index work requires separate investigation')
    map_path = vault / '.mirror-map.json'
    map_bytes = _read(map_path) if map_path.exists() or map_path.is_symlink() else None
    if map_bytes is not None:
        _strict_json(map_bytes)
    state = provider._mirror_state()
    if state['pending_creates'] or state['pending_deletes'] or state.get('refresh_required'):
        raise ValueError('Pending mirror recovery or refresh')
    provider._validate_replay_history(state)
    if provider._mirror_inbox.pending():
        raise ValueError('Pending notifications require separate investigation')
    staging = vault / '.mirror-staging'
    if staging.is_symlink() or (staging.exists() and any(staging.iterdir())):
        raise ValueError('Unresolved mirror staging entries')
    facts = _fact_inventory(vault)
    sources, entries = {}, {}
    for target, name in (('memory', 'MEMORY.md'), ('user', 'USER.md')):
        path = root / 'profile' / 'memories' / name
        data = _read(path) if path.exists() or path.is_symlink() else b''
        sources[name] = {'sha256': _digest(data), 'identity': _identity(path) if path.exists() else None}
        # Match MemoryStore._read_raw_checked: utf-8-sig decoding plus text
        # mode's universal-newline conversion before delimiter parsing. The
        # plan still binds the original source bytes, not normalized bytes.
        text = data.decode('utf-8-sig').replace('\r\n', '\n').replace('\r', '\n')
        values = [x.strip() for x in text.split('\n§\n') if x.strip()]
        if len(values) != len(set(values)):
            raise ValueError('Duplicate source entries are ambiguous')
        entries[target] = values
    if sum(map(len, entries.values())) > MAX_FILES or len(state['records']) > MAX_FILES:
        raise ValueError('Entry count limit exceeded')
    seen_paths, seen_entries, retired_entries, retire, missing, owned = set(), set(), set(), [], [], {}
    source_sets = {target: set(values) for target, values in entries.items()}
    total_owned = 0
    for key, record in state['records'].items():
        target, content, rel = record['target'], record['content'], record['path']
        if target not in entries or rel in seen_paths:
            raise ValueError('Ambiguous record ownership')
        if key != _digest((target + '\0' + content).encode()):
            raise ValueError('Invalid owned record identity')
        seen_paths.add(rel)
        path = provider._mirror_file(rel)
        data = _read(path)
        total_owned += len(data)
        if total_owned > LIMIT:
            raise ValueError('Owned fact byte limit exceeded')
        if not _owned_fact_exact(data, content, target):
            raise ValueError('Owned fact does not match map content')
        owned[rel] = {'sha256': _digest(data), 'identity': _identity(path)}
        # Apply the host's universal-newline semantics to legacy record
        # interpretation while preserving original content for its hash,
        # exact ownership check, archival bytes and retirement.
        normalized_content = content.removeprefix('\ufeff').replace('\r\n', '\n').replace('\r', '\n')
        parts = [x.strip() for x in normalized_content.split('\n§\n') if x.strip()]
        if not parts or len(set(parts)) != len(parts):
            raise ValueError('Ambiguous owned entries')
        needs_retirement = '\n§\n' in normalized_content or (len(parts) == 1 and content != parts[0])
        for part in parts:
            identity = (target, part)
            if identity in seen_entries or part not in source_sets[target]:
                raise ValueError('Duplicate or stale owned content')
            seen_entries.add(identity)
            if needs_retirement:
                retired_entries.add(identity)
        if needs_retirement:
            retire.append(key)
    for target, values in entries.items():
        for value in values:
            if (target, value) not in seen_entries or (target, value) in retired_entries:
                missing.append([target, value])
    map_hash = ({'sha256': _digest(map_bytes), 'identity': _identity(map_path)}
                if map_bytes is not None else None)
    return {'version': 1, 'snapshot_id': marker['id'], 'root': str(root),
            'sources': sources, 'map_sha256': map_hash, 'facts': facts, 'owned': owned,
            'watermark': provider._mirror_inbox.high_watermark(),
            'add': missing, 'retire': sorted(retire)}


def _open_provider(root):
    # Construct only the journal machinery: no startup, workers, indexing or replay.
    from . import ZvecMemoryProvider
    provider = ZvecMemoryProvider(config={})
    provider._vault = root / 'vault'
    provider._vault_lock = VaultLock(provider._vault / '.mirror.lock')
    # Offline contenders must not initialize SQLite concurrently with a peer's
    # long reconciliation transaction. MirrorInbox setup precedes plan/apply's
    # critical section, so admit it under the same cross-process vault lock.
    with provider._vault_lock:
        provider._mirror_inbox = MirrorInbox(provider._vault)
    return provider


def plan(root):
    root, marker = _snapshot(root)
    provider = _open_provider(root)
    try:
        with provider._vault_lock:
            proposal = _inventory(provider, root, marker)
        proposal['plan_sha256'] = _digest(json.dumps(proposal, sort_keys=True, ensure_ascii=False, separators=(',', ':')).encode())
        return proposal
    finally:
        provider._mirror_inbox.close()


def apply(root, expected_plan_sha256):
    """Apply only an exact reviewed proposal on the detached snapshot."""
    root, marker = _snapshot(root)
    provider = _open_provider(root)
    try:
        with provider._vault_lock:
            proposal = _inventory(provider, root, marker)
            digest = _digest(json.dumps(proposal, sort_keys=True, ensure_ascii=False, separators=(',', ':')).encode())
            if digest != expected_plan_sha256:
                raise ValueError('Stale or unconfirmed plan')
            if not proposal['add'] and not proposal['retire']:
                return {'status': 'unchanged', 'plan_sha256': digest}
            state = provider._mirror_state()
            # Save exact old bytes outside recall before publishing a journal.
            archive_root = root / 'vault' / '.reconcile-archive'
            archive_root.mkdir(mode=0o700, exist_ok=True)
            if archive_root.is_symlink() or archive_root.resolve() != archive_root:
                raise ValueError('Aliased archive')
            archive = archive_root / uuid.uuid4().hex
            archive.mkdir(mode=0o700)
            for name in ('MEMORY.md', 'USER.md'):
                path = root / 'profile' / 'memories' / name
                if path.exists():
                    _write_new(archive / name, _read(path))
            map_path = root / 'vault' / '.mirror-map.json'
            if map_path.exists():
                _write_new(archive / 'map.json', _read(map_path))
            for i, rel in enumerate(sorted(proposal['owned'])):
                _write_new(archive / ('fact-%d.bin' % i), _read(root / 'vault' / rel))
            with provider._mirror_inbox.connect() as src:
                dst = sqlite3.connect(archive / 'inbox.sqlite3')
                try:
                    src.backup(dst)
                finally:
                    dst.close()
            os.chmod(archive / 'inbox.sqlite3', 0o600)
            with (archive / 'inbox.sqlite3').open('rb') as f:
                os.fsync(f.fileno())
            _write_new(archive / 'plan.json', json.dumps(proposal, ensure_ascii=False, sort_keys=True).encode())
            _sync(archive)
            _sync(archive_root)
            _sync(root / 'vault')
            for target, content in proposal['add']:
                key = _digest((target + '\0' + content).encode())
                if key in state['records']:
                    raise ValueError('Unexpected record collision')
                staged = provider._write_fact(content, 'user_pref' if target == 'user' else 'general', 'mirror', directory='.mirror-staging')
                rel = 'facts/' + staged.name
                state['records'][key] = {'target': target, 'content': content, 'path': rel}
                state['pending_creates'].append({'staged': str(staged.relative_to(root / 'vault')), 'path': rel})
            for key in proposal['retire']:
                state['pending_deletes'].append(state['records'].pop(key)['path'])
            state['refresh_required'] = True
            _sync(root / 'vault' / '.mirror-staging')
            provider._save_mirror_state(state)
            provider._finish_mirror_deletes(state)
            return {'status': 'refresh_required', 'plan_sha256': digest, 'archive': str(archive)}
    finally:
        provider._mirror_inbox.close()
