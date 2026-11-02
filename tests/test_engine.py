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


def test_launcher_script_matches_the_current_generator_contract(tmp_path):
    engine = load_engine()
    home = tmp_path / "hermes-home"
    config = config_for(tmp_path)
    script = engine.launcher_script(home, config)
    assert script == (
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n"
        "# Dedicated, authenticated local engine for the default Hermes profile.\n"
        f'export ZVEC_GREP_HOME={tmp_path / "engine-home"}\n'
        f'export ZVEC_GREP_MODEL_CACHE={engine.model_cache(config)}/models\n'
        'export ZVEC_GREP_MODE="auto"\n'
        'export ZVEC_GREP_SERVER_URL=http://127.0.0.1:17999/mcp\n'
        'export ZVEC_GREP_SERVER_TOKEN_FILE="$ZVEC_GREP_HOME/server.token"\n'
        "unset ZVEC_GREP_SERVER_TOKEN ZVEC_GREP_API_KEY ZVEC_GREP_ENDPOINT DASHSCOPE_API_KEY QWEN_API_KEY\n"
        'if [[ "${1:-}" == "server" ]]; then\n'
        '  case "${2:-}" in\n'
        '    run) '
        f'exec /usr/bin/node {tmp_path / "runtime_root/managed-server.mjs"} '
        f'{tmp_path / "runtime_root/runtime/node_modules/@zvec/zvec-grep/dist/cli/index.js"} "$@" ;;\n'
        '    status|off) ;;\n'
        '    *) printf "%s\\n" "Managed daemon requires foreground server run via its service" >&2; exit 2 ;;\n'
        '  esac\n'
        'fi\n'
        f'exec /usr/bin/node {tmp_path / "runtime_root/runtime/node_modules/@zvec/zvec-grep/dist/cli/index.js"} "$@"\n')
    assert script.endswith('"$@"\n'), "the launcher must forward its arguments"


def test_default_runtime_root_honours_the_environment_override(tmp_path, monkeypatch):
    engine = load_engine()
    monkeypatch.setenv("HERMES_ZVEC_RUNTIME_DIR", str(tmp_path / "elsewhere"))
    assert engine.runtime_root(tmp_path / "hermes-home", {}) == tmp_path / "elsewhere"
    assert engine.launcher_path(tmp_path / "hermes-home", {}).parent == tmp_path / "elsewhere"




def test_unit_template_declares_the_task_ceiling_and_the_listen_address(tmp_path):
    engine = load_engine()
    config = config_for(tmp_path, server_url="http://127.0.0.1:18099/mcp")
    unit = engine.unit_template(tmp_path / "hermes-home", config)
    assert f"TasksMax={engine.TASKS_MAX}\n" in unit
    assert f'ExecStart="{tmp_path}/runtime_root/zg-default" server run --listen "127.0.0.1:18099"' in unit
    assert f'--token-file "{tmp_path}/engine-home/server.token"' in unit
    assert "WantedBy=default.target\n" in unit


def test_ensure_engine_lays_out_and_is_idempotent(tmp_path, unit_dir, default_node):
    engine = load_engine()
    fake_runtime(tmp_path)
    home = Path(os.environ["HERMES_HOME"])
    config = config_for(tmp_path)

    first = engine.ensure_engine(home, config)
    assert first["status"] == "updated" and first["version"] == "0.2.2"
    assert set(first["writes"]) == {"launcher", "unit", "manifest", "managed-server.mjs", "managed-policy.mjs", "managed-server-LICENSE.txt"}
    launcher = Path(first["zg_bin"])
    assert launcher.read_text() == engine.launcher_script(home, config, node=default_node)
    assert oct(launcher.stat().st_mode & 0o777) == "0o700"
    manifest = json.loads((Path(first["runtime_root"]) / "manifest.json").read_text())
    assert manifest["tasks_max"] == engine.TASKS_MAX and manifest["version"] == "0.2.2"

    artifacts = engine._artifact_set(home, config, node=default_node, version="0.2.2")
    for _, path, text, mode in artifacts:
        assert path.read_bytes() == text.encode("utf-8")
        assert path.stat().st_mode & 0o7777 == mode
    stamps = {path: path.stat().st_mtime_ns for _, path, _, _ in artifacts}
    second = engine.ensure_engine(home, config)
    assert second["status"] == "present" and second["writes"] == []
    assert {path: path.stat().st_mtime_ns for path in stamps} == stamps


def test_ensure_engine_fails_closed_without_a_verified_runtime(tmp_path, unit_dir):
    engine = load_engine()
    home = Path(os.environ["HERMES_HOME"])
    config = config_for(tmp_path)
    result = engine.ensure_engine(home, config)
    assert result["status"] == "missing-engine"
    detail = result["detail"].lower()
    assert "fresh" in detail and "installation" in detail
    assert "unavailable" in detail or "disabled" in detail
    assert "separately trusted" in detail
    assert not (tmp_path / "runtime_root/zg-default").exists()
    assert not (unit_dir / engine.UNIT_NAME).exists()


