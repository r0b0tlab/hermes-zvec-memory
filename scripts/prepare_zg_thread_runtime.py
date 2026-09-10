#!/usr/bin/env python3
"""Prepare a private zg package; never import zg or initialize native code.

Offline integration (NEXT PHASE ONLY, after active fault batches finish):
  python3 scripts/prepare_zg_thread_runtime.py \
    --source .test-tools/node_modules/@zvec/zvec-grep \
    --expected-sha256 724aac71d4574b6902fc88c26e198731c70b09791988a7a0f7813bbe85befec1

The example pin was independently captured from installed zg 0.2.2. It is
required input, not recomputed as an acceptance criterion. Only storage/zvec.js
is patched, inside its existing first-initialization guard. Defaults: query=1,
optimize=1. These are requested pool sizes, NOT a total process thread cap.
Native first initialization wins: use a fresh process in the future adapter.

The manifest entrypoint is the copied normal CLI (use [node, entrypoint, *args]
or its preserved executable mode). No pre-import shim or CLI invocation occurs
here. Only offline fixture entrypoints are exercised by unit tests. Production
CLI --liftoff-only is deliberately NOT executed: imports could load native code.

Dependency package roots are linked to their original realpaths, preserving
nested native bindings and avoiding copying model/native payloads. Links are
read-only by preparer policy, NOT an OS sandbox; this runtime depends on the
installation remaining unchanged. Literal emitted bare ESM imports and platform
binding resolution are verified by Node without importing package code. This
is not native ABI/workload validation or a general JavaScript parser. The pin
covers storage/zvec.js, NOT the native binary or dependency payloads.

Canonical physical source/destination containment is checked before any writes.
Final no-follow inventories require the whole source to remain unchanged and
exactly the storage file to differ by the patch derived from the pinned bytes.
Only declared dependency symlinks are excluded from the copied file inventory.
This is offline integrity checking, not isolation against a concurrent hostile
filesystem writer; keep the installation and destination quiescent throughout.

Destination must be absent; failed preparation may leave a private directory
without a manifest. Never reuse it. Do not select this runtime in the running
stress/soak campaign; later adapter wiring must record this manifest and measure
observed threads independently. No harness changes or commits are made here.
"""
import hashlib
import json
from pathlib import Path
import shutil
import os
import subprocess

# This program only imports Node builtins. resolve() never loads package code.
RESOLVE_JS = r'''
import fs from 'node:fs';
import path from 'node:path';
import {createRequire} from 'node:module';
import {pathToFileURL, fileURLToPath} from 'node:url';
const source = process.argv[1];
const req = createRequire(path.join(source, 'package.json'));
const meta = JSON.parse(fs.readFileSync(path.join(source, 'package.json'), 'utf8'));
function locate(name, from) {
  if (!/^(?:@[a-zA-Z0-9_.-]+\/)?[a-zA-Z0-9_.-]+$/.test(name)) throw Error('unsafe dependency name');
  for (const base of from.resolve.paths(name) || []) {
    const file = path.join(base, name, 'package.json');
    if (fs.existsSync(file)) {
      const root = fs.realpathSync(path.dirname(file));
      const pkg = JSON.parse(fs.readFileSync(file, 'utf8'));
      if (pkg.name !== name) throw Error('dependency name mismatch: '+name);
      return {path: root, version: pkg.version};
    }
  }
  throw Error('missing dependency: '+name);
}
const dependencies = {};
const missingOptional = [];
for (const name of Object.keys({...meta.dependencies, ...meta.optionalDependencies})) {
  try { dependencies[name] = locate(name, req); }
  catch (err) {
    if (name in (meta.dependencies || {})) throw err;
    missingOptional.push(name);
  }
}
const sdk = dependencies['@zvec/zvec'];
if (!sdk || sdk.version !== '0.7.1') throw Error('expected @zvec/zvec 0.7.1');
const sdkReq = createRequire(path.join(sdk.path, 'package.json'));
const libc = process.platform === 'linux' && !process.report.getReport().header.glibcVersionRuntime ? '-musl' : '';
const bindingName = '@zvec/bindings-'+process.platform+'-'+process.arch+libc;
const binding = locate(bindingName, sdkReq);
if (binding.version !== '0.7.1') throw Error('expected binding 0.7.1');
binding.name = bindingName;
binding.entrypoint = fs.realpathSync(sdkReq.resolve(bindingName));
if (!binding.entrypoint.endsWith('.node') || !fs.statSync(binding.entrypoint).isFile()) throw Error('binding is not a native file');
const imports = {};
function scan(dir) {
  for (const item of fs.readdirSync(dir, {withFileTypes:true})) {
    if (item.name === 'node_modules') continue;
    const file = path.join(dir, item.name);
    if (item.isDirectory()) { scan(file); continue; }
    if (!item.isFile() || !file.endsWith('.js')) continue;
    const text = fs.readFileSync(file, 'utf8');
    // Pinned emitted JS: static from, side-effect imports, literal dynamic imports.
    const pattern = /(?:^(?:import|export)\s+(?:[^;\n]*?\sfrom\s*)?|\bimport\s*\(\s*)["']([^"'\n]+)["']/gm;
    for (const match of text.matchAll(pattern)) {
      const spec = match[1];
      if (spec.startsWith('.') || spec.startsWith('node:') || spec.startsWith('/')) continue;
      const url = import.meta.resolve(spec, pathToFileURL(file).href);
      imports[path.relative(source, file)+' :: '+spec] = fs.realpathSync(fileURLToPath(url));
    }
  }
}
scan(source);
console.log(JSON.stringify({dependencies, imports, missing_optional: missingOptional, native_binding: binding, node_version: process.version}));
'''


