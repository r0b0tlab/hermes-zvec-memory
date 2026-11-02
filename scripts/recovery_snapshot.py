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


FTS_SQL = {
    'messages_fts_src': "CREATE VIEW messages_fts_src AS SELECT id, CASE WHEN role = 'tool' THEN substr(COALESCE(content, ''), 1, 8192) ELSE content END AS content, tool_name, tool_calls FROM messages",
    'messages_fts_trigram_src': "CREATE VIEW messages_fts_trigram_src AS SELECT m.id, m.role, m.content, m.tool_name FROM messages AS m JOIN sessions AS s ON s.id = m.session_id WHERE m.role <> 'tool' AND s.source NOT IN ('cron', 'subagent') AND json_extract(COALESCE(s.model_config, '{}'), '$._delegate_from') IS NULL",
    'messages_fts': "CREATE VIRTUAL TABLE messages_fts USING fts5(content, tool_name, tool_calls, content='messages_fts_src', content_rowid='id')",
    'messages_fts_trigram': "CREATE VIRTUAL TABLE messages_fts_trigram USING fts5(content, tool_name, content='messages_fts_trigram_src', content_rowid='id', tokenize='trigram')",
}
# The second allowlisted representation is transcribed from sqlite_schema on
# the observed host. Compare raw strings: whitespace inside literals/comments
# must never be normalized into an allowed statement.
FTS_HOST_SQL = {
    'messages_fts_trigram': """CREATE VIRTUAL TABLE messages_fts_trigram USING fts5(
    content,
    tool_name,
    content='messages_fts_trigram_src',
    content_rowid='id',
    tokenize='trigram'
)""",
    'messages_fts': """CREATE VIRTUAL TABLE messages_fts USING fts5(
    content,
    tool_name,
    tool_calls,
    content='messages_fts_src',
    content_rowid='id'
)""",
    'messages_fts_src': """CREATE VIEW messages_fts_src AS
    SELECT id,
           CASE WHEN role = 'tool'
                THEN substr(COALESCE(content, ''), 1, 8192)
                ELSE content END AS content,
           tool_name, tool_calls
    FROM messages""",
    'messages_fts_trigram_src': """CREATE VIEW messages_fts_trigram_src AS
    SELECT m.id, m.role, m.content, m.tool_name
    FROM messages AS m
    JOIN sessions AS s ON s.id = m.session_id
    WHERE m.role <> 'tool' AND s.source NOT IN ('cron', 'subagent') AND json_extract(COALESCE(s.model_config, '{}'), '$._delegate_from') IS NULL""",
}
FTS_NAMES = ('messages_fts', 'messages_fts_trigram')


def sqlite_connect(*args, **kwargs):
    """Own each connection; no caller-supplied modules or loaded extensions."""
    db = sqlite3.connect(*args, **kwargs)
    try:
        db.enable_load_extension(False)
    except (AttributeError, sqlite3.Error) as exc:
        db.close()
        raise Refusal('cannot explicitly disable SQLite extensions') from exc
    return db


