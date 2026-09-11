"""The engine runtime is laid out from verified local state, never guessed."""
import importlib.util
import json
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PLUGIN = ROOT / "zvec-memory"
HOST = Path(os.environ.get("HERMES_AGENT_DIR", str(Path.home() / ".hermes/hermes-agent")))
sys.path.insert(0, str(HOST))


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


def test_launcher_script_is_byte_identical_to_the_verified_launcher(tmp_path):
    engine = load_engine()
    home = tmp_path / "hermes-home"
    config = config_for(tmp_path)
    script = engine.launcher_script(home, config)
    assert script == (
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n"
        "# Dedicated, authenticated local engine for the default Hermes profile.\n"
        f'export ZVEC_GREP_HOME="{tmp_path / "engine-home"}"\n'
        f'export ZVEC_GREP_MODEL_CACHE="{engine.model_cache(config)}/models"\n'
        'export ZVEC_GREP_MODE="auto"\n'
        'export ZVEC_GREP_SERVER_URL="http://127.0.0.1:17999/mcp"\n'
        'export ZVEC_GREP_SERVER_TOKEN_FILE="$ZVEC_GREP_HOME/server.token"\n'
        "unset ZVEC_GREP_SERVER_TOKEN ZVEC_GREP_API_KEY ZVEC_GREP_ENDPOINT DASHSCOPE_API_KEY QWEN_API_KEY\n"
        f'exec /usr/bin/node {tmp_path / "runtime_root/runtime/node_modules/@zvec/zvec-grep/dist/cli/index.js"} "$@"\n')
    assert script.endswith('"$@"\n'), "the launcher must forward its arguments"


def test_default_runtime_root_honours_the_environment_override(tmp_path, monkeypatch):
    engine = load_engine()
    monkeypatch.setenv("HERMES_ZVEC_RUNTIME_DIR", str(tmp_path / "elsewhere"))
    assert engine.runtime_root(tmp_path / "hermes-home", {}) == tmp_path / "elsewhere"
    assert engine.launcher_path(tmp_path / "hermes-home", {}).parent == tmp_path / "elsewhere"


@pytest.mark.skipif(not Path.home().joinpath(".local/share/hermes-zvec-memory/zg-default").is_file(),
                    reason="no hand-built engine runtime on this machine")
def test_generator_reproduces_the_installed_launcher(tmp_path, monkeypatch):
    """The generated launcher must match the verified production file exactly."""
    engine = load_engine()
    monkeypatch.delenv("HERMES_ZVEC_RUNTIME_DIR", raising=False)
    monkeypatch.delenv("HERMES_ZVEC_MODEL_CACHE", raising=False)
    home = Path.home() / ".hermes"
    installed = Path.home() / ".local/share/hermes-zvec-memory/zg-default"
    assert engine.launcher_script(home, {}) == installed.read_text(encoding="utf-8")


def test_unit_template_declares_the_task_ceiling_and_the_listen_address(tmp_path):
    engine = load_engine()
    config = config_for(tmp_path, server_url="http://127.0.0.1:18099/mcp")
    unit = engine.unit_template(tmp_path / "hermes-home", config)
    assert f"TasksMax={engine.TASKS_MAX}\n" in unit
    assert f"ExecStart={tmp_path}/runtime_root/zg-default server run --listen 127.0.0.1:18099" in unit
    assert f"--token-file {tmp_path}/engine-home/server.token" in unit
    assert "WantedBy=default.target\n" in unit


def test_ensure_engine_lays_out_and_is_idempotent(tmp_path, unit_dir):
    engine = load_engine()
    fake_runtime(tmp_path)
    home = tmp_path / "hermes-home"
    config = config_for(tmp_path)

    first = engine.ensure_engine(home, config)
    assert first["status"] == "updated" and first["version"] == "0.2.2"
    assert set(first["writes"]) == {"launcher", "unit", "manifest"}
    launcher = Path(first["zg_bin"])
    assert launcher.read_text() == engine.launcher_script(home, config)
    assert oct(launcher.stat().st_mode & 0o777) == "0o700"
    manifest = json.loads((Path(first["runtime_root"]) / "manifest.json").read_text())
    assert manifest["tasks_max"] == engine.TASKS_MAX and manifest["version"] == "0.2.2"

    stamps = {path: path.stat().st_mtime_ns for path in
              (launcher, Path(first["unit"]), Path(first["runtime_root"]) / "manifest.json")}
    second = engine.ensure_engine(home, config)
    assert second["status"] == "present" and second["writes"] == []
    assert {path: path.stat().st_mtime_ns for path in stamps} == stamps


def test_ensure_engine_fails_closed_without_a_verified_runtime(tmp_path, unit_dir):
    engine = load_engine()
    home = tmp_path / "hermes-home"
    config = config_for(tmp_path)
    result = engine.ensure_engine(home, config)
    assert result["status"] == "missing-engine"
    assert "engine install" in result["detail"]
    assert not (tmp_path / "runtime_root/zg-default").exists()
    assert not (unit_dir / engine.UNIT_NAME).exists()


