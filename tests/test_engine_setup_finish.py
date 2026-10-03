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


@pytest.mark.parametrize("failure", ["missing", "metadata", "native", "unit"])
def test_real_host_hook_setup_failure_never_publishes_config_or_activation(
        setup_host, monkeypatch, default_node, failure):
    from hermes_cli.memory_setup import _post_setup_hook
    provider, engine, config = host_setup_target(setup_host, monkeypatch, default_node)
    if failure != "missing":
        fake_runtime(setup_host.tmp)
    if failure == "metadata":
        engine.package_json_path(setup_host.home, config["plugins"]["zvec-memory"]).write_text("{}")
    elif failure == "native":
        monkeypatch.setattr(engine.subprocess, "run", lambda argv, **kwargs:
                            engine.subprocess.CompletedProcess(argv, 0, "v22.0.0\n" if len(argv) == 2 else "0.2.1\n", ""))
    elif failure == "unit":
        engine.unit_path().parent.mkdir(parents=True, exist_ok=True)
        engine.unit_path().write_text("[Service]\nExecStart=/usr/bin/true\n")
    before_config = copy.deepcopy(config)
    before = tree_state(setup_host.tmp)
    with pytest.raises(RuntimeError):
        _post_setup_hook(provider, config)
    assert config == before_config
    assert tree_state(setup_host.tmp) == before
    assert not engine.provider_config_path(setup_host.home).exists()
    assert not (setup_host.home / "config.yaml").exists()


def test_ready_bare_provider_settings_never_write_provider_json_or_host(
        tmp_path, isolated_home, default_node, monkeypatch):
    engine = load_engine()
    fake_runtime(tmp_path)
    config = config_for(tmp_path)
    original = copy.deepcopy(config)
    result = engine.post_setup(isolated_home, config)
    assert result["status"] == "updated" and result["owns_config"] is False
    assert not engine.provider_config_path(isolated_home).exists()
    assert not (isolated_home / "config.yaml").exists()
    assert config == original
    assert result["recall_readiness"] == "not_checked"


@pytest.mark.parametrize("operation", ["ensure_engine", "install_engine", "post_setup"])
@pytest.mark.parametrize("scope", ["named", "custom"])
def test_managed_scope_refuses_before_any_subprocess_or_write(
        tmp_path, isolated_home, monkeypatch, operation, scope):
    engine = load_engine()
    home = isolated_home / "profiles/secondary" if scope == "named" else tmp_path / "custom-home"
    fake_runtime(tmp_path)
    monkeypatch.setattr(engine.subprocess, "run", lambda *a, **k: pytest.fail("scope must refuse before subprocess"))
    before = tree_state(tmp_path)
    with pytest.raises(ValueError, match="default"):
        getattr(engine, operation)(home, config_for(tmp_path))
    assert tree_state(tmp_path) == before


@pytest.mark.parametrize("operation", ["ensure_engine", "install_engine"])
@pytest.mark.parametrize("member", ["launcher", "unit", "manifest"])
@pytest.mark.parametrize("conflict", ["bytes", "mode", "symlink", "directory"])
def test_complete_artifact_conflict_has_no_member_side_effect(
        tmp_path, isolated_home, monkeypatch, default_node, operation, member, conflict):
    engine = load_engine()
    config = config_for(tmp_path)
    fake_runtime(tmp_path)
    first = engine.ensure_engine(isolated_home, config)
    assert first["status"] == "updated"
    paths = {"launcher": engine.launcher_path(isolated_home, config),
             "unit": engine.unit_path(), "manifest": engine.runtime_root(isolated_home, config) / "manifest.json"}
    path = paths[member]
    if conflict == "bytes":
        path.write_text("operator-owned artifact\n")
    elif conflict == "mode":
        path.chmod(0o644)
    else:
        path.unlink()
        if conflict == "directory":
            path.mkdir()
        else:
            target = tmp_path / "operator-target"
            target.write_text("operator-owned target\n")
            path.symlink_to(target)
    # The other members must be preflighted, not recreated early.
    if member != "launcher":
        paths["launcher"].unlink()
    before = tree_state(tmp_path)
    with pytest.raises(RuntimeError, match="artifact.*conflict|conflict.*artifact"):
        getattr(engine, operation)(isolated_home, config)
    assert tree_state(tmp_path) == before
    assert not engine.unit_path().with_name(engine.UNIT_NAME + ".new").exists()