def admit_sqlite_schema(db):
    """Admit only the exact paired host FTS schema under trusted SQLite."""
    rows = list(db.execute('PRAGMA main.table_list'))
    main = {row[1]: row[2] for row in rows if row[0] == 'main'}
    if main.get('sqlite_schema') != 'table':
        raise Refusal('SQLite table inventory unavailable')
    if any(kind not in ('table', 'view', 'virtual', 'shadow') for kind in main.values()):
        raise Refusal('unsupported SQLite table kind')
    special = {name: kind for name, kind in main.items() if kind in ('virtual', 'shadow')}
    if not special:
        return False
    expected = {name: 'virtual' for name in FTS_NAMES}
    expected.update({f'{name}_{suffix}': 'shadow' for name in FTS_NAMES
                     for suffix in ('data', 'idx', 'docsize', 'config')})
    if special != expected:
        raise Refusal('SQLite virtual tables and shadow tables are unsupported')
    schema = {name: (kind, owner, sql) for kind, name, owner, sql in
              db.execute('SELECT type, name, tbl_name, sql FROM main.sqlite_schema')}
    for name, sql in FTS_SQL.items():
        kind = 'view' if name.endswith('_src') else 'table'
        if (schema.get(name) not in ((kind, name, sql), (kind, name, FTS_HOST_SQL[name]))
                or main.get(name) != ('view' if kind == 'view' else 'virtual')):
            raise Refusal('unsupported SQLite FTS schema')
    suffixes = {
        'data': '(id INTEGER PRIMARY KEY, block BLOB)',
        'idx': '(segid, term, pgno, PRIMARY KEY(segid, term)) WITHOUT ROWID',
        'docsize': '(id INTEGER PRIMARY KEY, sz BLOB)',
        'config': '(k PRIMARY KEY, v) WITHOUT ROWID',
    }
    for name in FTS_NAMES:
        for suffix, definition in suffixes.items():
            shadow = f'{name}_{suffix}'
            if schema.get(shadow) != ('table', shadow, f"CREATE TABLE '{shadow}'{definition}"):
                raise Refusal('unsupported SQLite FTS shadow schema')
    if any(main.get(name) != 'table' for name in ('messages', 'sessions')):
        raise Refusal('unsupported SQLite FTS content dependencies')
    # Compile each exact view with an authorizer: every resolved read, including
    # underlying dependencies, must be in main, and only the two base tables.
    for view, bases in (('messages_fts_src', {'messages'}),
                        ('messages_fts_trigram_src', {'messages', 'sessions'})):
        seen = set()
        def authorizer(action, arg1, arg2, database, origin):
            if action == sqlite3.SQLITE_READ:
                if database != 'main' or arg1 not in bases | {view}:
                    return sqlite3.SQLITE_DENY
                if arg1 in bases:
                    seen.add(arg1)
            return sqlite3.SQLITE_OK
        db.set_authorizer(authorizer)
        try:
            db.execute(f'SELECT * FROM main.{view} LIMIT 0').fetchall()
        except sqlite3.Error as exc:
            raise Refusal('unsupported SQLite FTS content dependencies') from exc
        finally:
            db.set_authorizer(None)
        if seen != bases:
            raise Refusal('unsupported SQLite FTS content dependencies')
    return True


def check_fts(db):
    """Rank-1 checks compare both indexes to their main-schema content views."""
    try:
        db.execute('SAVEPOINT fts_validation')
        for name in FTS_NAMES:
            db.execute(f"INSERT INTO main.{name}({name}, rank) VALUES('integrity-check', 1)")
    except sqlite3.Error as exc:
        raise Refusal('SQLite FTS integrity check failed') from exc
    finally:
        db.execute('ROLLBACK TO fts_validation')
        db.execute('RELEASE fts_validation')


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


def file_identity(st):
    """Metadata used to detect source changes while a file is captured."""
    return (st.st_dev, st.st_ino, st.st_mode, st.st_nlink, st.st_size,
            st.st_mtime_ns, st.st_ctime_ns)


def stream_receipt(stream):
    """Count and hash the entire stream, independently of the copy."""
    stream.seek(0)
    sha = hashlib.sha256()
    size = 0
    for block in iter(lambda: stream.read(1024 * 1024), b''):
        size += len(block)
        sha.update(block)
    return size, sha.hexdigest()


