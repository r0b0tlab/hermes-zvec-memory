# SOURCE-ONLY DRAFT: NOT_EXECUTED. Parent materializes/runs under original pt.sh shared lock/resource caps.
"""Strict managed setup: offline fixtures, real host dispatch and config API."""
import copy
import importlib
import json
from pathlib import Path

import pytest

from test_engine import config_for, default_node, fake_runtime, load_engine
from test_engine_arguments import tree_state
from test_host_contract import host as contract_host, load as load_host_provider


@pytest.fixture
def setup_host(contract_host, monkeypatch):
    """Reuse the real host fixture, selecting its synthetic default home.

    Host imports already occur under runner-isolated roots; move its copied
    plugin before discovery and use the host's actual context-local selector.
    No old fixture or profile-scoped provider behaviour is redefined.
    """
    from hermes_constants import set_hermes_home_override, reset_hermes_home_override
    home = contract_host.tmp / ".hermes"
    contract_host.home.rename(home)
    contract_host.home = home
    contract_host.plugin = home / "plugins/zvec-memory"
    monkeypatch.setenv("HOME", str(contract_host.tmp))
    monkeypatch.setenv("HERMES_HOME", str(home))
    token = set_hermes_home_override(home)
    try:
        yield contract_host
    finally:
        reset_hermes_home_override(token)


def host_setup_target(host, monkeypatch, default_node):
    provider = load_host_provider(host)
    engine = importlib.import_module(provider.__module__ + ".engine")
    config = {"memory": {"provider": "previous"}, "plugins": {
        "zvec-memory": config_for(host.tmp, node_bin=str(default_node))}}
    def versions(argv, **kwargs):
        assert argv in ([str(default_node), "--version"],
                        [str(default_node), str(engine.entry_path(host.home, config["plugins"]["zvec-memory"])), "--version"])
        return engine.subprocess.CompletedProcess(argv, 0,
                    "v22.0.0\n" if len(argv) == 2 else "0.2.2\n", "")
    monkeypatch.setattr(engine.subprocess, "run", versions)
    return provider, engine, config












@pytest.mark.parametrize("native", [None, {}, {"auto_extract": False, "recall_limit": 0, "unknown": {"keep": [1, False]}, "embedding": "local/potion-retrieval-32m"}])
def test_shared_settings_presence_and_lossless_legacy_setup(
        tmp_path, isolated_home, monkeypatch, default_node, native):
    engine = load_engine()
    root = fake_runtime(tmp_path)
    monkeypatch.setenv("HERMES_ZVEC_RUNTIME_DIR", str(root))
    # Isolate settings ownership from the host save contract tested separately.
    monkeypatch.setattr(engine, "_save_host_config", lambda home: True)
    legacy = {"auto_extract": False, "recall_limit": 0,
              "unknown": {"legacy": [0, False]}, "embedding": "local/potion-retrieval-32m"}
    config = {"memory": {}, "plugins": {"zvec-memory": legacy}}
    path = engine.provider_config_path(isolated_home)
    if native is not None:
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps(native))
    expected = dict(legacy if native is None else native)
    result = engine.post_setup(isolated_home, config)
    assert json.loads(path.read_text()) == {**expected, "zg_bin": result["zg_bin"]}
    assert result["recall_readiness"] == "not_checked"
