def test_ensure_engine_never_clobbers_a_hand_edited_unit(tmp_path, unit_dir):
    engine = load_engine()
    fake_runtime(tmp_path)
    home = tmp_path / "hermes-home"
    config = config_for(tmp_path)
    unit_dir.mkdir(parents=True)
    mine = "[Service]\nExecStart=/usr/bin/true\n"
    (unit_dir / engine.UNIT_NAME).write_text(mine)

    result = engine.ensure_engine(home, config)
    assert result["unit_status"] == "conflict"
    assert (unit_dir / engine.UNIT_NAME).read_text() == mine
    assert (unit_dir / f"{engine.UNIT_NAME}.new").read_text() == engine.unit_template(home, config)


def test_post_setup_preserves_user_settings_and_records_the_launcher(tmp_path, unit_dir, monkeypatch):
    engine = load_engine()
    fake_runtime(tmp_path)
    home = tmp_path / "hermes-home"
    (home / "zvec-memory").mkdir(parents=True)
    (home / "zvec-memory/config.json").write_text(json.dumps(
        {"embedding": "local/custom", "recall_limit": 9, "vault": "$HERMES_HOME/my-vault"}))
    saved = []
    monkeypatch.setattr(engine, "_save_host_config", lambda config: saved.append(config) or True)
    config = {"plugins": {"zvec-memory": {"runtime_dir": str(tmp_path / "runtime_root"),
                                          "engine_home": str(tmp_path / "engine-home")}},
              "memory": {}}

    result = engine.post_setup(home, config)
    stored = json.loads((home / "zvec-memory/config.json").read_text())
    assert stored["embedding"] == "local/custom" and stored["recall_limit"] == 9
    assert stored["vault"] == "$HERMES_HOME/my-vault"
    assert stored["zg_bin"] == result["zg_bin"] == str(tmp_path / "runtime_root/zg-default")
    assert config["memory"]["provider"] == "zvec-memory"
    assert saved == [config]

    stamps = (home / "zvec-memory/config.json").stat().st_mtime_ns
    engine.post_setup(home, config)
    assert (home / "zvec-memory/config.json").stat().st_mtime_ns == stamps


def test_post_setup_activates_without_an_engine_and_ignores_a_bare_block(tmp_path, unit_dir, monkeypatch):
    engine = load_engine()
    home = tmp_path / "hermes-home"
    monkeypatch.setattr(engine, "_save_host_config", lambda config: pytest.fail("must not save"))
    result = engine.post_setup(home, {"runtime_dir": str(tmp_path / "runtime_root")})
    assert result["status"] == "missing-engine"
    assert json.loads((home / "zvec-memory/config.json").read_text())["vault"] == "$HERMES_HOME/zvec-memory"
    assert result["provider"] == "zvec-memory"


def test_post_setup_activating_a_host_config_marks_ownership(tmp_path, unit_dir, monkeypatch):
    from test_provider import _mod

    importlib.import_module("zvec_memory_provider.engine")
    host_engine = sys.modules["zvec_memory_provider.engine"]
    fake_runtime(tmp_path)
    home = tmp_path / "hermes-home"
    saved = []
    monkeypatch.setattr(host_engine, "_save_host_config", lambda config: saved.append(config) or True)
    config = {"plugins": {"zvec-memory": {"runtime_dir": str(tmp_path / "runtime_root"),
                                          "engine_home": str(tmp_path / "engine-home")}},
              "memory": {}}
    result = _mod.post_setup(home, config)
    assert result["owns_config"] is True and result["provider"] == "zvec-memory"
    assert config["memory"]["provider"] == "zvec-memory"
    assert config["plugins"]["zvec-memory"]["vault"] == "$HERMES_HOME/zvec-memory"
    assert saved == [config]


def test_provider_instance_exposes_post_setup_for_the_host_hook(tmp_path, unit_dir, monkeypatch):
    """`_post_setup_hook` looks the hook up on the instance and then stops."""
    from test_provider import make_provider

    importlib.import_module("zvec_memory_provider.engine")
    monkeypatch.setattr(sys.modules["zvec_memory_provider.engine"], "_save_host_config",
                        lambda config: True)
    fake_runtime(tmp_path)
    provider = make_provider(tmp_path)
    try:
        assert callable(getattr(provider, "post_setup"))
        config = {"plugins": {"zvec-memory": {"runtime_dir": str(tmp_path / "runtime_root"),
                                              "engine_home": str(tmp_path / "engine-home")}},
                  "memory": {}}
        result = provider.post_setup(str(tmp_path / "hermes-home"), config)
        assert result["provider"] == "zvec-memory" and result["owns_config"] is True
        assert config["memory"]["provider"] == "zvec-memory"
        assert Path(result["zg_bin"]).is_file()
    finally:
        provider.shutdown()


def test_provider_package_exports_post_setup_for_a_bare_block(tmp_path, unit_dir, monkeypatch):
    from test_provider import _mod

    importlib.import_module("zvec_memory_provider.engine")
    host_engine = sys.modules["zvec_memory_provider.engine"]
    monkeypatch.setattr(host_engine, "_save_host_config", lambda config: pytest.fail("must not save"))
    result = _mod.post_setup(str(tmp_path / "hermes-home"),
                             {"runtime_dir": str(tmp_path / "runtime_root")})
    assert result["status"] == "missing-engine" and result["provider"] == "zvec-memory"
    assert result["owns_config"] is False