def test_ensure_engine_never_clobbers_a_hand_edited_unit(tmp_path, unit_dir):
    engine = load_engine()
    fake_runtime(tmp_path)
    home = Path(os.environ["HERMES_HOME"])
    config = config_for(tmp_path)
    unit_dir.mkdir(parents=True)
    mine = "[Service]\nExecStart=/usr/bin/true\n"
    (unit_dir / engine.UNIT_NAME).write_text(mine)

    with pytest.raises(RuntimeError, match="artifact conflict"):
        engine.ensure_engine(home, config)
    assert (unit_dir / engine.UNIT_NAME).read_text() == mine
    assert not (unit_dir / f"{engine.UNIT_NAME}.new").exists()
    assert not engine.launcher_path(home, config).exists()
    assert not (engine.runtime_root(home, config) / engine.MANIFEST_NAME).exists()


def test_post_setup_missing_engine_refuses_a_bare_block(tmp_path, unit_dir, monkeypatch):
    engine = load_engine()
    home = Path(os.environ["HERMES_HOME"])
    monkeypatch.setattr(engine, "_save_host_config", lambda config: pytest.fail("must not save"))
    with pytest.raises(RuntimeError, match="missing-engine"):
        engine.post_setup(home, {"runtime_dir": str(tmp_path / "runtime_root")})
    assert not (home / "zvec-memory/config.json").exists()


def test_post_setup_activating_a_host_config_marks_ownership(tmp_path, unit_dir, monkeypatch):
    from test_provider import _mod

    importlib.import_module("zvec_memory_provider.engine")
    host_engine = sys.modules["zvec_memory_provider.engine"]
    fake_runtime(tmp_path)
    home = Path(os.environ["HERMES_HOME"])
    saved = []
    monkeypatch.setattr(host_engine, "_save_host_config", lambda config: saved.append(config) or True)
    config = {"plugins": {"zvec-memory": {"runtime_dir": str(tmp_path / "runtime_root"),
                                          "engine_home": str(tmp_path / "engine-home")}},
              "memory": {}}
    result = _mod.post_setup(home, config)
    assert result["owns_config"] is True and result["provider"] == "zvec-memory"
    assert config["memory"] == {}
    assert "vault" not in config["plugins"]["zvec-memory"]
    assert saved == [home]


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
        result = provider.post_setup(os.environ["HERMES_HOME"], config)
        assert result["provider"] == "zvec-memory" and result["owns_config"] is True
        assert config["memory"] == {}
        assert Path(result["zg_bin"]).is_file()
    finally:
        provider.shutdown()


def test_provider_package_exports_strict_post_setup_for_a_bare_block(tmp_path, unit_dir, monkeypatch):
    from test_provider import _mod

    importlib.import_module("zvec_memory_provider.engine")
    host_engine = sys.modules["zvec_memory_provider.engine"]
    monkeypatch.setattr(host_engine, "_save_host_config", lambda config: pytest.fail("must not save"))
    with pytest.raises(RuntimeError, match="missing-engine"):
        _mod.post_setup(os.environ["HERMES_HOME"],
                        {"runtime_dir": str(tmp_path / "runtime_root")})
    assert not (Path(os.environ["HERMES_HOME"]) / "zvec-memory/config.json").exists()


@pytest.mark.parametrize("operation", ["ensure_engine"])
@pytest.mark.parametrize("selection", ["missing", "relative", "absent", "directory", "non-executable"])
def test_node_admission_refuses_before_npm_or_writes(tmp_path, monkeypatch, selection, operation):
    engine = load_engine()
    config = config_for(tmp_path)
    candidate = tmp_path / "selected-node"
    if selection == "relative":
        config["node_bin"] = "relative/node"
    elif selection == "absent":
        config["node_bin"] = str(candidate)
    elif selection == "directory":
        candidate.mkdir()
        config["node_bin"] = str(candidate)
    elif selection == "non-executable":
        candidate.write_text("#!/bin/sh\nexit 0\n")
        candidate.chmod(0o600)
        config["node_bin"] = str(candidate)
    monkeypatch.setattr(engine.shutil, "which", lambda name: None)
    monkeypatch.setattr(engine.subprocess, "run", lambda *a, **k: pytest.fail("must not run npm"))
    home = Path(os.environ["HERMES_HOME"])
    fake_runtime(tmp_path)
    before = {p: p.read_bytes() for p in engine.runtime_root(home, config).rglob("*") if p.is_file()}
    with pytest.raises(ValueError, match="Node|node_bin"):
        getattr(engine, operation)(home, config)
    assert {p: p.read_bytes() for p in before} == before

    assert not engine.launcher_path(home, config).exists()
    assert not (engine.runtime_root(home, config) / engine.MANIFEST_NAME).exists()
    assert not engine.unit_path().exists()


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


