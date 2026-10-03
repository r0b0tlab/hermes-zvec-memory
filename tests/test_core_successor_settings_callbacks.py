# SOURCE-ONLY DRAFT: NOT_EXECUTED. Parent materializes/runs under original pt.sh shared lock/resource caps.
"""Supported CLI callback successors; NOT_EXECUTED, no native acceptance."""
import copy
import json
import importlib

import pytest

from test_engine_setup_finish import setup_host, host_setup_target, contract_host
from test_engine import default_node
from test_host_contract import load
from test_provider import ZvecMemoryProvider, make_provider


def _cli_target(setup_host, monkeypatch, default_node):
    provider, engine, config = host_setup_target(setup_host, monkeypatch, default_node)
    launcher = str(setup_host.tmp / "owned-runtime/zg-default")
    monkeypatch.setattr(engine, "ensure_engine", lambda home, block: {
        "status": "present", "unit_status": "current", "zg_bin": launcher})
    monkeypatch.setattr(provider, "save_config", lambda *a: pytest.fail("No generic desktop writer"))
    return provider, engine, config, launcher


def test_cli_setup_settings_roundtrip_preserves_native_authority(setup_host, monkeypatch, default_node):
    from hermes_cli.memory_setup import _post_setup_hook
    provider, engine, config, launcher = _cli_target(setup_host, monkeypatch, default_node)
    supplied = {"embedding": "local/potion-retrieval-32m", "recall_limit": 11,
                "preview": "full", "context_chars": 321, "unknown": [False, 0]}
    config["plugins"]["zvec-memory"] = supplied
    original = copy.deepcopy(config)
    assert _post_setup_hook(provider, config) is True
    path = engine.provider_config_path(setup_host.home)
    expected = {**supplied, "zg_bin": launcher}
    assert json.loads(path.read_text()) == expected
    assert path.stat().st_mode & 0o777 == 0o600
    fresh = load(setup_host)
    assert fresh._recall_limit() == 11 and fresh._config["preview"] == "full"
    assert fresh._config["context_chars"] == 321 and fresh._config["unknown"] == [False, 0]
    before = (path.read_bytes(), path.stat().st_mtime_ns)
    config["plugins"]["zvec-memory"] = {"recall_limit": 99}
    assert _post_setup_hook(provider, config) is True
    assert (path.read_bytes(), path.stat().st_mtime_ns) == before
    assert supplied == original["plugins"]["zvec-memory"]
    # Activation is the real pinned host writer, not the disabled generic save.
    from hermes_cli.config import load_config
    assert load_config()["memory"]["provider"] == "zvec-memory"


def test_explicit_empty_config_ignores_cli_written_ambient(setup_host, monkeypatch, default_node):
    from hermes_cli.memory_setup import _post_setup_hook
    provider, engine, config, launcher = _cli_target(setup_host, monkeypatch, default_node)
    config["plugins"]["zvec-memory"] = {"recall_limit": 11}
    assert _post_setup_hook(provider, config) is True
    assert load(setup_host)._recall_limit() == 11
    assert ZvecMemoryProvider(config={})._config == {}


@pytest.mark.parametrize("raw", [b'{broken', b'[]', b'null', b'\xff'])
def test_cli_setup_corrupt_native_authority_not_silently_replaced(setup_host, monkeypatch, default_node, raw):
    from hermes_cli.memory_setup import _post_setup_hook
    provider, engine, config, launcher = _cli_target(setup_host, monkeypatch, default_node)
    path = engine.provider_config_path(setup_host.home)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(raw)
    before = (path.read_bytes(), path.stat().st_mode, path.stat().st_mtime_ns)
    host_path = setup_host.home / "config.yaml"
    assert not host_path.exists()
    original = copy.deepcopy(config)
    with pytest.raises((ValueError, UnicodeError)):
        _post_setup_hook(provider, config)
    assert (path.read_bytes(), path.stat().st_mode, path.stat().st_mtime_ns) == before
    assert config == original and not host_path.exists()


def test_legacy_unowned_fact_survives_migration_refusal(tmp_path):
    p = make_provider(tmp_path)
    legacy = p._write_fact("legacy preference", "user_pref", "mirror")
    try:
        original = legacy.read_bytes()
        p._apply_mirror("remove", "user", "", {"old_text": "legacy preference"})
        assert legacy.exists() and legacy.read_bytes() == original
        with pytest.raises(NotImplementedError, match="core.*migration"):
            p.migrate_legacy_mirrors([{"path": str(legacy.relative_to(p._vault)),
                                     "target": "user", "content": "legacy preference"}])
        assert legacy.exists() and legacy.read_bytes() == original
    finally:
        p.shutdown()


def test_legacy_config_read_remains_cold_while_save_refuses(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    legacy = tmp_path / "config.yaml"
    text = "unrelated: keep\nplugins:\n  zvec-memory:\n    preview: full\n"
    legacy.write_text(text)
    p = ZvecMemoryProvider()
    assert p._config == {"preview": "full"}
    assert p._vault is None and p._disk_worker is None and p._index_worker is None
    with pytest.raises(NotImplementedError, match="core.*CLI"):
        p.save_config({"recall_limit": 9}, str(tmp_path))
    assert legacy.read_text() == text
    assert not (tmp_path / "zvec-memory/config.json").exists()