@pytest.mark.parametrize("member", ["launcher", "unit", "manifest"])
def test_fresh_install_preflights_artifacts_before_fetch(
        tmp_path, isolated_home, monkeypatch, default_node, member):
    engine = load_engine()
    config = config_for(tmp_path)
    paths = {"launcher": engine.launcher_path(isolated_home, config),
             "unit": engine.unit_path(), "manifest": engine.runtime_root(isolated_home, config) / "manifest.json"}
    path = paths[member]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("operator-owned artifact\n")
    real_run = engine.subprocess.run
    def versions_only(argv, **kwargs):
        assert argv == [str(default_node), "--version"], "must not fetch on artifact conflict"
        return real_run(argv, **kwargs)
    monkeypatch.setattr(engine.subprocess, "run", versions_only)
    before = tree_state(tmp_path)
    with pytest.raises(RuntimeError, match="artifact.*conflict|conflict.*artifact"):
        engine.install_engine(isolated_home, config)
    assert tree_state(tmp_path) == before


@pytest.mark.parametrize("native", [None, {}, {"auto_extract": False, "recall_limit": 0, "unknown": {"keep": [1, False]}, "embedding": "local/native"}])
def test_shared_settings_presence_and_lossless_legacy_setup(
        tmp_path, isolated_home, monkeypatch, default_node, native):
    engine = load_engine()
    root = fake_runtime(tmp_path)
    monkeypatch.setenv("HERMES_ZVEC_RUNTIME_DIR", str(root))
    # Isolate settings ownership from the host save contract tested separately.
    monkeypatch.setattr(engine, "_save_host_config", lambda home: True)
    legacy = {"auto_extract": False, "recall_limit": 0,
              "unknown": {"legacy": [0, False]}, "embedding": "local/legacy"}
    config = {"memory": {}, "plugins": {"zvec-memory": legacy}}
    path = engine.provider_config_path(isolated_home)
    if native is not None:
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps(native))
    expected = dict(legacy if native is None else native)
    result = engine.post_setup(isolated_home, config)
    assert json.loads(path.read_text()) == {**expected, "zg_bin": result["zg_bin"]}
    assert result["recall_readiness"] == "not_checked"


@pytest.mark.parametrize("raw", [b'{"broken":', b'[]', b'null', b'1', b'"text"', b'\xff'])
def test_real_host_hook_preserves_invalid_present_config_before_effects(
        setup_host, monkeypatch, default_node, raw):
    from hermes_cli.memory_setup import _post_setup_hook
    provider, engine, config = host_setup_target(setup_host, monkeypatch, default_node)
    fake_runtime(setup_host.tmp)
    path = engine.provider_config_path(setup_host.home)
    path.parent.mkdir(parents=True)
    path.write_bytes(raw)
    monkeypatch.setattr(engine.subprocess, "run", lambda *a, **k: pytest.fail("invalid settings must fail cold"))
    before = tree_state(setup_host.tmp)
    original = copy.deepcopy(config)
    with pytest.raises((ValueError, UnicodeError)):
        _post_setup_hook(provider, config)
    assert tree_state(setup_host.tmp) == before
    assert config == original


@pytest.mark.parametrize("operation", ["ensure_engine", "install_engine", "post_setup"])
def test_custom_executable_refused_before_subprocess_or_writes(
        tmp_path, isolated_home, monkeypatch, operation):
    engine = load_engine()
    fake_runtime(tmp_path)
    config = config_for(tmp_path, zg_bin=str(tmp_path / "operator-zg"))
    monkeypatch.setattr(engine.subprocess, "run", lambda *a, **k: pytest.fail("custom executable must refuse before subprocess"))
    before = tree_state(tmp_path)
    with pytest.raises(RuntimeError, match="Custom zg_bin"):
        getattr(engine, operation)(isolated_home, config)
    assert tree_state(tmp_path) == before


def test_real_host_hook_respects_native_custom_executable(
        setup_host, monkeypatch, default_node):
    from hermes_cli.memory_setup import _post_setup_hook
    provider, engine, config = host_setup_target(setup_host, monkeypatch, default_node)
    fake_runtime(setup_host.tmp)
    path = engine.provider_config_path(setup_host.home)
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"zg_bin": str(setup_host.tmp / "custom-zg")}))
    monkeypatch.setattr(engine.subprocess, "run", lambda *a, **k: pytest.fail("custom native selection must remain cold"))
    original = copy.deepcopy(config)
    before = tree_state(setup_host.tmp)
    with pytest.raises(RuntimeError, match="Custom zg_bin"):
        _post_setup_hook(provider, config)
    assert config == original and tree_state(setup_host.tmp) == before


