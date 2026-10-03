# SOURCE-ONLY DRAFT: NOT_EXECUTED. Parent materializes/runs under original pt.sh shared lock/resource caps.
"""The engine runtime is laid out from verified local state, never guessed."""
import importlib.util
import json
import os
import shutil
import sys
from pathlib import Path
from source_support import PROVIDER_ROOT

import pytest

ROOT = Path(__file__).resolve().parents[1]
PLUGIN = PROVIDER_ROOT / "zvec-memory"
HOST = Path(os.environ.get("HERMES_AGENT_DIR", str(Path.home() / ".hermes/hermes-agent")))
sys.path.insert(0, str(HOST))
RESOLVE_EXECUTABLE = shutil.which


def load_engine():
    if "zvec_engine_pkg.engine" in sys.modules:
        return sys.modules["zvec_engine_pkg.engine"]
    spec = importlib.util.spec_from_file_location(
        "zvec_engine_pkg", PLUGIN / "__init__.py", submodule_search_locations=[str(PLUGIN)])
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["zvec_engine_pkg"] = module
    spec.loader.exec_module(module)
    return importlib.import_module("zvec_engine_pkg.engine")


def fake_runtime(tmp_path: Path, version="0.2.2", entry=True) -> Path:
    root = tmp_path / "runtime_root"
    package = root / "runtime/node_modules/@zvec/zvec-grep"
    (package / "dist/cli").mkdir(parents=True)
    (package / "package.json").write_text(json.dumps({"name": "@zvec/zvec-grep", "version": version}))
    if entry:
        (package / "dist/cli/index.js").write_text("// entry\n")
    return root


def config_for(tmp_path: Path, **overrides) -> dict:
    config = {"runtime_dir": str(tmp_path / "runtime_root"),
              "engine_home": str(tmp_path / "engine-home")}
    config.update(overrides)
    return config


@pytest.fixture()
def unit_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    return tmp_path / "xdg/systemd/user"
















def test_post_setup_preserves_user_settings_and_records_the_launcher(tmp_path, unit_dir, monkeypatch):
    engine = load_engine()
    fake_runtime(tmp_path)
    home = Path(os.environ["HERMES_HOME"])
    (home / "zvec-memory").mkdir(parents=True)
    (home / "zvec-memory/config.json").write_text(json.dumps(
        {**config_for(tmp_path), "embedding": "local/potion-retrieval-32m", "recall_limit": 9, "vault": "$HERMES_HOME/my-vault"}))
    saved = []
    monkeypatch.setattr(engine, "_save_host_config", lambda config: saved.append(config) or True)
    config = {"plugins": {"zvec-memory": {"runtime_dir": str(tmp_path / "runtime_root"),
                                          "engine_home": str(tmp_path / "engine-home")}},
              "memory": {}}

    result = engine.post_setup(home, config)
    stored = json.loads((home / "zvec-memory/config.json").read_text())
    assert stored["embedding"] == "local/potion-retrieval-32m" and stored["recall_limit"] == 9
    assert stored["vault"] == "$HERMES_HOME/my-vault"
    assert stored["zg_bin"] == result["zg_bin"] == str(tmp_path / "runtime_root/zg-default")
    assert config["memory"] == {}
    assert saved == [home]

    stamps = (home / "zvec-memory/config.json").stat().st_mtime_ns
    engine.post_setup(home, config)
    assert (home / "zvec-memory/config.json").stat().st_mtime_ns == stamps














def fixture_node(tmp_path, *, version="v22.0.0", rc=0, native_version="0.2.2", native_rc=0):
    """Harmless executable; neither Node nor the native engine is launched."""
    import shlex

    node = tmp_path / "version-manager/bin/node"
    node.parent.mkdir(parents=True, exist_ok=True)
    node.write_text(
        "#!/bin/sh\n"
        f'if [ "$1" = "--version" ]; then printf "%s\\n" {shlex.quote(version)}; exit {rc}; fi\n'
        f'if [ "$2" = "--version" ]; then printf "%s\\n" {shlex.quote(native_version)}; exit {native_rc}; fi\n'
        "exit 97\n")
    node.chmod(0o700)
    return node


@pytest.fixture(autouse=True)
def default_node(tmp_path, monkeypatch):
    """Existing managed tests also use a fixture interpreter, never live Node."""
    import shutil

    node = fixture_node(tmp_path / "default-interpreter")
    real_which = shutil.which
    monkeypatch.setattr(shutil, "which", lambda name, *a, **k:
                        str(node) if name == "node" else real_which(name, *a, **k))
    return node
















