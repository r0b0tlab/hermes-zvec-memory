# SOURCE-ONLY DRAFT: NOT_EXECUTED. Parent materializes/runs under original pt.sh shared lock/resource caps.
"""Unknown settings cannot establish a no-override rebuild destination.

Read denial is a scoped synthetic PermissionError at the real settings seam,
not kernel EACCES under the isolated namespace root. Parsing faults are real.
"""
import json
from pathlib import Path

import pytest

from test_cli import load_cli
from test_cli_health_finish import dispatch






VAULT_VALUES = [pytest.param("__missing__", id="missing"),
    pytest.param(None, id="null"), pytest.param(False, id="false"),
    pytest.param(0, id="zero"), pytest.param("", id="empty"),
    pytest.param("  ", id="blank"), pytest.param([], id="empty-list"),
    pytest.param({}, id="empty-object"), pytest.param(True, id="true"),
    pytest.param(1, id="one"), pytest.param(-1, id="negative"),
    pytest.param([1], id="list"), pytest.param({"x": 1}, id="object"),
    pytest.param("relative/vault", id="relative"),
    pytest.param("$HERMES_HOME/vault", id="dollar-home"),
    pytest.param("${HERMES_HOME}/vault", id="braced-home"),
    pytest.param("~/vault", id="tilde"), pytest.param("__absolute__", id="absolute")]












@pytest.mark.parametrize("raw", [None, False, 0], ids=["null", "false", "zero"])
@pytest.mark.parametrize("store", ["native", "legacy"])
def test_cold_provider_canonical_home_and_backup_share_falsey_default(
        tmp_path, monkeypatch, raw, store):
    import yaml
    from test_provider import _mod

    home = tmp_path / "cold-home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    config = {"vault":raw, "unknown":[None, False, 0]}
    if store == "native":
        source = home / "zvec-memory/config.json"
        source.parent.mkdir()
        source.write_text(json.dumps(config))
    else:
        source = home / "config.yaml"
        source.write_text(yaml.safe_dump({"plugins":{"zvec-memory":config}}))
    before = source.read_bytes()
    provider = _mod.ZvecMemoryProvider()
    default = (home / "zvec-memory").resolve()
    assert provider._resolve_vault(None) == default
    with pytest.raises(NotImplementedError, match="core.*backup"):
        provider.backup_paths()
    assert type(provider._config["vault"]) is type(raw) and provider._config == config
    assert provider._vault is None and provider._disk_worker is None and provider._index_worker is None
    assert source.read_bytes() == before and not (home / "None").exists()
    assert not (home / "False").exists() and not (home / "0").exists()
    assert not (default / ".reindex-request.json").exists()





L7_TRUTHY_DIAGNOSTIC_VALUES = [
    pytest.param(True, id="bool-true"),
    pytest.param(1, id="int-positive"),
    pytest.param(-1, id="int-negative"),
    pytest.param(1.5, id="float-positive"),
    pytest.param(["opaque-l7-fixture-content"], id="list-nonempty"),
    pytest.param({"opaque": "opaque-l7-fixture-content"}, id="object-nonempty"),
]