def test_real_host_hook_scoped_activation_preserves_current_config_and_caller(
        setup_host, monkeypatch, default_node):
    from hermes_cli.memory_setup import _post_setup_hook
    from hermes_cli.config import save_config
    provider, engine, stale = host_setup_target(setup_host, monkeypatch, default_node)
    fake_runtime(setup_host.tmp)
    current = {"model": "current-not-stale", "memory": {"provider": "previous", "unrelated": False},
               "plugins": {"other": {"unknown": 0}}, "web": {"port": 12345}}
    save_config(current)
    original = copy.deepcopy(stale)
    assert _post_setup_hook(provider, stale) is True
    assert stale == original
    raw = importlib.import_module(provider.__module__ + ".hostio").read_user_config_raw(setup_host.home / "config.yaml")
    assert raw == {**current, "memory": {**current["memory"], "provider": "zvec-memory"}}
    values = json.loads(engine.provider_config_path(setup_host.home).read_text())
    assert values == {**stale["plugins"]["zvec-memory"], "zg_bin": str(engine.launcher_path(setup_host.home, values))}
    paths = [engine.provider_config_path(setup_host.home), engine.launcher_path(setup_host.home, values),
             engine.unit_path(), engine.runtime_root(setup_host.home, values) / "manifest.json"]
    before = {p: (p.read_bytes(), p.stat().st_mode, p.stat().st_mtime_ns) for p in paths}
    assert _post_setup_hook(provider, stale) is True
    assert {p: (p.read_bytes(), p.stat().st_mode, p.stat().st_mtime_ns) for p in paths} == before
    assert stale == original


@pytest.mark.parametrize("failure", ["exception", "no-write"])
def test_real_host_hook_host_save_failure_raises_visibly(
        setup_host, monkeypatch, default_node, failure):
    from hermes_cli.memory_setup import _post_setup_hook
    host_config = importlib.import_module("hermes_cli.config")
    provider, engine, config = host_setup_target(setup_host, monkeypatch, default_node)
    fake_runtime(setup_host.tmp)
    def failed_save(*args, **kwargs):
        if failure == "exception":
            raise OSError("synthetic host write failure")
    monkeypatch.setattr(host_config, "save_config", failed_save)
    original = copy.deepcopy(config)
    with pytest.raises(RuntimeError, match="Host|host"):
        _post_setup_hook(provider, config)
    assert config == original
    assert not (setup_host.home / "config.yaml").exists()


@pytest.mark.parametrize("failure", ["no-write", "different-readback"])
def test_real_host_hook_provider_readback_failure_cannot_activate(
        setup_host, monkeypatch, default_node, failure):
    from hermes_cli.memory_setup import _post_setup_hook
    provider, engine, config = host_setup_target(setup_host, monkeypatch, default_node)
    fake_runtime(setup_host.tmp)
    def failed_write(path, values, **kwargs):
        if failure == "different-readback":
            path.parent.mkdir(parents=True)
            path.write_text('{"wrong": true}')
    monkeypatch.setattr(engine, "atomic_json_write", failed_write)
    original = copy.deepcopy(config)
    with pytest.raises(RuntimeError, match="Provider configuration readback"):
        _post_setup_hook(provider, config)
    assert config == original
    assert not (setup_host.home / "config.yaml").exists()


@pytest.mark.parametrize("mismatch", ["active-home", "destination"])
def test_whole_host_wrong_destination_refuses_before_artifacts_or_config(
        setup_host, monkeypatch, default_node, mismatch):
    from hermes_constants import set_hermes_home_override, reset_hermes_home_override
    provider, engine, config = host_setup_target(setup_host, monkeypatch, default_node)
    fake_runtime(setup_host.tmp)
    token = None
    if mismatch == "active-home":
        token = set_hermes_home_override(setup_host.home / "profiles/secondary")
    else:
        monkeypatch.setattr(importlib.import_module("hermes_cli.config"), "get_config_path",
                            lambda: setup_host.tmp / "wrong/config.yaml")
    monkeypatch.setattr(engine.subprocess, "run", lambda *a, **k: pytest.fail("wrong host destination must refuse cold"))
    before = tree_state(setup_host.tmp)
    original = copy.deepcopy(config)
    try:
        with pytest.raises(RuntimeError, match="profile|destination"):
            provider.post_setup(str(setup_host.home), config)
        assert tree_state(setup_host.tmp) == before and config == original
    finally:
        if token is not None:
            reset_hermes_home_override(token)


