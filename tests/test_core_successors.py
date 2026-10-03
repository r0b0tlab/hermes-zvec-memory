"""Source-only successor regressions. NOT_EXECUTED by the source author.

Parent must run new behavior cases against the unchanged baseline first, then
against the composed candidate, using the original guarded runner. Native
readiness and actual CLI lifecycle remain separate parent-owned acceptance.
"""
import argparse
import importlib
import json

import pytest

from test_cli import load_cli
from test_engine import load_engine
from test_host_contract import host, load, write_native
from test_provider import ZvecMemoryProvider
from test_core_contract import _tree_state


@pytest.mark.parametrize("method", ["ensure_engine", "install_engine", "post_setup"])
def test_core_supplied_host_plugin_model_refused_before_scope(method, tmp_path, monkeypatch):
    engine = load_engine()
    monkeypatch.setattr(engine, "_require_managed_scope",
                        lambda home: pytest.fail("Unsupported supplied model must fail before path resolution"))
    supplied = {"memory": {}, "plugins": {"zvec-memory": {"embedding": "local/other"}}}
    before = _tree_state(tmp_path)
    with pytest.raises(ValueError, match="core.*embedding"):
        getattr(engine, method)(tmp_path, supplied)
    assert _tree_state(tmp_path) == before


def test_core_post_setup_validates_native_model_before_artifacts(tmp_path, monkeypatch):
    engine = load_engine()
    monkeypatch.setattr(engine, "_require_managed_scope", lambda home: None)
    monkeypatch.setattr(engine, "_host_config_destination",
                        lambda home: pytest.fail("No activation path before config validation"))
    folder = tmp_path / "zvec-memory"
    folder.mkdir()
    settings = folder / "config.json"
    original = '{"embedding": "local/other"}'
    settings.write_text(original)
    before = _tree_state(tmp_path)
    with pytest.raises(ValueError, match="core.*embedding"):
        engine.post_setup(tmp_path, {"memory": {}})
    assert settings.read_text() == original
    assert _tree_state(tmp_path) == before


def test_core_ensure_engine_rejects_version_override_before_scope(tmp_path, monkeypatch):
    engine = load_engine()
    monkeypatch.setattr(engine, "_require_managed_scope", lambda home: pytest.fail("No scope resolution"))
    before = _tree_state(tmp_path)
    with pytest.raises(ValueError, match="core.*engine"):
        engine.ensure_engine(tmp_path, {}, expected_version="0.2.3")
    assert _tree_state(tmp_path) == before


def test_core_cli_reindex_rejects_unsupported_model_before_health_or_marker(tmp_path, monkeypatch, capsys):
    cli = load_cli()
    monkeypatch.setattr(cli, "_configured", lambda: (tmp_path, {"embedding": "local/other"}))
    monkeypatch.setattr(cli, "collect_checks", lambda *a: pytest.fail("No engine/health before model validation"))
    monkeypatch.setattr(cli, "_request_reindex", lambda *a: pytest.fail("No durable request before validation"))
    parser = argparse.ArgumentParser()
    cli.register_cli(parser)
    before = _tree_state(tmp_path)
    assert cli.zvec_memory_command(parser.parse_args(["reindex", "--json"])) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["action"]["status"] == "failed"
    assert payload["action"]["error"] == "ValueError"
    assert payload["action"]["completed"] is False
    assert _tree_state(tmp_path) == before


@pytest.mark.parametrize("explicit", [False, True])
def test_core_default_and_explicit_fixed_model_remain_cold(explicit):
    p = ZvecMemoryProvider(config={"embedding": "local/potion-retrieval-32m"} if explicit else {})
    assert p.get_tool_schemas()
    assert p._vault is None and p._disk_worker is None and p._index_worker is None


def test_core_real_host_cli_setup_hook_keeps_json_and_activation_writer(host, monkeypatch):
    # Real pinned host dispatcher/provider/engine/config writer; only native
    # verification/artifacts boundary is substituted in this offline unit.
    from hermes_cli.memory_setup import _post_setup_hook

    provider = load(host)
    engine = importlib.import_module(type(provider).__module__ + ".engine")
    native = write_native(host, {"embedding": "local/potion-retrieval-32m", "recall_limit": 9})
    launcher = str(host.tmp / "owned-runtime" / "zg-default")
    monkeypatch.setattr(engine, "_require_managed_scope", lambda home: None)
    monkeypatch.setattr(engine, "ensure_engine", lambda home, config: {
        "status": "present", "unit_status": "current", "zg_bin": launcher})
    monkeypatch.setattr(provider, "save_config", lambda *a: pytest.fail("CLI must not use generic desktop save"))
    assert _post_setup_hook(provider, {"memory": {}}) is True
    saved = json.loads(native.read_text())
    assert saved == {"embedding": "local/potion-retrieval-32m", "recall_limit": 9, "zg_bin": launcher}
    from hermes_cli.config import load_config
    assert load_config()["memory"]["provider"] == "zvec-memory"
    assert native.stat().st_mode & 0o777 == 0o600


def test_core_real_host_backup_collector_tolerates_explicit_unsupported_provider(host, monkeypatch):
    from hermes_cli.backup import _collect_memory_provider_external_paths
    monkeypatch.setattr(host.discovery, "_get_active_memory_provider", lambda: "zvec-memory")
    provider = load(host)
    monkeypatch.setattr(provider, "_resolve_vault", lambda *a: pytest.fail("No vault resolution for backup"))
    monkeypatch.setattr(host.discovery, "load_memory_provider", lambda *a: provider)
    assert _collect_memory_provider_external_paths() == []
    assert provider._vault is None and provider._disk_worker is None
