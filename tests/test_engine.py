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
    home = tmp_path / "hermes-home"
    config = config_for(tmp_path)

    first = engine.ensure_engine(home, config)
    assert first["status"] == "updated" and first["version"] == "0.2.2"
    assert set(first["writes"]) == {"launcher", "unit", "manifest"}
    launcher = Path(first["zg_bin"])
    assert launcher.read_text() == engine.launcher_script(home, config, node=default_node)
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


def test_install_engine_resolves_npm_from_path(tmp_path, monkeypatch, default_node):
    engine = load_engine()
    real_run = engine.subprocess.run

    def fake_which(name):
        if name == "node":
            return str(default_node)
        if name == "npm":
            return "/opt/node/bin/npm"
        return name if name == "/opt/node/bin/npm" else None

    monkeypatch.setattr(engine.shutil, "which", fake_which)
    seen = []

    def npm_failure(argv, **kwargs):
        if argv == [str(default_node), "--version"]:
            return real_run(argv, **kwargs)
        assert argv[:2] == [str(default_node), "/opt/node/bin/npm"]
        seen.append({"argv": argv, "env": kwargs.get("env")})
        return engine.subprocess.CompletedProcess(argv, 1, "", "registry unreachable")

    monkeypatch.setattr(engine.subprocess, "run", npm_failure)
    result = engine.install_engine(tmp_path / "hermes-home", config_for(tmp_path))
    assert result["status"] == "install-failed"
    argv, environment = seen[0]["argv"], seen[0]["env"]
    assert argv[:2] == [str(default_node), "/opt/node/bin/npm"], \
        "PATH npm is run through the verified interpreter, not the hardcoded default"
    assert f"{engine.ENGINE_PACKAGE}@{engine.PINNED_VERSION}" in argv
    assert str(tmp_path / "runtime_root/runtime") in argv
    assert environment["SHARP_IGNORE_GLOBAL_LIBVIPS"] == "1", \
        "sharp must not attempt a source build against a global libvips"


@pytest.mark.parametrize("operation", ["install_engine", "ensure_engine"])
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
    if operation == "ensure_engine":
        fake_runtime(tmp_path)
    before = {p: p.read_bytes() for p in engine.runtime_root(home, config).rglob("*") if p.is_file()}
    with pytest.raises(ValueError, match="Node|node_bin"):
        getattr(engine, operation)(home, config)
    assert {p: p.read_bytes() for p in before} == before
    if operation == "install_engine":
        assert not engine.runtime_root(home, config).exists()
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


@pytest.mark.parametrize("version,rc", [
    ("v21.9.0", 0), ("v20.0.0", 0), ("22.0.0", 0), ("v22.0", 0),
    ("v22.0.0 extra", 0), ("", 0), ("v22.0.0", 1),
])
def test_node_version_refused_before_npm_or_writes(tmp_path, monkeypatch, version, rc):
    engine = load_engine()
    node = fixture_node(tmp_path, version=version, rc=rc)
    config = config_for(tmp_path, node_bin=str(node))
    real_run = engine.subprocess.run
    seen = []

    def only_version(argv, **kwargs):
        assert argv == [str(node), "--version"], "npm must never be invoked"
        seen.append((argv, kwargs))
        return real_run(argv, **kwargs)

    monkeypatch.setattr(engine.subprocess, "run", only_version)
    with pytest.raises(ValueError, match="Node.js >=22"):
        engine.install_engine(Path(os.environ["HERMES_HOME"]), config)
    assert len(seen) == 1
    assert seen[0][1]["stdin"] == engine.subprocess.DEVNULL
    assert seen[0][1]["timeout"] == 10
    assert not engine.runtime_root(Path(os.environ["HERMES_HOME"]), config).exists()
    assert not engine.unit_path().exists()