@pytest.mark.parametrize("member", ["launcher", "unit", "manifest"])
@pytest.mark.parametrize("failure", ["missing", "different-bytes"])
def test_real_host_hook_artifact_readback_failure_cannot_publish_settings(
        setup_host, monkeypatch, default_node, member, failure):
    from hermes_cli.memory_setup import _post_setup_hook
    provider, engine, config = host_setup_target(setup_host, monkeypatch, default_node)
    fake_runtime(setup_host.tmp)
    values = config["plugins"]["zvec-memory"]
    target = {"launcher": engine.launcher_path(setup_host.home, values),
              "unit": engine.unit_path(),
              "manifest": engine.runtime_root(setup_host.home, values) / "manifest.json"}[member]
    real_write = engine._write_if_changed
    def faulty_write(path, text, mode=0o600):
        if path == target:
            if failure == "different-bytes":
                real_write(path, "incorrect persisted artifact\n", mode)
            return True
        return real_write(path, text, mode)
    monkeypatch.setattr(engine, "_write_if_changed", faulty_write)
    original = copy.deepcopy(config)
    with pytest.raises(RuntimeError, match="artifact"):
        _post_setup_hook(provider, config)
    assert config == original
    assert not engine.provider_config_path(setup_host.home).exists()
    assert not (setup_host.home / "config.yaml").exists()


@pytest.mark.parametrize("member", ["launcher", "unit", "manifest"])
def test_matching_partial_generation_only_creates_missing_member(
        tmp_path, isolated_home, default_node, member):
    engine = load_engine()
    fake_runtime(tmp_path)
    values = config_for(tmp_path)
    first = engine.ensure_engine(isolated_home, values)
    paths = {"launcher": Path(first["zg_bin"]), "unit": Path(first["unit"]),
             "manifest": engine.runtime_root(isolated_home, values) / "manifest.json"}
    missing = paths.pop(member)
    missing.unlink()
    before = {p: (p.read_bytes(), p.stat().st_mode, p.stat().st_mtime_ns) for p in paths.values()}
    result = engine.ensure_engine(isolated_home, values)
    assert result["writes"] == [member]
    assert {p: (p.read_bytes(), p.stat().st_mode, p.stat().st_mtime_ns) for p in paths.values()} == before


@pytest.mark.parametrize("host_key", ["memory", "plugins", "model", "providers", "agent", "web"])
def test_whole_host_shape_never_migrates_unrelated_blocks_into_provider_json(
        tmp_path, isolated_home, monkeypatch, default_node, host_key):
    engine = load_engine()
    root = fake_runtime(tmp_path)
    monkeypatch.setenv("HERMES_ZVEC_RUNTIME_DIR", str(root))
    monkeypatch.setattr(engine, "_save_host_config", lambda home: None)
    config = {host_key: {"unknown-host-value": False}}
    original = copy.deepcopy(config)
    result = engine.post_setup(isolated_home, config)
    assert json.loads(engine.provider_config_path(isolated_home).read_text()) == {"zg_bin": result["zg_bin"]}
    assert config == original


@pytest.mark.parametrize("raw", [None, b'{"retain": false}', b'{"broken":'])
def test_bare_settings_preserve_any_existing_provider_json(
        tmp_path, isolated_home, default_node, raw):
    engine = load_engine()
    fake_runtime(tmp_path)
    path = engine.provider_config_path(isolated_home)
    before = None
    if raw is not None:
        path.parent.mkdir(parents=True)
        path.write_bytes(raw)
        before = (path.read_bytes(), path.stat().st_mode, path.stat().st_mtime_ns)
    result = engine.post_setup(isolated_home, config_for(tmp_path))
    assert result["owns_config"] is False
    if raw is None:
        assert not path.exists()
    else:
        assert (path.read_bytes(), path.stat().st_mode, path.stat().st_mtime_ns) == before
    assert not (isolated_home / "config.yaml").exists()