def node_metadata(source):
    env = {k: v for k, v in os.environ.items() if not k.startswith('NODE_')}
    result = subprocess.run(['node', '--experimental-import-meta-resolve', '--input-type=module', '--eval', RESOLVE_JS, str(source)],
                            capture_output=True, text=True, timeout=30, env=env)
    if result.returncode:
        raise ValueError('Node resolution failed: ' + result.stderr.strip())
    return json.loads(result.stdout)


def file_hashes(root, *, dependency_links=None, independent=False):
    """Inventory regular files without following symlinks (including dangling ones).

    Only explicitly declared dependency links are excluded, never arbitrary
    node_modules contents. Descriptor-relative traversal refuses symlink swaps.
    """
    import stat
    allowed = dependency_links or {}
    seen_links = set()
    hashes = {}

    def scan(directory, relative):
        for name in sorted(os.listdir(directory)):
            key = str(relative / name)
            info = os.stat(name, dir_fd=directory, follow_symlinks=False)
            if stat.S_ISLNK(info.st_mode):
                if key not in allowed or os.readlink(name, dir_fd=directory) != allowed[key]:
                    raise ValueError('package contains an unexpected symlink: ' + key)
                seen_links.add(key)
                continue
            if key in allowed:
                raise ValueError('expected dependency symlink: ' + key)
            if stat.S_ISDIR(info.st_mode):
                child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
                try:
                    scan(child, relative / name)
                finally:
                    os.close(child)
            elif stat.S_ISREG(info.st_mode):
                fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
                with os.fdopen(fd, 'rb') as stream:
                    current = os.fstat(stream.fileno())
                    if not stat.S_ISREG(current.st_mode):
                        raise ValueError('package contains a special file: ' + key)
                    if independent and current.st_nlink != 1:
                        raise ValueError('copied package contains a shared inode: ' + key)
                    hashes[key] = hashlib.file_digest(stream, 'sha256').hexdigest()
            else:
                raise ValueError('package contains a special file: ' + key)

    directory = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        scan(directory, Path())
    finally:
        os.close(directory)
    if seen_links != set(allowed):
        raise ValueError('missing dependency symlink')
    return hashes

STORAGE = Path("dist/engine/storage/zvec.js")
INITIALIZER = "ZVecInitialize({ logLevel: ZVecLogLevel.WARN });"