def capture_file(source, dest, *, evidence=False):
    """Bind a copy to a stable, independently read source before attesting it."""
    label = 'SQLite evidence' if evidence else 'ordinary file'
    regular(source)
    before = file_identity(source.lstat())
    with source.open('rb') as src:
        def stable():
            return (file_identity(os.fstat(src.fileno())) == before
                    and file_identity(source.lstat()) == before)
        if not stable():
            raise Refusal(f'{label} source drift: {source}')
        expected = stream_receipt(src)
        if expected[0] != before[4] or not stable():
            raise Refusal(f'{label} source drift: {source}')
        src.seek(0)
        with dest.open('xb') as dst:
            shutil.copyfileobj(src, dst)
        os.chmod(dest, 0o600)
        if not stable() or stream_receipt(src) != expected or not stable():
            raise Refusal(f'{label} source drift: {source}')
        regular(dest)
        if (dest.stat().st_size != expected[0] or digest(dest) != expected[1]
                or not same_bytes(source, dest)):
            raise Refusal(f'{label} copy drift: {source}')
        if not stable():
            raise Refusal(f'{label} source drift: {source}')
    return expected


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
        frames, commit = 0, 0
        while True:
            frame = f.read(24 + page)
            if not frame: break
            if len(frame) != 24 + page: bad()
            pgno, commit, s1, s2, c1, c2 = struct.unpack('>6I', frame[:24])
            if not pgno or (s1, s2) != (salt1, salt2): bad()
            state = checksum(frame[:8] + frame[24:], endian, state)
            if state != (c1, c2): bad()
            frames += 1
        if not frames or not commit: bad()
    # Stream the WAL-index as 32 KiB blocks. Never retain all frames or SHM.
    index_size = shm.stat().st_size
    if index_size < 32768 or index_size % 32768: bad()
    with shm.open('rb') as f:
        first = f.read(136)
    h = first[:48]
    if h != first[48:96]: bad()
    version, _, _, init, big, sz, index_frames, dbsize, ck1, ck2 = struct.unpack('=IIIBBHIIII', h[:32])
    if (version != 3007000 or init != 1 or big != (magic & 1)
            or (65536 if sz == 1 else sz) != page or index_frames != frames or dbsize != commit
            or (ck1, ck2) != state or h[32:40] != wh[16:24]
            or checksum(h[:40], '=') != struct.unpack('=II', h[40:48])):
        bad()
    backfill, *marks = struct.unpack('=6I', first[96:120])
    attempted, unused = struct.unpack('=2I', first[128:136])
    if (backfill > frames or attempted > frames or unused or first[120:128] != bytes(8)
            or any(mark != 0xffffffff and mark > frames for mark in marks)):
        bad()
    # Independently rebuild and compare each index block, matching frame page
    # numbers directly to WAL; memory is bounded by one index block.
    offset = 0
    with shm.open('rb') as idx, wal.open('rb') as log:
        log.seek(32)
        for block in range(index_size // 32768):
            chunk = idx.read(32768)
            capacity = 4062 if block == 0 else 4096
            count = min(capacity, frames - offset)
            if block and count <= 0: bad()
            start = 136 if block == 0 else 0
            array = bytearray(capacity * 4)
            table = bytearray(8192 * 2)
            for i in range(1, count + 1):
                header = log.read(24)
                if len(header) != 24: bad()
                pgno = struct.unpack('>I', header[:4])[0]
                log.seek(page, os.SEEK_CUR)
                struct.pack_into('=I', array, (i - 1) * 4, pgno)
                slot = (pgno * 383) % 8192
                while struct.unpack_from('=H', table, slot * 2)[0]:
                    slot = (slot + 1) % 8192
                struct.pack_into('=H', table, slot * 2, i)
            if chunk[start:start + len(array)] != array or chunk[start + len(array):] != table:
                bad()
            offset += count
    if offset != frames: bad()


def same_bytes(first, second):
    with open(first, 'rb') as left, open(second, 'rb') as right:
        while True:
            a, b = left.read(1024 * 1024), right.read(1024 * 1024)
            if a != b:
                return False
            if not a:
                return True


def checked_copy(source, dest, expected):
    """Check both the admitted input and the resulting copy against its receipt."""
    size, sha = expected
    regular(source)
    if source.stat().st_size != size or digest(source) != sha:
        raise Refusal(f'SQLite evidence drift: {source}')
    shutil.copyfile(source, dest)
    path = Path(dest)
    regular(path)
    if path.stat().st_size != size or digest(path) != sha or not same_bytes(source, path):
        raise Refusal(f'SQLite copy drift: {path}')


def compare_sqlite_pages(src, dst, source, output, work):
    """Compare the committed pager image reconstructed from raw main + WAL.

    This is stronger than comparing SQL rows: it includes every page, including
    rowids, internal metadata and unused bytes. Only explicitly admitted
    main-schema FTS may have virtual or shadow tables. Only the two
    volatile page-one header words historically exempted by this tool differ.
    The on-disk map keeps the latest committed WAL frame per page without
    holding a database-sized image or a frame-sized index in memory.
    """
    def different():
        raise Refusal('SQLite backup differs from admitted source logical pages')

    if any(Path(str(output) + suffix).exists() for suffix in ('-wal', '-shm', '-journal')):
        different()
    admit_sqlite_schema(src)
    admit_sqlite_schema(dst)
    # A pager comparison covers only main. SQLite may initialize an empty temp
    # schema during integrity_check; no temp objects or attached DBs are covered.
    for db in (src, dst):
        names = [row[1] for row in db.execute('PRAGMA database_list')]
        if (not names or names[0] != 'main' or any(name not in ('main', 'temp') for name in names)
                or ('temp' in names and db.execute('SELECT 1 FROM temp.sqlite_schema LIMIT 1').fetchone())):
            raise Refusal('SQLite attached databases are unsupported')
    page = src.execute('PRAGMA page_size').fetchone()[0]
    count = src.execute('PRAGMA page_count').fetchone()[0]
    if (page not in (512, 1024, 2048, 4096, 8192, 16384, 32768, 65536)
            or count < 1 or dst.execute('PRAGMA page_size').fetchone()[0] != page
            or dst.execute('PRAGMA page_count').fetchone()[0] != count
            or output.stat().st_size != page * count):
        different()
    wal = Path(str(source) + '-wal')
    # Map file lives solely in the private temporary workspace. Cache is bounded;
    # disk use grows with the number of distinct WAL pages, not database size.
    with tempfile.TemporaryDirectory(prefix='page-map-', dir=work) as mapping:
        with closing(sqlite_connect(Path(mapping) / 'map.db')) as index:
            index.execute('PRAGMA journal_mode=OFF')
            index.execute('PRAGMA synchronous=OFF')
            index.execute('PRAGMA cache_size=-2048')
            index.execute('PRAGMA mmap_size=0')
            index.execute('CREATE TABLE frames (page INTEGER PRIMARY KEY, offset INTEGER NOT NULL)')
            if wal.exists():
                with wal.open('rb') as log:
                    header = log.read(32)
                    if len(header) != 32 or int.from_bytes(header[8:12], 'big') != page:
                        different()
                    commit = 0
                    while True:
                        frame = log.read(24)
                        if not frame: break
                        if len(frame) != 24: different()
                        pgno, commit = struct.unpack('>II', frame[:8])
                        if not pgno: different()
                        index.execute('INSERT OR REPLACE INTO frames VALUES (?, ?)', (pgno, log.tell()))
                        log.seek(page, os.SEEK_CUR)
                    if commit != count or log.tell() != wal.stat().st_size: different()
            index.commit()
            offsets = iter(index.execute('SELECT page, offset FROM frames WHERE page <= ? ORDER BY page', (count,)))
            next_frame = next(offsets, None)
            with source.open('rb') as main, output.open('rb') as backup, (wal.open('rb') if wal.exists() else open(os.devnull, 'rb')) as log:
                for pgno in range(1, count + 1):
                    if next_frame is not None and next_frame[0] == pgno:
                        log.seek(next_frame[1])
                        expected = log.read(page)
                        next_frame = next(offsets, None)
                    else:
                        main.seek((pgno - 1) * page)
                        expected = main.read(page)
                    actual = backup.read(page)
                    if len(expected) != page or len(actual) != page:
                        different()
                    if pgno == 1:
                        if expected[:24] != actual[:24] or expected[28:92] != actual[28:92] or expected[96:] != actual[96:]:
                            different()
                    elif expected != actual:
                        different()
            if next_frame is not None: different()


def sqlite_copy(raw, evidence_base, dest, scratch_parent, *, admitted=None):
    sidecar = lambda suffix: Path(str(evidence_base) + suffix)
    if sidecar('-journal').exists():
        raise Refusal(f'ambiguous SQLite journal: {sidecar("-journal")}')
    wal, shm = sidecar('-wal'), sidecar('-shm')
    inputs = {'': raw, **{s: sidecar(s) for s in ('-wal', '-shm') if sidecar(s).exists()}}
    # Callers pass the capture receipts and, where available, the original
    # captured paths rather than an intermediate copy for the reference lane.
    if admitted is None:
        admitted = {s: (p, (p.stat().st_size, digest(p))) for s, p in inputs.items()}
    if set(admitted) != set(inputs):
        raise Refusal('SQLite evidence sidecar drift')
    for suffix, path in inputs.items():
        expected = admitted[suffix][1]
        regular(path)
        if path.stat().st_size != expected[0] or digest(path) != expected[1]:
            raise Refusal(f'SQLite evidence drift: {path}')
    if wal.exists() or shm.exists(): validate_wal(raw, wal, shm)
    # Two independent, byte-validated copies of admitted evidence. SQLite
    # never opens original evidence or its SHM; it builds scratch WAL indexes.
    with tempfile.TemporaryDirectory(prefix='.sqlite-work-', dir=scratch_parent) as work:
        def progress(status, remaining, total):
            if status in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED):
                raise Refusal(f'SQLite database locked: {raw}')
        for lane, output in (('source', dest), ('reference', Path(work) / 'reference-backup.db')):
            folder = Path(work) / lane
            folder.mkdir(mode=0o700)
            copied = folder / ('source.db' if lane == 'source' else 'reference.db')
            lane_inputs = inputs if lane == 'source' else {s: item[0] for s, item in admitted.items()}
            # Validate a copied SHM too, but do not give it to SQLite.
            for suffix, path in lane_inputs.items():
                checked_copy(path, str(copied) + suffix, admitted[suffix][1])
            if '-wal' in lane_inputs or '-shm' in lane_inputs:
                validate_wal(copied, Path(str(copied) + '-wal'), Path(str(copied) + '-shm'))
                Path(str(copied) + '-shm').unlink()
            # Only a sidecar-free admitted image may be opened immutable. A
            # normal read-only SQLite open of a closed WAL-mode database can
            # create an empty WAL/SHM, which is not admitted evidence.
            no_wal = '-wal' not in lane_inputs
            uri = ('file:' + quote(str(copied.absolute()), safe='/') + '?mode=ro'
                   + ('&immutable=1' if no_wal else ''))
            def require_no_sidecars():
                if no_wal and any(os.path.lexists(str(copied) + s)
                                  for s in ('-wal', '-shm', '-journal')):
                    raise Refusal('SQLite staged sidecar drift')
            require_no_sidecars()
            with closing(sqlite_connect(uri, uri=True, timeout=0)) as src:
                has_fts = admit_sqlite_schema(src)
                if src.execute('PRAGMA integrity_check').fetchall() != [('ok',)]:
                    raise Refusal(f'SQLite source integrity check failed: {raw}')
                with closing(sqlite_connect(output)) as dst:
                    # SQLite's backup API increments the destination's schema
                    # cookie. Seed it so the resulting physical image can still
                    # be checked against the *source* cookie without exemption.
                    cookie = src.execute('PRAGMA schema_version').fetchone()[0]
                    predecessor = (cookie - 1 + 2**31) % 2**32 - 2**31
                    dst.execute(f'PRAGMA schema_version={predecessor}')
                    src.backup(dst, pages=-1, progress=progress, sleep=0)
                    if admit_sqlite_schema(dst) != has_fts:
                        raise Refusal('SQLite FTS schema mismatch')
                    if dst.execute('PRAGMA integrity_check').fetchall() != [('ok',)]:
                        raise Refusal(f'SQLite integrity check failed: {raw}')
                    if has_fts:
                        check_fts(dst)
                # Closing the backup connection checkpoints any destination WAL;
                # compare only the resulting stable, sidecar-free backup bytes.
                dest_uri = 'file:' + quote(str(output.absolute()), safe='/') + '?mode=ro&immutable=1'
                require_no_sidecars()
                with closing(sqlite_connect(dest_uri, uri=True)) as dst:
                    compare_sqlite_pages(src, dst, copied, output, work)
                require_no_sidecars()
                # Rebind the exact pager inputs after SQLite has finished reading.
                for suffix in ('', '-wal'):
                    if suffix in lane_inputs:
                        path = Path(str(copied) + suffix)
                        size, sha = admitted[suffix][1]
                        if not path.is_file() or path.stat().st_size != size or digest(path) != sha:
                            raise Refusal(f'SQLite evidence drift: {path}')
        reference = Path(work) / 'reference-backup.db'
        # Exact backup-page identity is stronger than a SQL dump comparison and
        # includes schema, hidden data and committed WAL pages. Refuse even
        # harmless physical differences rather than claiming logical equality.
        expected = (reference.stat().st_size, digest(reference))
        if (dest.stat().st_size != expected[0] or digest(dest) != expected[1]
                or not same_bytes(dest, reference)):
            raise Refusal(f'SQLite backup differs from admitted source: {raw}')
    os.chmod(dest, 0o600)
    return expected


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
            original = rel.as_posix()
            admitted = {}
            for suffix in ('', '-wal', '-shm'):
                matches = [e for e in records if e.get('source_path') == original + suffix]
                if matches:
                    entry = matches[0]
                    admitted[suffix] = (args.destination / entry['path'], (entry['size'], entry['sha256']))
            expected = sqlite_copy(args.destination / 'raw-database-evidence' / rel,
                                   args.destination / 'sidecar-evidence' / rel,
                                   dest, args.destination, admitted=admitted)
        else:
            expected = capture_file(source, dest, evidence=source_path is not None)
        size, sha = expected
        record = {'path': rel.as_posix(), 'type': kind, 'size': size, 'sha256': sha}
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
    sha = digest(manifest_path)
    verify(args.destination, sha)
    print('manifest_sha256=' + sha)


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
                with closing(sqlite_connect(uri, uri=True)) as db:
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
                with closing(sqlite_connect(uri, uri=True)) as db:
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