@pytest.mark.parametrize("kind", ["directory", "unreadable"])
def test_real_host_hook_preserves_unreadable_present_settings_cold(
        setup_host, monkeypatch, default_node, kind):
    from hermes_cli.memory_setup import _post_setup_hook
    provider, engine, config = host_setup_target(setup_host, monkeypatch, default_node)
    path = engine.provider_config_path(setup_host.home)
    path.parent.mkdir(parents=True)
    if kind == "directory":
        path.mkdir()
    else:
        path.write_text('{"keep": false}')
        real_read = Path.read_text
        def denied_read(self, *args, **kwargs):
            if self == path:
                raise PermissionError("synthetic provider read denial")
            return real_read(self, *args, **kwargs)
        monkeypatch.setattr(Path, "read_text", denied_read)
    before = tree_state(setup_host.tmp)
    original = copy.deepcopy(config)
    with pytest.raises(OSError):
        _post_setup_hook(provider, config)
    assert tree_state(setup_host.tmp) == before and config == original


@pytest.mark.parametrize("scope", ["named", "custom"])
def test_real_host_hook_rejects_active_nondefault_home_cold(
        setup_host, monkeypatch, default_node, scope):
    from hermes_cli.memory_setup import _post_setup_hook
    from hermes_constants import set_hermes_home_override, reset_hermes_home_override
    provider, engine, config = host_setup_target(setup_host, monkeypatch, default_node)
    home = setup_host.home / "profiles/secondary" if scope == "named" else setup_host.tmp / "custom"
    home.mkdir(parents=True)
    sentinel = home / "sentinel"
    sentinel.write_text("other-profile preserved")
    token = set_hermes_home_override(home)
    before = tree_state(setup_host.tmp)
    original = copy.deepcopy(config)
    try:
        with pytest.raises(ValueError, match="default"):
            _post_setup_hook(provider, config)
        assert tree_state(setup_host.tmp) == before and config == original
    finally:
        reset_hermes_home_override(token)


@pytest.mark.parametrize("field", ["package", "version", "node", "entry", "engine_home", "server_url", "launcher", "unit", "unit_status", "tasks_max", "extra"])
def test_inconsistent_manifest_generation_never_rewrites_any_member(
        tmp_path, isolated_home, default_node, field):
    engine = load_engine()
    fake_runtime(tmp_path)
    config = config_for(tmp_path)
    engine.ensure_engine(isolated_home, config)
    path = engine.runtime_root(isolated_home, config) / "manifest.json"
    data = json.loads(path.read_text())
    data[field] = "operator override"
    path.write_text(json.dumps(data))
    before = tree_state(tmp_path)
    with pytest.raises(RuntimeError, match="artifact conflict"):
        engine.ensure_engine(isolated_home, config)
    assert tree_state(tmp_path) == before


def test_matching_legacy_manifest_timestamp_is_retained_byte_for_byte(
        tmp_path, isolated_home, default_node):
    engine = load_engine()
    fake_runtime(tmp_path)
    config = config_for(tmp_path)
    engine.ensure_engine(isolated_home, config)
    path = engine.runtime_root(isolated_home, config) / "manifest.json"
    data = json.loads(path.read_text())
    data["updated"] = "2026-09-27T00:00:00+00:00"
    path.write_text(json.dumps(data))
    before = tree_state(tmp_path)
    result = engine.ensure_engine(isolated_home, config)
    assert result["writes"] == [] and result["status"] == "present"
    assert tree_state(tmp_path) == before


def test_missing_artifact_set_creation_is_byte_mode_mtime_idempotent(
        tmp_path, isolated_home, default_node):
    engine = load_engine()
    fake_runtime(tmp_path)
    config = config_for(tmp_path)
    first = engine.ensure_engine(isolated_home, config)
    assert set(first["writes"]) == {"launcher", "unit", "manifest"}
    paths = [engine.launcher_path(isolated_home, config), engine.unit_path(),
             engine.runtime_root(isolated_home, config) / "manifest.json"]
    before = {p: (p.read_bytes(), p.stat().st_mode, p.stat().st_mtime_ns) for p in paths}
    assert [p.stat().st_mode & 0o777 for p in paths] == [0o700, 0o600, 0o600]
    second = engine.ensure_engine(isolated_home, config)
    assert second["status"] == "present" and second["writes"] == []
    assert {p: (p.read_bytes(), p.stat().st_mode, p.stat().st_mtime_ns) for p in paths} == before
