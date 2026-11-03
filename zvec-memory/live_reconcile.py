"""One-time mirror cutover inside an independently enforced cooperative outage.

This module does not stop, start, fence, or recover services. Never reopen writers
on an error without independent inspection. It does not automate rollback.
"""
import hashlib
import json
import os
from pathlib import Path
import stat

from . import offline_reconcile as off

BINDING = '.live-reconcile-binding.json'


def _sources(profile):
    result = {}
    for name in ('MEMORY.md', 'USER.md'):
        path = profile / 'memories' / name
        try:
            st = path.lstat()
        except FileNotFoundError:
            result[name] = None
            continue
        data = off._read(path)
        after = path.lstat()
        identity = lambda s: [s.st_dev, s.st_ino, s.st_mode, s.st_nlink, s.st_size, s.st_mtime_ns, s.st_ctime_ns]
        if identity(st) != identity(after) or len(data) != st.st_size:
            raise ValueError('Source changed during capture')
        result[name] = {'identity': identity(st), 'sha256': off._digest(data)}
    return result


def _verify_input_vault(vault, receipt):
    observed = set()
    def fail(error):
        raise error
    for parent, dirs, files in os.walk(vault, followlinks=False, onerror=fail):
        parent = Path(parent)
        for name in dirs:
            if not stat.S_ISDIR((parent / name).lstat().st_mode):
                raise ValueError('Unsafe vault directory')
        for name in files:
            path = parent / name
            rel = path.relative_to(vault).as_posix()
            if rel in ('.mirror.lock', '.mirror-inbox.sqlite3-shm'):
                st = path.lstat()
                if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1:
                    raise ValueError('Unsafe transient entry')
                continue
            if rel not in receipt:
                raise ValueError('Vault changed since capture')
            record = receipt[rel]
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            try:
                st = os.fstat(fd)
                identity = lambda s: [s.st_dev, s.st_ino, s.st_mode, s.st_nlink, s.st_size, s.st_mtime_ns, s.st_ctime_ns]
                if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1 or identity(st) != record['source_identity']:
                    raise ValueError('Vault changed since capture')
                h = hashlib.sha256()
                size = 0
                while True:
                    chunk = os.read(fd, off.COPY_CHUNK)
                    if not chunk:
                        break
                    size += len(chunk)
                    h.update(chunk)
                if (size != record['size'] or h.hexdigest() != record['sha256'] or
                        identity(os.fstat(fd)) != identity(st) or identity(path.lstat()) != identity(st)):
                    raise ValueError('Vault changed since capture')
            finally:
                os.close(fd)
            observed.add(rel)
    if observed != set(receipt):
        raise ValueError('Vault changed since capture')


def prepare(profile, vault, destination):
    """Fresh backup and reviewable plan, while all external writers remain stopped."""
    profile, vault = off._directory(profile), off._directory(vault)
    off._directory(profile / 'memories')
    before = _sources(profile)
    marker = off.create_snapshot(profile, vault, destination)
    root = Path(destination)
    after = _sources(profile)
    if before != after:
        raise ValueError('Source changed during snapshot')
    for name, record in after.items():
        copy = root / 'profile' / 'memories' / name
        if (record is None) != (not copy.exists()):
            raise ValueError('Source snapshot mismatch')
        if record is not None and off._digest(off._read(copy)) != record['sha256']:
            raise ValueError('Source snapshot mismatch')
    receipt = off._strict_json(off._read(root / '.snapshot-inventory.json'))
    _verify_input_vault(vault, receipt)
    proposal = off.plan(root)
    binding = {'version': 1, 'snapshot_id': marker['id'], 'profile': str(profile),
               'vault': str(vault), 'sources': after, 'plan_sha256': proposal['plan_sha256']}
    off._write_new(root / BINDING, json.dumps(binding, sort_keys=True).encode())
    off._sync(root)
    return proposal


def commit(snapshot, expected_plan_sha256):
    """Commit under the SAME outage; on any error leave admission stopped."""
    root, marker = off._snapshot(snapshot)
    binding = off._strict_json(off._read(root / BINDING))
    if (binding.get('version') != 1 or binding.get('snapshot_id') != marker['id'] or
            binding.get('plan_sha256') != expected_plan_sha256 or
            binding.get('profile') != marker['source_profile'] or
            binding.get('vault') != marker['source_vault']):
        raise ValueError('Invalid or unconfirmed live binding')
    profile, vault = off._directory(binding['profile']), off._directory(binding['vault'])
    off._directory(profile / 'memories')
    receipt = off._strict_json(off._read(root / '.snapshot-inventory.json'))
    def check():
        if _sources(profile) != binding['sources']:
            raise ValueError('Source changed since capture')
        _verify_input_vault(vault, receipt)
    return off._apply(root, expected_plan_sha256, profile=profile, vault=vault, live_check=check)
