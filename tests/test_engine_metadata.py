"""Metadata admission must fail before generated artifacts or npm writes."""
import json

import pytest

from test_engine import config_for, fake_runtime, load_engine
from test_engine_arguments import tree_state


BAD_METADATA = [
    None, [], 1, "not-an-object", {},
    {"version": "0.2.2"},
    {"name": "different-package", "version": "0.2.2"},
    {"name": "@zvec/zvec-grep"},
    {"name": "@zvec/zvec-grep", "version": None},
    {"name": "@zvec/zvec-grep", "version": True},
    {"name": "@zvec/zvec-grep", "version": 2},
    {"name": "@zvec/zvec-grep", "version": ["0.2.2"]},
    {"name": "@zvec/zvec-grep", "version": ""},
    {"name": "@zvec/zvec-grep", "version": "  "},
    {"name": "@zvec/zvec-grep", "version": " 0.2.2"},
]


@pytest.mark.parametrize("metadata", BAD_METADATA)
def test_invalid_metadata_cannot_generate_runtime_artifacts(
        tmp_path, isolated_home, monkeypatch, metadata):
    engine = load_engine()
    fake_runtime(tmp_path)
    config = config_for(tmp_path)
    path = engine.package_json_path(isolated_home, config)
    path.write_text(json.dumps(metadata))
    monkeypatch.setattr(engine.subprocess, "run", lambda *a, **k: pytest.fail("invalid metadata must not execute"))
    before = tree_state(tmp_path)

    result = engine.ensure_engine(isolated_home, config)

    assert result["status"] == "invalid-engine-metadata"
    assert engine.installed_version(isolated_home, config) is None
    assert tree_state(tmp_path) == before


def unexpected_install_runner(calls):
    from types import SimpleNamespace

    def run(*args, **kwargs):
        calls.append((args, kwargs))
        return SimpleNamespace(returncode=1, stdout="", stderr="unexpected npm attempt")
    return run


@pytest.mark.parametrize("metadata", BAD_METADATA)
def test_invalid_existing_package_is_not_overwritten_by_install(
        tmp_path, isolated_home, monkeypatch, metadata):
    engine = load_engine()
    fake_runtime(tmp_path)
    config = config_for(tmp_path)
    engine.package_json_path(isolated_home, config).write_text(json.dumps(metadata))
    calls = []
    monkeypatch.setattr(engine.shutil, "which", lambda name: "/not-executed/npm")
    monkeypatch.setattr(engine.subprocess, "run", unexpected_install_runner(calls))
    before = tree_state(tmp_path)

    result = engine.install_engine(isolated_home, config)

    assert result["status"] == "invalid-engine-metadata"
    assert calls == []
    assert tree_state(tmp_path) == before


@pytest.mark.parametrize("installed", ["0.2.1", "0.2.3"])
@pytest.mark.parametrize("entry", [True, False])
def test_different_installed_version_requires_explicit_migration(
        tmp_path, isolated_home, monkeypatch, installed, entry):
    engine = load_engine()
    fake_runtime(tmp_path, version=installed, entry=entry)
    config = config_for(tmp_path)
    calls = []
    monkeypatch.setattr(engine.shutil, "which", lambda name: "/not-executed/npm")
    monkeypatch.setattr(engine.subprocess, "run", unexpected_install_runner(calls))
    before = tree_state(tmp_path)

    result = engine.install_engine(isolated_home, config)

    assert result["status"] == "migration-required"
    assert result["found"] == installed and result["expected"] == engine.PINNED_VERSION
    assert calls == []
    assert tree_state(tmp_path) == before


@pytest.mark.parametrize("installed", ["0.2.1", "0.2.3"])
def test_ensure_engine_requires_the_expected_version(tmp_path, isolated_home, installed):
    engine = load_engine()
    fake_runtime(tmp_path, version=installed)
    config = config_for(tmp_path)
    before = tree_state(tmp_path)

    result = engine.ensure_engine(isolated_home, config)

    assert result["status"] == "version-mismatch"
    assert result["found"] == installed and result["expected"] == engine.PINNED_VERSION
    assert tree_state(tmp_path) == before