def prepare_runtime(source, destination, *, expected_sha256, query_threads=1, optimize_threads=1):
    query_threads = validate_budget(query_threads)
    optimize_threads = validate_budget(optimize_threads)
    source, destination = Path(source).absolute(), Path(destination).absolute()
    for path in (destination, *destination.parents):
        if path.is_symlink():
            raise ValueError("destination or ancestor is a symlink")
    if destination.exists() or source.is_symlink():
        raise ValueError("destination must be new and source must not be a symlink")
    # Resolve source ancestor aliases and '..' before physical containment checks.
    # Keep the lexical destination symlink refusal above, even for aliases that
    # resolve outside the source. No destination writes precede these checks.
    source, destination = source.resolve(strict=True), destination.resolve()
    if source == destination or source in destination.parents or destination in source.parents:
        raise ValueError("source and destination must be disjoint")
    metadata = json.loads((source / "package.json").read_text())
    if metadata.get("name") != "@zvec/zvec-grep" or metadata.get("version") != "0.2.2":
        raise ValueError("expected @zvec/zvec-grep 0.2.2")
    entry = Path(metadata.get("bin", {}).get("zg", ""))
    if entry.is_absolute() or ".." in entry.parts or not (source / entry).is_file():
        raise ValueError("unsafe or missing zg entrypoint")
    for path in source.rglob("*"):
        if path.is_symlink() or not (path.is_dir() or path.is_file()):
            raise ValueError("source package contains symlink or special file")
    original = (source / STORAGE).read_bytes()
    if (not isinstance(expected_sha256, str) or len(expected_sha256) != 64
            or any(c not in "0123456789abcdef" for c in expected_sha256)
            or hashlib.sha256(original).hexdigest() != expected_sha256):
        raise ValueError("source SHA-256 does not match explicit expected hash")
    guarded = ("function initializeZvec() {\n"
               "    if (zvecInitialized) {\n"
               "        return;\n"
               "    }\n"
               "    " + INITIALIZER + "\n"
               "    zvecInitialized = true;\n}").encode()
    if original.count(guarded) != 1 or original.count(INITIALIZER.encode()) != 1:
        raise ValueError("expected exactly one guarded initializer")
    source_hashes = file_hashes(source)
    if source_hashes[str(STORAGE)] != expected_sha256:
        raise ValueError('source changed after pinned storage read')
    resolution = node_metadata(source)
    if file_hashes(source) != source_hashes:
        raise ValueError('source changed during dependency resolution')
    destination.mkdir(parents=True, mode=0o700)
    copied = destination / "package"
    shutil.copytree(source, copied, ignore=shutil.ignore_patterns('node_modules'))
    for path in copied.rglob('*'):
        if path.is_symlink():
            raise ValueError('copied package contains a symlink before patch')
        if path.is_file() and (path.stat().st_nlink != 1 or path.samefile(source / path.relative_to(copied))):
            raise ValueError('copied package contains a shared inode')
    if file_hashes(copied) != source_hashes or file_hashes(source) != source_hashes:
        raise ValueError('source or copy changed during preparation')
    target = copied / STORAGE
    patched = original.replace(INITIALIZER.encode(), (
        "ZVecInitialize({ logLevel: ZVecLogLevel.WARN, "
        f"queryThreads: {query_threads}, optimizeThreads: {optimize_threads} "
        "});").encode())
    target.write_bytes(patched)
    metadata = json.loads((copied / "package.json").read_text())
    for name, dependency in resolution['dependencies'].items():
        link = copied / 'node_modules' / name
        link.parent.mkdir(parents=True, exist_ok=True)
        link.symlink_to(dependency['path'], target_is_directory=True)
    verified_resolution = node_metadata(copied)
    if verified_resolution != resolution:
        raise ValueError('copied package dependency resolution differs')
    dependency_links = {str(Path('node_modules') / name): dependency['path']
                        for name, dependency in resolution['dependencies'].items()}
    copied_hashes = file_hashes(copied, dependency_links=dependency_links, independent=True)
    if file_hashes(source) != source_hashes:
        raise ValueError('source changed during final verification')
    expected_copy_hashes = {**source_hashes, str(STORAGE): hashlib.sha256(patched).hexdigest()}
    changed = {name for name in source_hashes if copied_hashes.get(name) != source_hashes[name]}
    if copied_hashes != expected_copy_hashes or changed != {str(STORAGE)}:
        raise ValueError('copy changed beyond the exact expected storage patch')
    manifest = {
        **resolution,
        "source_package": str(source),
        "package_version": "0.2.2",
        "source_file_sha256": source_hashes,
        "copied_file_sha256": copied_hashes,
        "patched_sha256": copied_hashes[str(STORAGE)],
        "dependency_policy": "shared realpath symlinks; preparer never writes dependencies; not OS-enforced read-only",
        "thread_scope": "requested native query/optimize pools only, not an observed process thread cap; first native initialization wins",
        "entrypoint": str(copied / metadata["bin"]["zg"]),
        "source_sha256": source_hashes[str(STORAGE)],
        "requested_native_threads": {"queryThreads": query_threads, "optimizeThreads": optimize_threads},
        "observed_total_threads": None,
    }
    (destination / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    return manifest


def validate_budget(value):
    """Native uint32 budgets are strictly positive integers, not booleans."""
    if type(value) is not int or not 1 <= value <= 0xFFFFFFFF:
        raise ValueError("thread budget must be a positive uint32 integer")
    return value


def main():
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', required=True, type=Path, help='installed @zvec/zvec-grep 0.2.2 package root (read-only)')
    parser.add_argument('--destination', type=Path, help='new private directory; default .test-tools/thread-runtimes/zg-qN-oN-HASH')
    parser.add_argument('--expected-sha256', required=True, help='independently captured SHA-256 of dist/engine/storage/zvec.js')
    parser.add_argument('--query-threads', type=int, default=1)
    parser.add_argument('--optimize-threads', type=int, default=1)
    args = parser.parse_args()
    try:
        validate_budget(args.query_threads)
        validate_budget(args.optimize_threads)
        destination = args.destination or (Path(__file__).resolve().parents[1] / '.test-tools/thread-runtimes' /
            f'zg-q{args.query_threads}-o{args.optimize_threads}-{args.expected_sha256[:12]}')
        manifest = prepare_runtime(args.source, destination, expected_sha256=args.expected_sha256,
                                   query_threads=args.query_threads, optimize_threads=args.optimize_threads)
    except (ValueError, OSError, subprocess.SubprocessError) as exc:
        parser.exit(2, f'preparation refused: {exc}\n')
    print(json.dumps(manifest, indent=2))


if __name__ == '__main__':
    main()
