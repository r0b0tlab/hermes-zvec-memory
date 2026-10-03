"""Reduced core contract: unsupported entry points refuse before side effects."""
import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import stat

import pytest

from test_provider import ZvecMemoryProvider, _mod
from test_cli import load_cli
from test_engine import load_engine
from source_support import PROVIDER_ROOT


def _tree_state(root):
    """Compare all pre-existing fixture paths, modes and content without following links."""
    result: dict[str, tuple[str, int, str | None]] = {".": ("directory", root.lstat().st_mode, None)}
    for path in sorted(root.rglob("*")):
        mode = path.lstat().st_mode
        if stat.S_ISLNK(mode):
            kind, content = "symlink", os.readlink(path)
        elif stat.S_ISREG(mode):
            kind, content = "file", hashlib.sha256(path.read_bytes()).hexdigest()
        elif stat.S_ISDIR(mode):
            kind, content = "directory", None
        else:
            raise AssertionError("Unexpected special file in test fixture")
        result[path.relative_to(root).as_posix()] = (kind, mode, content)
    return result


@pytest.mark.parametrize("embedding", ["local/other", "remote/model", "", None, False])
def test_core_rejects_model_switch_before_provider_state(tmp_path, embedding):
    vault = tmp_path / "not-created"
    with pytest.raises(ValueError, match="core.*embedding"):
        ZvecMemoryProvider(config={"vault": str(vault), "embedding": embedding})
    assert not vault.exists()


def test_core_fixed_default_remains_available_without_workers():
    p = ZvecMemoryProvider(config={})
    assert p.get_tool_schemas()
    assert p._disk_worker is None and p._index_worker is None


@pytest.mark.parametrize("command", ["migrate-config", "backup", "restore"])
def test_core_cli_unsupported_before_home_or_engine(command, monkeypatch, capsys):
    cli = load_cli()
    monkeypatch.setattr(cli, "_hermes_home", lambda: pytest.fail("No home resolution for unsupported action"))
    parser = argparse.ArgumentParser()
    cli.register_cli(parser)
    if command in {"backup", "restore"}:
        # These commands never existed. Argparse already refuses before dispatch;
        # do not invent operational entries to satisfy a JSON-output oracle.
        with pytest.raises(SystemExit) as refused:
            parser.parse_args([command, "--json"])
        assert refused.value.code == 2
        captured = capsys.readouterr()
        assert not captured.out
        assert "invalid choice" in captured.err
        return
    args = parser.parse_args([command, "--json"])
    assert cli.zvec_memory_command(args) == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload["action"] == {"name": command, "status": "unsupported"}
    assert payload["health"]["status"] == "not_checked"


def test_core_legacy_mirror_migration_refused_without_initialized_vault():
    p = ZvecMemoryProvider(config={})
    with pytest.raises(NotImplementedError, match="core.*migration"):
        p.migrate_legacy_mirrors([])
    assert p._vault is None


def test_core_backup_declaration_refuses_instead_of_advertising_snapshot():
    p = ZvecMemoryProvider(config={})
    with pytest.raises(NotImplementedError, match="core.*backup"):
        p.backup_paths()
    assert p._vault is None


def test_core_desktop_has_no_declared_or_legacy_config_surface():
    spec = importlib.util.spec_from_file_location("core_schema", PROVIDER_ROOT / "zvec-memory/config_schema.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.CONFIG_SCHEMA is None
    assert ZvecMemoryProvider(config={}).get_config_schema() == []


def test_core_generic_config_save_refused_before_file_publication(tmp_path):
    p = ZvecMemoryProvider(config={})
    with pytest.raises(NotImplementedError, match="core.*CLI setup"):
        p.save_config({"recall_limit": 9}, str(tmp_path))
    assert not (tmp_path / "zvec-memory").exists()


@pytest.mark.parametrize("method", ["ensure_engine", "install_engine", "post_setup"])
def test_core_setup_rejects_model_before_runtime_or_process(method, tmp_path, monkeypatch):
    engine = load_engine()
    monkeypatch.setattr(engine, "_require_managed_scope", lambda home: None)
    monkeypatch.setattr(engine.subprocess, "run", lambda *a, **k: pytest.fail("No process before config refusal"))
    with pytest.raises(ValueError, match="core.*embedding"):
        getattr(engine, method)(tmp_path, {"embedding": "local/other", "runtime_dir": str(tmp_path / "runtime")})
    assert not (tmp_path / "runtime").exists()


def test_core_engine_version_switch_refused_before_layout(tmp_path, monkeypatch):
    engine = load_engine()
    monkeypatch.setattr(engine, "_require_managed_scope", lambda home: None)
    before = _tree_state(tmp_path)
    with pytest.raises(ValueError, match="core.*engine"):
        engine.install_engine(tmp_path, {}, version="0.2.3")
    assert _tree_state(tmp_path) == before
