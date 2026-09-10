"""Offline-only preparer tests; fixtures never load a native SDK."""
import importlib.util
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/prepare_zg_thread_runtime.py"


def load_module():
    if not SCRIPT.exists():
        raise AssertionError("standalone runtime preparer is missing")
    spec = importlib.util.spec_from_file_location("thread_runtime", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class BudgetTests(unittest.TestCase):
    def test_positive_uint32_only(self):
        module = load_module()
        for value in [True, False, 0, -1, 1.0, float("inf"), float("nan"), 2**32, "1", None]:
            with self.subTest(value=value), self.assertRaises(ValueError):
                module.validate_budget(value)
        for value in [1, 2, 2**32 - 1]:
            self.assertEqual(module.validate_budget(value), value)


class CopyTests(unittest.TestCase):
    def setUp(self):
        import tempfile
        import json
        import hashlib
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / "installed/node_modules/@zvec/zvec-grep"
        self.source.mkdir(parents=True)
        self.storage = self.source / "dist/engine/storage/zvec.js"
        self.storage.parent.mkdir(parents=True)
        self.original = ('let zvecInitialized = false;\n'
                         'function initializeZvec() {\n'
                         '    if (zvecInitialized) {\n'
                         '        return;\n'
                         '    }\n'
                         '    ZVecInitialize({ logLevel: ZVecLogLevel.WARN });\n'
                         '    zvecInitialized = true;\n'
                         '}\n')
        self.storage.write_text(self.original)
        (self.source / "package.json").write_text(json.dumps({
            "name": "@zvec/zvec-grep", "version": "0.2.2", "type": "module",
            "bin": {"zg": "dist/cli/index.js"}, "dependencies": {"@zvec/zvec": "0.7.1"}}))
        cli = self.source / "dist/cli/index.js"
        cli.parent.mkdir()
        cli.write_text('console.log(JSON.stringify(process.argv.slice(2)));\n')
        (self.source / "README.md").write_text("offline fixture package\n")
        self.sdk = self.source.parent / "zvec"
        self.sdk.mkdir()
        (self.sdk / "package.json").write_text(json.dumps({
            "name": "@zvec/zvec", "version": "0.7.1", "main": "index.js"}))
        (self.sdk / "index.js").write_text('throw Error("MUST NOT IMPORT SDK");\n')
        import subprocess
        target = subprocess.check_output(["node", "-p", "process.platform+'-'+process.arch"], text=True).strip()
        binding = self.sdk / "node_modules" / ("@zvec/bindings-" + target)
        binding.mkdir(parents=True, exist_ok=True)
        (binding / "package.json").write_text(json.dumps({"name": "@zvec/bindings-" + target, "version": "0.7.1", "main": "fixture.node"}))
        (binding / "fixture.node").write_text("OFFLINE FIXTURE - NOT A NATIVE BINARY")
        self.expected = hashlib.sha256(self.storage.read_bytes()).hexdigest()
        self.dest = self.root / "private/runtime"
        self.module = load_module()

    def prepare(self, **kwargs):
        return self.module.prepare_runtime(self.source, self.dest,
                                           expected_sha256=self.expected, **kwargs)

    def test_dependency_resolution_preserves_nested_native_binding_without_import(self):
        import json
        import subprocess
        platform = subprocess.check_output(["node", "-p", "process.platform+'-'+process.arch"], text=True).strip()
        binding_name = "@zvec/bindings-" + platform
        binding = self.sdk / "node_modules" / binding_name
        binding.mkdir(parents=True, exist_ok=True)
        (binding / "package.json").write_text(json.dumps({"name": binding_name, "version": "0.7.1", "main": "fixture.node"}))
        (binding / "fixture.node").write_text("OFFLINE FIXTURE - NOT A NATIVE BINARY")
        sdk_meta = json.loads((self.sdk / "package.json").read_text())
        sdk_meta["optionalDependencies"] = {binding_name: "0.7.1"}
        (self.sdk / "package.json").write_text(json.dumps(sdk_meta))
        manifest = self.prepare()
        self.assertIn("dependencies", manifest, "explicit verified resolution is missing")
        self.assertEqual(manifest["dependencies"]["@zvec/zvec"]["path"], str(self.sdk))
        self.assertEqual(manifest["native_binding"]["entrypoint"], str(binding / "fixture.node"))
        self.assertEqual((self.dest / "package/node_modules/@zvec/zvec").resolve(), self.sdk)
        self.assertEqual(manifest["requested_native_threads"], {"queryThreads": 1, "optimizeThreads": 1})
        self.assertIn("dist/cli/index.js", manifest["copied_file_sha256"])
        self.assertEqual(manifest["patched_sha256"], manifest["copied_file_sha256"]["dist/engine/storage/zvec.js"])

    def test_rejects_sdk_version_or_missing_dependency(self):
        import json
        meta_path = self.sdk / "package.json"
        meta = json.loads(meta_path.read_text())
        meta["version"] = "0.8.0"
        meta_path.write_text(json.dumps(meta))
        with self.assertRaisesRegex(ValueError, "0.7.1"):
            self.prepare()
        self.assertFalse(self.dest.exists())
        meta_path.unlink()
        with self.assertRaises(ValueError):
            self.prepare()
        self.assertFalse(self.dest.exists())

    def test_rejects_missing_literal_import_without_loading_packages(self):
        cli = self.source / 'dist/cli/index.js'
        cli.write_text('import "@zvec/zvec/missing-file.js";\n')
        with self.assertRaisesRegex(ValueError, 'resolution'):
            self.prepare()
        self.assertFalse(self.dest.exists())

    def test_copy_tampering_is_rejected_before_patch(self):
        import os
        import shutil
        from unittest.mock import patch
        copytree = shutil.copytree
        for mode in ['hardlink', 'symlink', 'bytes']:
            with self.subTest(mode=mode):
                def tamper(source, destination, **kwargs):
                    with patch.object(self.module.shutil, 'copytree', copytree):
                        result = copytree(source, destination, **kwargs)
                    target = Path(destination) / 'dist/engine/storage/zvec.js'
                    target.unlink()
                    if mode == 'hardlink':
                        os.link(self.storage, target)
                    elif mode == 'symlink':
                        target.symlink_to(self.storage)
                    else:
                        target.write_text('unexpected copied bytes')
                    return result
                with patch.object(self.module.shutil, 'copytree', side_effect=tamper):
                    with self.assertRaises(ValueError):
                        self.prepare()
                self.assertEqual(self.storage.read_text(), self.original)
                self.assertFalse((self.dest / 'manifest.json').exists())
                shutil.rmtree(self.dest)

    def test_final_source_mutation_is_rejected_without_manifest(self):
        from unittest.mock import patch
        resolve = self.module.node_metadata
        def mutate_after_resolution(package):
            result = resolve(package)
            if package == self.dest / 'package':
                (self.source / 'README.md').write_text('changed after copy')
            return result
        with patch.object(self.module, 'node_metadata', side_effect=mutate_after_resolution):
            with self.assertRaisesRegex(ValueError, 'source.*changed'):
                self.prepare()
        self.assertTrue((self.dest / 'package').is_dir(), 'retain failed private copy')
        self.assertFalse((self.dest / 'manifest.json').exists())

    def test_final_copy_bytes_and_file_set_must_match_exact_patch(self):
        from unittest.mock import patch
        resolve = self.module.node_metadata
        before = self.module.file_hashes(self.source)
        for mode in ['storage', 'readme', 'extra', 'missing', 'unpatched']:
            with self.subTest(mode=mode):
                self.dest = self.root / 'private' / mode
                def mutate_after_resolution(package):
                    result = resolve(package)
                    if package == self.dest / 'package':
                        if mode == 'missing':
                            (package / 'README.md').unlink()
                        elif mode == 'extra':
                            (package / 'unexpected.txt').write_text('extra')
                        elif mode == 'readme':
                            (package / 'README.md').write_text('tampered')
                        elif mode == 'unpatched':
                            (package / self.module.STORAGE).write_text(self.original)
                        else:
                            with (package / self.module.STORAGE).open('a') as target:
                                target.write('// injected after valid initializer patch\n')
                    return result
                with patch.object(self.module, 'node_metadata', side_effect=mutate_after_resolution):
                    with self.assertRaisesRegex(ValueError, 'copy.*changed|patch'):
                        self.prepare()
                self.assertFalse((self.dest / 'manifest.json').exists())
                self.assertTrue((self.dest / 'package').is_dir())
                self.assertEqual(self.module.file_hashes(self.source), before)

    def test_final_inventory_rejects_links_without_following_them(self):
        import os
        from unittest.mock import patch
        resolve = self.module.node_metadata
        outside = self.root / 'outside.txt'
        outside.write_text('offline fixture package\n')
        for tree in ['source', 'copy']:
            for mode in ['file-symlink', 'dangling', 'directory-symlink', 'hardlink']:
                if tree == 'source' and mode == 'hardlink':
                    continue  # Source files may already be hardlinked; copies must not be.
                with self.subTest(tree=tree, mode=mode):
                    self.dest = self.root / 'private' / (tree + '-' + mode)
                    package = self.source if tree == 'source' else self.dest / 'package'
                    target = package / ('README.md' if mode in ['file-symlink', 'hardlink'] else 'unexpected')
                    def mutate_after_resolution(path):
                        result = resolve(path)
                        if path == self.dest / 'package':
                            if mode in ['file-symlink', 'hardlink']:
                                target.unlink()
                            if mode == 'hardlink':
                                os.link(outside, target)
                            else:
                                referent = outside if mode == 'file-symlink' else (
                                    self.root / 'absent' if mode == 'dangling' else self.sdk)
                                target.symlink_to(referent, target_is_directory=mode == 'directory-symlink')
                        return result
                    try:
                        with patch.object(self.module, 'node_metadata', side_effect=mutate_after_resolution):
                            with self.assertRaisesRegex(ValueError, 'symlink|shared inode'):
                                self.prepare()
                        self.assertFalse((self.dest / 'manifest.json').exists())
                        self.assertEqual(outside.read_text(), 'offline fixture package\n')
                    finally:
                        if tree == 'source':
                            target.unlink(missing_ok=True)
                            if target.name == 'README.md':
                                target.write_text('offline fixture package\n')

    def test_source_baseline_precedes_resolution_and_stays_pinned(self):
        from unittest.mock import patch
        resolve = self.module.node_metadata
        for relative in [self.module.STORAGE, Path('README.md')]:
            with self.subTest(relative=relative):
                self.dest = self.root / 'private' / relative.stem
                target = self.source / relative
                original = target.read_bytes()
                def mutate_after_resolution(package):
                    result = resolve(package)
                    if package == self.source:
                        target.write_bytes(original + b'// changed during resolution\n')
                    return result
                try:
                    with patch.object(self.module, 'node_metadata', side_effect=mutate_after_resolution):
                        with self.assertRaisesRegex(ValueError, 'source.*changed'):
                            self.prepare()
                    self.assertFalse(self.dest.exists(), 'reject before private writes')
                finally:
                    target.write_bytes(original)

    def test_dependency_exclusion_is_exact_not_all_node_modules(self):
        from unittest.mock import patch
        resolve = self.module.node_metadata
        for mode in ['extra-file', 'missing-link', 'redirected-link', 'regular-directory', 'special-file']:
            with self.subTest(mode=mode):
                self.dest = self.root / 'private' / mode
                def mutate_after_resolution(package):
                    result = resolve(package)
                    if package == self.dest / 'package':
                        link = package / 'node_modules/@zvec/zvec'
                        if mode == 'extra-file':
                            (package / 'node_modules/unexpected.txt').write_text('must be inventoried')
                        elif mode == 'special-file':
                            import os
                            os.mkfifo(package / 'unexpected.fifo')
                        else:
                            link.unlink()
                            if mode == 'redirected-link':
                                link.symlink_to(self.source, target_is_directory=True)
                            elif mode == 'regular-directory':
                                link.mkdir()
                    return result
                with patch.object(self.module, 'node_metadata', side_effect=mutate_after_resolution):
                    with self.assertRaises(ValueError):
                        self.prepare()
                self.assertFalse((self.dest / 'manifest.json').exists())
                self.assertTrue((self.sdk / 'index.js').exists())

    def test_import_keywords_in_strings_are_not_dependencies(self):
        (self.source / 'dist/cli/index.js').write_text('const words = ["from",\n "import",\n "export"];\n')
        manifest = self.prepare()
        self.assertEqual(manifest['imports'], {})

    def test_missing_platform_binding_is_rejected(self):
        import shutil
        shutil.rmtree(self.sdk / 'node_modules')
        with self.assertRaisesRegex(ValueError, 'binding'):
            self.prepare()
        self.assertFalse(self.dest.exists())

    def test_cli_requires_explicit_pin_and_rejects_bad_budget(self):
        import subprocess
        import json
        base = ['python3', str(SCRIPT), '--source', str(self.source), '--destination', str(self.dest)]
        missing = subprocess.run(base, capture_output=True, text=True)
        self.assertNotEqual(missing.returncode, 0, 'CLI must require an explicit source hash')
        invalid = subprocess.run(base + ['--expected-sha256', self.expected, '--query-threads', '1.5'], capture_output=True, text=True)
        self.assertNotEqual(invalid.returncode, 0)
        self.assertFalse(self.dest.exists())
        valid = subprocess.run(base + ['--expected-sha256', self.expected], capture_output=True, text=True, check=True)
        self.assertEqual(json.loads(valid.stdout)['requested_native_threads'], {'queryThreads': 1, 'optimizeThreads': 1})

    def test_rejects_untrusted_source_before_creating_destination(self):
        import hashlib
        import json
        for case in ["hash", "version", "name", "guard", "duplicate", "symlink", "entrypoint"]:
            with self.subTest(case=case):
                original_meta = (self.source / "package.json").read_bytes()
                original_hash = self.expected
                if case == "hash":
                    self.expected = "0" * 64
                elif case in {"version", "name", "entrypoint"}:
                    meta = json.loads(original_meta)
                    if case == "entrypoint":
                        meta["bin"]["zg"] = "../../outside.js"
                    else:
                        meta[case] = "unexpected"
                    (self.source / "package.json").write_text(json.dumps(meta))
                elif case == "symlink":
                    (self.source / "linked").symlink_to(self.storage)
                else:
                    text = self.original.replace("if (zvecInitialized)", "if (false)") if case == "guard" else self.original * 2
                    self.storage.write_text(text)
                    self.expected = hashlib.sha256(self.storage.read_bytes()).hexdigest()
                try:
                    with self.assertRaises(ValueError):
                        self.prepare()
                    self.assertFalse(self.dest.exists())
                finally:
                    (self.source / "package.json").write_bytes(original_meta)
                    (self.source / "linked").unlink(missing_ok=True)
                    self.storage.write_text(self.original)
                    self.expected = original_hash

    def test_rejects_symlink_destination_ancestors_and_existing_hardlinks(self):
        import os
        self.dest.parent.mkdir()
        outside = self.root / "outside"
        outside.mkdir()
        self.dest.symlink_to(outside, target_is_directory=True)
        with self.assertRaises(ValueError):
            self.prepare()
        self.dest.unlink()
        self.dest.parent.rmdir()
        self.dest.parent.symlink_to(outside, target_is_directory=True)
        with self.assertRaises(ValueError):
            self.prepare()
        self.dest.parent.unlink()
        self.dest.mkdir(parents=True)
        os.link(self.storage, self.dest / "hardlink")
        with self.assertRaises(ValueError):
            self.prepare()
        self.assertEqual(self.storage.read_text(), self.original)
        self.assertEqual(list(outside.iterdir()), [])

    def test_physical_overlap_is_rejected_before_any_write_or_node(self):
        from unittest.mock import patch
        alias = self.root / 'installed-alias'
        alias.symlink_to(self.root / 'installed', target_is_directory=True)
        sources = [
            self.source,
            self.source.parent / '..' / '@zvec/zvec-grep',
            alias / 'node_modules/@zvec/zvec-grep',
        ]
        destinations = [root / suffix for root in sources for suffix in ['.', 'review-example', '..']]
        for source in sources:
            for destination in destinations:
                with self.subTest(source=source, destination=destination):
                    with patch.object(Path, 'mkdir', side_effect=AssertionError('must not write')):
                        with patch.object(self.module, 'node_metadata', side_effect=AssertionError('must reject before Node')):
                            with self.assertRaises(ValueError):
                                self.module.prepare_runtime(source, destination, expected_sha256=self.expected)
        self.assertFalse((self.source / 'review-example').exists())
        self.assertEqual(self.storage.read_text(), self.original)

    def test_invalid_budgets_do_not_touch_paths_or_start_node(self):
        from unittest.mock import patch
        with patch("subprocess.run", side_effect=AssertionError("node must not start")):
            for value in [True, 0, 1.0, float("nan"), 2**32]:
                for key in ["query_threads", "optimize_threads"]:
                    with self.subTest(value=value, key=key), self.assertRaises(ValueError):
                        self.prepare(**{key: value})
        self.assertFalse(self.dest.exists())

    def test_complete_independent_copy_and_normal_entrypoint(self):
        import json
        import subprocess
        self.assertTrue(callable(getattr(self.module, "prepare_runtime", None)),
                        "private package copy preparer is missing")
        manifest = self.prepare(query_threads=2, optimize_threads=3)
        copied = self.dest / "package"
        changed = []
        for source in self.source.rglob("*"):
            if source.is_file():
                target = copied / source.relative_to(self.source)
                self.assertTrue(target.is_file())
                self.assertFalse(target.is_symlink())
                self.assertNotEqual(source.stat().st_ino, target.stat().st_ino)
                if source.read_bytes() != target.read_bytes():
                    changed.append(str(source.relative_to(self.source)))
        self.assertEqual(changed, ["dist/engine/storage/zvec.js"])
        patched = copied / "dist/engine/storage/zvec.js"
        self.assertEqual(patched.read_text(), self.original.replace(
            "logLevel: ZVecLogLevel.WARN", "logLevel: ZVecLogLevel.WARN, queryThreads: 2, optimizeThreads: 3"))
        self.assertEqual(manifest["requested_native_threads"], {"queryThreads": 2, "optimizeThreads": 3})
        self.assertIsNone(manifest["observed_total_threads"])
        self.assertEqual(json.loads((self.dest / "manifest.json").read_text()), manifest)
        args = ["--liftoff-only", "spaces remain intact", "--query", "semi;$(no)"]
        result = subprocess.run(["node", manifest["entrypoint"], *args], capture_output=True, text=True, check=True)
        self.assertEqual(json.loads(result.stdout), args)
        patched.write_text("private mutation")
        self.assertEqual(self.storage.read_text(), self.original)
        self.assertEqual(manifest["source_sha256"], self.expected)


if __name__ == "__main__":
    unittest.main()