@pytest.mark.parametrize("requested", ["0.2.2", "0.2.3"])
@pytest.mark.parametrize("selection", ["path", "explicit"])
def test_node_path_selected_for_install_readiness_and_launcher(tmp_path, unit_dir, monkeypatch, requested, selection):
    """A version-manager symlink resolves once; later PATH cannot switch Node."""
    import shlex
    import subprocess

    engine = load_engine()
    selected = fixture_node(tmp_path / "node path with spaces ' literal", native_version=requested,
                            version="v22.0.0" if requested == "0.2.2" else "v24.1.0")
    manager = tmp_path / "manager/bin"
    manager.mkdir(parents=True)
    (manager / "node").symlink_to(selected)
    npm = manager / "npm"
    npm.write_text("// npm boundary fixture: never execute\n")
    npm.chmod(0o700)
    # Use the real PATH resolver, rather than the legacy tests' default double.
    monkeypatch.setattr(engine.shutil, "which", RESOLVE_EXECUTABLE)
    monkeypatch.setenv("PATH", f"{manager}:/usr/bin:/bin")
    config = config_for(tmp_path)
    if selection == "explicit":
        config["node_bin"] = str(manager / "node")
    original_config = dict(config)
    home = tmp_path / "user-home/.hermes"
    real_run = subprocess.run
    seen = []

    def fixture_install(argv, **kwargs):
        seen.append((argv, kwargs))
        if "install" in argv:
            assert argv[:2] == [str(selected), str(npm)]
            assert kwargs["env"]["PATH"].split(os.pathsep)[0] == str(selected.parent)
            assert kwargs["env"]["SHARP_IGNORE_GLOBAL_LIBVIPS"] == "1"
            assert argv[2:] == ["install", "--prefix", str(tmp_path / "runtime_root/runtime"),
                                "--no-audit", "--no-fund", "--save-exact",
                                f"@zvec/zvec-grep@{requested}"]
            fake_runtime(tmp_path, version=requested)
            monkeypatch.setattr(engine.shutil, "which", lambda name: None)
            return subprocess.CompletedProcess(argv, 0, "", "")
        assert argv[0] == str(selected), "all readiness calls use the admitted absolute Node"
        return real_run(argv, **kwargs)

    monkeypatch.setattr(subprocess, "run", fixture_install)
    result = engine.install_engine(home, config, version=requested)
    assert result["status"] == "updated"
    assert config == original_config, "the installer must not mutate caller-owned settings"
    manifest = json.loads((tmp_path / "runtime_root/manifest.json").read_text())
    assert manifest["node"] == str(selected)
    assert manifest["version"] == requested
    launcher = Path(result["zg_bin"])
    assert f'exec {shlex.quote(str(selected))} ' in launcher.read_text()
    assert [str(selected), str(engine.entry_path(home, config)), "--version"] in [a for a, _ in seen]
    # A different PATH Node must not affect the already generated launcher.
    decoy = tmp_path / "decoy"
    decoy.mkdir()
    (decoy / "node").write_text("#!/bin/sh\nexit 98\n")
    (decoy / "node").chmod(0o700)
    proc = real_run([str(launcher), "--version"], capture_output=True, text=True,
                    env={**os.environ, "PATH": f"{decoy}:/usr/bin:/bin"}, timeout=10)
    assert (proc.returncode, proc.stdout.strip()) == (0, requested)


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


def test_node_with_invalid_executable_format_refuses_before_npm(tmp_path, monkeypatch):
    engine = load_engine()
    node = tmp_path / "broken-node"
    node.write_text("#!/no/such/fixture-interpreter\n")
    node.chmod(0o700)
    with pytest.raises(ValueError, match="Node.js >=22 verification failed"):
        engine.install_engine(Path(os.environ["HERMES_HOME"]), config_for(tmp_path, node_bin=str(node)))
    assert not (tmp_path / "runtime_root").exists()
    assert not engine.unit_path().exists()


@pytest.mark.parametrize("version", ["latest", "^0.2.2", "~0.2.2", ">=0.2.2", "0.2", " 0.2.2", "0.2.2 ", "", None, True])
def test_requested_package_version_must_be_exact_before_npm(tmp_path, monkeypatch, version):
    engine = load_engine()
    monkeypatch.setattr(engine.subprocess, "run", lambda *a, **k: pytest.fail("no command before exact package admission"))
    with pytest.raises(ValueError, match="exact.*version"):
        engine.install_engine(Path(os.environ["HERMES_HOME"]), config_for(tmp_path), version=version)
    assert not (tmp_path / "runtime_root").exists()
    assert not engine.unit_path().exists()


def test_install_engine_without_npm_says_so(tmp_path, monkeypatch, default_node):
    engine = load_engine()
    monkeypatch.setattr(engine.shutil, "which", lambda name: None)
    result = engine.install_engine(tmp_path / "hermes-home", config_for(tmp_path, node_bin=str(default_node)))
    assert result["status"] == "no-npm" and "Node.js" in result["detail"]
