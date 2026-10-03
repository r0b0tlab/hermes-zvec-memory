#!/usr/bin/env python3
"""Emit source-bound reduced-core pytest argv, never run or relax isolation.

The complete historical suite remains available and its failures remain FAIL.
This explicit selection substitutes supported-model safety successors and only
individually defers entries outside the reduced contract. Output is JSON, not
shell text: callers must preserve every argument literally, including `${...}`.
Run the output inside the same isolated, resource-capped test lane. SDK/model
preparation belongs inside that lane; this helper neither installs nor warms it.
"""
from __future__ import annotations

import argparse
import ast
from collections import Counter
import hashlib
import json
from pathlib import Path, PurePosixPath
import re

ROOT = Path(__file__).resolve().parents[1]
EXPECTED = {'successor-required': 120, 'deferred': 57, 'asset-replay': 2}
CASE_MATRIX_SHA256 = '31721f02a67b3f80663e842a525d74085dd4190add3f4e53b8be9a11f6cd5275'


def _source(root: Path, relative: str) -> Path:
    if not isinstance(relative, str):
        raise ValueError('Source path must be a string')
    path = PurePosixPath(relative)
    if (path.is_absolute() or len(path.parts) != 2 or path.parts[0] != 'tests'
            or not path.name.startswith('test_') or path.suffix != '.py'
            or str(path) != relative):
        raise ValueError('Not a literal test-source path')
    selected = root / relative
    if selected.is_symlink() or not selected.is_file() or not selected.resolve().is_relative_to(root.resolve()):
        raise ValueError('Source binding is missing or outside the checkout')
    return selected


def _nodeid(root: Path, nodeid: str) -> tuple[Path, str]:
    if not isinstance(nodeid, str) or any(c in nodeid for c in '\n\r\0'):
        raise ValueError('Nodeid must be a literal single-line string')
    parts = nodeid.split('::')
    if len(parts) != 2:
        raise ValueError('Only an exact file/function nodeid is admitted')
    name = parts[1].split('[', 1)[0]
    if not re.fullmatch(r'test_[A-Za-z0-9_]+', name):
        raise ValueError('Not an exact test function')
    source = _source(root, parts[0])
    return source, name


def validate(root: Path, manifest: dict) -> dict:
    if manifest.get('schema_version') != 1 or manifest.get('scope') != 'reduced-core-v0.3.0':
        raise ValueError('Unrecognized scope manifest')
    if manifest.get('expected_disposition_counts') != EXPECTED:
        raise ValueError('Scope obligations cannot be silently reduced')
    cases = manifest.get('cases')
    if not isinstance(cases, list) or len(cases) != 179:
        raise ValueError('The original 179 obligations must all be present')
    counts, seen, hashes, functions = Counter(), set(), {}, {}
    for case in cases:
        identifier = case['original_nodeid']
        if identifier in seen:
            raise ValueError('Duplicate original obligation')
        seen.add(identifier)
        path, name = _nodeid(root, identifier)
        binding = case['source_binding']
        if binding['file'] != identifier.split('::')[0] or binding['function'] != name:
            raise ValueError('Original obligation/source binding mismatch')
        if path not in hashes:
            raw = path.read_bytes()
            hashes[path] = hashlib.sha256(raw).hexdigest()
            functions[path] = {node.name for node in ast.walk(ast.parse(raw))
                               if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))}
        if hashes[path] != binding['sha256'] or name not in functions[path]:
            raise ValueError('Original source binding mismatch')
        disposition = case['disposition']
        if disposition not in EXPECTED:
            raise ValueError('Unknown disposition; no implicit exclusion')
        counts[disposition] += 1
        mappings = case['successor_mapping']
        if not isinstance(mappings, list) or not all(isinstance(x, str) for x in mappings):
            raise ValueError('Successor mappings must be literal nodeids')
        if disposition == 'successor-required' and not mappings:
            raise ValueError('Mandatory safety/recovery successor is missing')
        if disposition == 'asset-replay' and mappings != [identifier]:
            raise ValueError('Prerequisite replay must retain the unchanged original case')
        for successor in mappings:
            successor_path, successor_name = _nodeid(root, successor)
            if successor_path not in functions:
                functions[successor_path] = {node.name for node in ast.walk(ast.parse(successor_path.read_bytes()))
                                            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))}
            if successor_name not in functions[successor_path]:
                raise ValueError('Mapped successor is absent')
            if disposition == 'successor-required' and not successor_path.name.startswith('test_core_'):
                raise ValueError('Core successor must be explicitly named')
    if dict(counts) != EXPECTED:
        raise ValueError('Incomplete original obligation dispositions')
    canonical = json.dumps(sorted(cases, key=lambda row: row['original_nodeid']),
                           sort_keys=True, separators=(',', ':')).encode()
    if hashlib.sha256(canonical).hexdigest() != CASE_MATRIX_SHA256:
        raise ValueError('Original literal IDs or scope mappings drifted')
    return dict(counts)


def pytest_arguments(root: Path, manifest: dict) -> list[str]:
    validate(root, manifest)
    args = ['tests', '-m', 'not integration']
    for case in manifest['cases']:
        if case['disposition'] != 'asset-replay':
            args.extend(['--deselect', case['original_nodeid']])
    return args


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args(argv)
    manifest = json.loads((ROOT / 'tests/reduced_core_scope.json').read_text())
    print(json.dumps(pytest_arguments(ROOT, manifest)))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