def test_fresh_explicit_install_honours_requested_version(tmp_path, isolated_home, monkeypatch):
    from types import SimpleNamespace

    engine = load_engine()
    config = config_for(tmp_path)
    calls = []

    def install(argv, **kwargs):
        calls.append(argv)
        assert argv[-1] == f"{engine.ENGINE_PACKAGE}@0.2.3"
        fake_runtime(tmp_path, version="0.2.3")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(engine.shutil, "which", lambda name: "/not-executed/npm")
    monkeypatch.setattr(engine.subprocess, "run", install)

    result = engine.install_engine(isolated_home, config, version="0.2.3")

    assert len(calls) == 1
    assert result["status"] == "updated" and result["version"] == "0.2.3"
    manifest = json.loads((engine.runtime_root(isolated_home, config) / engine.MANIFEST_NAME).read_text())
    assert manifest["version"] == "0.2.3"


@pytest.mark.parametrize("operation", ["ensure_engine", "install_engine"])
@pytest.mark.parametrize("condition", ["malformed", "invalid-utf8", "missing", "directory"])
def test_unreadable_metadata_preserved_before_admission(
        tmp_path, isolated_home, monkeypatch, operation, condition):
    engine = load_engine()
    fake_runtime(tmp_path)
    config = config_for(tmp_path)
    path = engine.package_json_path(isolated_home, config)
    if condition == "malformed":
        path.write_bytes(b'{"name":')
    elif condition == "invalid-utf8":
        path.write_bytes(b"\xff")
    else:
        path.unlink()
        if condition == "directory":
            path.mkdir()
    calls = []
    monkeypatch.setattr(engine.shutil, "which", lambda name: "/not-executed/npm")
    monkeypatch.setattr(engine.subprocess, "run", unexpected_install_runner(calls))
    before = tree_state(tmp_path)

    result = getattr(engine, operation)(isolated_home, config)

    assert result["status"] == "invalid-engine-metadata"
    assert calls == [] and tree_state(tmp_path) == before


@pytest.mark.parametrize("metadata", BAD_METADATA + [{"name": "@zvec/zvec-grep", "version": "0.2.1"}])
def test_post_fetch_metadata_is_revalidated_before_artifact_writes(
        tmp_path, isolated_home, monkeypatch, metadata):
    from types import SimpleNamespace

    engine = load_engine()
    config = config_for(tmp_path)
    calls = []

    def install(argv, **kwargs):
        calls.append(argv)
        assert argv[-1] == f"{engine.ENGINE_PACKAGE}@{engine.PINNED_VERSION}"
        fake_runtime(tmp_path)
        engine.package_json_path(isolated_home, config).write_text(json.dumps(metadata))
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(engine.shutil, "which", lambda name: "/not-executed/npm")
    monkeypatch.setattr(engine.subprocess, "run", install)

    result = engine.install_engine(isolated_home, config)

    assert len(calls) == 1 and result["status"] == "version-mismatch"
    assert not engine.launcher_path(isolated_home, config).exists()
    assert not engine.unit_path().exists()
    assert not (engine.runtime_root(isolated_home, config) / engine.MANIFEST_NAME).exists()
    assert json.loads(engine.package_json_path(isolated_home, config).read_text()) == metadata


@pytest.mark.parametrize("preexisting", [False, True])
def test_same_version_explicit_install_remains_supported(tmp_path, isolated_home, monkeypatch, preexisting):
    from types import SimpleNamespace

    engine = load_engine()
    config = config_for(tmp_path)
    if preexisting:
        fake_runtime(tmp_path)
    calls = []

    def install(argv, **kwargs):
        calls.append(argv)
        if not preexisting:
            fake_runtime(tmp_path)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(engine.shutil, "which", lambda name: "/not-executed/npm")
    monkeypatch.setattr(engine.subprocess, "run", install)

    result = engine.install_engine(isolated_home, config)

    assert len(calls) == 1 and result["status"] == "updated"
    assert result["version"] == engine.PINNED_VERSION


def test_valid_metadata_still_generates_artifacts(tmp_path, isolated_home):
    engine = load_engine()
    fake_runtime(tmp_path)

    result = engine.ensure_engine(isolated_home, config_for(tmp_path))

    assert result["status"] == "updated"
    assert result["version"] == engine.PINNED_VERSION