@pytest.mark.parametrize("native_version,native_rc", [("0.2.1", 0), ("v0.2.2", 0), ("0.2.2 extra", 0), ("", 0), ("0.2.2", 1)])
def test_native_executable_version_must_match_metadata_before_artifacts(tmp_path, unit_dir, native_version, native_rc):
    engine = load_engine()
    fake_runtime(tmp_path)
    node = fixture_node(tmp_path, native_version=native_version, native_rc=native_rc)
    config = config_for(tmp_path, node_bin=str(node))
    home = Path(os.environ["HERMES_HOME"])
    before = {p: (p.read_bytes(), p.stat().st_mtime_ns, p.stat().st_mode)
              for p in engine.runtime_root(home, config).rglob("*") if p.is_file()}
    result = engine.ensure_engine(home, config)
    assert result["status"] == "executable-version-mismatch"
    assert result["expected"] == "0.2.2"
    assert result["found"] == native_version
    assert {p: (p.read_bytes(), p.stat().st_mtime_ns, p.stat().st_mode) for p in before} == before
    assert not engine.launcher_path(home, config).exists()
    assert not (engine.runtime_root(home, config) / engine.MANIFEST_NAME).exists()
    assert not (unit_dir / engine.UNIT_NAME).exists()


def test_matching_installed_version_verifies_without_npm(tmp_path, unit_dir, monkeypatch, default_node):
    import subprocess

    engine = load_engine()
    fake_runtime(tmp_path)
    home = Path(os.environ["HERMES_HOME"])
    config = config_for(tmp_path)
    real_run = subprocess.run
    seen = []

    def versions_only(argv, **kwargs):
        assert "install" not in argv, "an admitted existing pin must not be reinstalled"
        seen.append(argv)
        return real_run(argv, **kwargs)

    monkeypatch.setattr(subprocess, "run", versions_only)
    result = engine.install_engine(home, config)
    assert result["status"] == "updated"
    assert seen == [[str(default_node), "--version"],
                    [str(default_node), str(engine.entry_path(home, config)), "--version"]]
    paths = [engine.launcher_path(home, config), engine.unit_path(),
             engine.runtime_root(home, config) / engine.MANIFEST_NAME]
    before = {p: (p.read_bytes(), p.stat().st_mtime_ns, p.stat().st_mode) for p in paths}
    second = engine.install_engine(home, config)
    assert second["status"] == "present" and second["writes"] == []
    assert {p: (p.read_bytes(), p.stat().st_mtime_ns, p.stat().st_mode) for p in paths} == before


@pytest.mark.parametrize("boundary", ["node", "native"])
@pytest.mark.parametrize("failure", ["os-error", "timeout"])
def test_runtime_version_execution_failures_refuse_before_artifacts(
        tmp_path, unit_dir, monkeypatch, default_node, boundary, failure):
    engine = load_engine()
    fake_runtime(tmp_path)
    home = Path(os.environ["HERMES_HOME"])
    config = config_for(tmp_path)
    real_run = engine.subprocess.run

    def fail_boundary(argv, **kwargs):
        is_node = argv == [str(default_node), "--version"]
        if is_node == (boundary == "node"):
            assert kwargs["timeout"] == 10 and kwargs["stdin"] == engine.subprocess.DEVNULL
            if failure == "os-error":
                raise PermissionError("synthetic-private-detail")
            raise engine.subprocess.TimeoutExpired(argv, kwargs["timeout"])
        return real_run(argv, **kwargs)

    monkeypatch.setattr(engine.subprocess, "run", fail_boundary)
    if boundary == "node":
        with pytest.raises(ValueError, match="Node.js >=22 verification failed"):
            engine.ensure_engine(home, config)
    else:
        result = engine.ensure_engine(home, config)
        assert result["status"] == "runtime-verification-failed"
        assert "synthetic-private-detail" not in result["detail"]
    assert not engine.launcher_path(home, config).exists()
    assert not (engine.runtime_root(home, config) / engine.MANIFEST_NAME).exists()
    assert not engine.unit_path().exists()


@pytest.mark.parametrize("version", ["latest", "^0.2.2", "~0.2.2", ">=0.2.2", "0.2", " 0.2.2", "0.2.2 ", "", None, True])
def test_requested_package_version_must_be_exact_before_npm(tmp_path, monkeypatch, version):
    engine = load_engine()
    monkeypatch.setattr(engine.subprocess, "run", lambda *a, **k: pytest.fail("no command before exact package admission"))
    with pytest.raises(ValueError, match="exact.*version"):
        engine.install_engine(Path(os.environ["HERMES_HOME"]), config_for(tmp_path), version=version)
    assert not (tmp_path / "runtime_root").exists()
    assert not engine.unit_path().exists()
