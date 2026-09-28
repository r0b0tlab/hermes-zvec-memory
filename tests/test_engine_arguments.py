"""Execute generated launchers against a harmless argument-capture program."""
import json
import os
import shutil
import subprocess

import pytest

from test_engine import fake_runtime, load_engine


def tree_state(root):
    return {str(path.relative_to(root)): (
        path.is_dir(), path.stat().st_mode, path.stat().st_mtime_ns,
        path.read_bytes() if path.is_file() else None)
        for path in [root, *root.rglob("*")]}


@pytest.mark.parametrize("operation", ["ensure_engine", "install_engine"])
@pytest.mark.parametrize("setting", ["runtime_dir", "engine_home"])
@pytest.mark.parametrize("bad", ["'", '"', "\\", "$", "%", "\n", "\r", "\t", "\x1b", "\x7f"],
                         ids=["apostrophe", "quote", "backslash", "dollar", "percent",
                              "newline", "return", "tab", "escape", "delete"])
def test_systemd_bound_values_refused_before_any_effect(
        tmp_path, monkeypatch, isolated_home, operation, setting, bad):
    from types import SimpleNamespace

    engine = load_engine()
    config = {"runtime_dir": str(tmp_path / "runtime"),
              "engine_home": str(tmp_path / "engine")}
    config[setting] = str(tmp_path / ("unsupported" + bad + "value"))
    entry = engine.entry_path(isolated_home, config)
    entry.parent.mkdir(parents=True)
    entry.write_text("// non-executable fixture\n")
    engine.package_json_path(isolated_home, config).write_text(
        json.dumps({"name": engine.ENGINE_PACKAGE, "version": engine.PINNED_VERSION}))
    calls = []
    monkeypatch.setattr(engine.shutil, "which", lambda name: "/not-executed/npm")
    monkeypatch.setattr(engine.subprocess, "run", lambda *a, **k: (
        calls.append((a, k)) or SimpleNamespace(returncode=1, stdout="", stderr="fixture")))
    before = tree_state(tmp_path)

    with pytest.raises(ValueError, match="systemd"):
        getattr(engine, operation)(isolated_home, config)

    assert calls == []
    assert tree_state(tmp_path) == before


def test_systemd_template_preserves_spaces_in_each_argument(tmp_path, isolated_home):
    engine = load_engine()
    config = {"runtime_dir": str(tmp_path / "runtime with spaces"),
              "engine_home": str(tmp_path / "engine with spaces")}

    unit = engine.unit_template(isolated_home, config)

    assert next(line for line in unit.splitlines() if line.startswith("ExecStart=")) == (
        f'ExecStart="{engine.launcher_path(isolated_home, config)}" server run '
        '--listen "127.0.0.1:17999" '
        f'--token-file "{engine.token_file(isolated_home, config)}"')


@pytest.mark.parametrize("bad", ["'", '"', "\\", "$", "%"])
def test_systemd_listen_argument_refuses_expansion_characters(isolated_home, bad):
    engine = load_engine()
    with pytest.raises(ValueError, match="systemd"):
        engine.unit_template(isolated_home, {"server_url": f"http://host{bad}name:17999/mcp"})


def test_systemd_parser_accepts_quoted_spaces_and_rejects_unquoted_control(
        tmp_path, isolated_home):
    engine = load_engine()
    analyzer = shutil.which("systemd-analyze")
    assert analyzer is not None, "systemd-analyze is required for the unit grammar test"
    config = {"runtime_dir": str(tmp_path / "runtime with spaces"),
              "engine_home": str(tmp_path / "engine with spaces")}
    launcher = engine.launcher_path(isolated_home, config)
    launcher.parent.mkdir(parents=True)
    # Verification must parse this unit, never execute its command.
    launcher.write_text("#!/usr/bin/env bash\nexit 73\n")
    launcher.chmod(0o700)
    text = engine.unit_template(isolated_home, config)
    good = tmp_path / "quoted-fixture.service"
    bad = tmp_path / "unquoted-fixture.service"
    good.write_text(text)
    bad.write_text(text.replace(f'ExecStart="{launcher}"', f'ExecStart={launcher}', 1))
    runtime = tmp_path / "systemd-runtime"
    runtime.mkdir(mode=0o700)
    environment = {**os.environ, "XDG_RUNTIME_DIR": str(runtime)}
    results = []
    for unit in (good, bad):
        result = subprocess.run(
            [analyzer, "--user", "--man=no", "--generators=no", "--recursive-errors=no",
             "verify", str(unit)],
            stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=10,
            env=environment)
        results.append(result)
    (tmp_path / "parser-results.json").write_text(json.dumps([
        {"returncode": result.returncode, "stdout": result.stdout, "stderr": result.stderr}
        for result in results]))
    assert results[0].returncode == 0, results[0].stderr
    assert results[1].returncode != 0, "negative control did not detect the broken executable path"
    assert f"Command {str(launcher).split(' ', 1)[0]} is not executable" in results[1].stderr


def test_shell_only_model_cache_is_not_restricted_by_systemd_grammar(tmp_path, isolated_home):
    engine = load_engine()
    root = fake_runtime(tmp_path)
    config = {"runtime_dir": str(root), "engine_home": str(tmp_path / "engine"),
              "model_cache": str(tmp_path / "cache'\"\\$percent%")}

    result = engine.ensure_engine(isolated_home, config)

    assert result["status"] == "updated"
    assert engine.unit_path().read_text() == engine.unit_template(isolated_home, config)
    assert engine.launcher_path(isolated_home, config).read_text() == engine.launcher_script(isolated_home, config)


@pytest.mark.parametrize("name", ["with spaces", "with'quote", 'with"quote',
                                  "with$ZVEC_ARGUMENT_SENTINEL", "with\\backslash"],
                         ids=["spaces", "apostrophe", "double-quote", "dollar", "backslash"])
def test_generated_launcher_preserves_paths_environment_and_arguments(tmp_path, monkeypatch, name):
    engine = load_engine()
    probe_dir = tmp_path / name
    probe_dir.mkdir()
    node = probe_dir / "capture-node"
    node.write_text(
        "#!/usr/bin/python3\n"
        "import json, os, sys\n"
        "print(json.dumps({'argv': sys.argv[1:], 'env': {key: os.environ.get(key) "
        "for key in ('ZVEC_GREP_HOME', 'ZVEC_GREP_MODEL_CACHE', 'ZVEC_GREP_MODE', "
        "'ZVEC_GREP_SERVER_URL', 'ZVEC_GREP_SERVER_TOKEN_FILE', 'ZVEC_GREP_SERVER_TOKEN', "
        "'ZVEC_GREP_API_KEY', 'ZVEC_GREP_ENDPOINT', 'DASHSCOPE_API_KEY', 'QWEN_API_KEY')}}))\n",
        encoding="utf-8")
    node.chmod(0o700)
    monkeypatch.setattr(engine, "NODE_BIN", str(node))
    monkeypatch.setenv("ZVEC_ARGUMENT_SENTINEL", "expanded-would-be-wrong")
    for key in ("ZVEC_GREP_SERVER_TOKEN", "ZVEC_GREP_API_KEY", "ZVEC_GREP_ENDPOINT",
                "DASHSCOPE_API_KEY", "QWEN_API_KEY"):
        monkeypatch.setenv(key, "synthetic-must-be-removed")
    home = tmp_path / "fixture-home"
    config = {"runtime_dir": str(probe_dir / "runtime"),
              "engine_home": str(probe_dir / "engine"),
              "model_cache": str(probe_dir / "cache"),
              "server_url": "http://127.0.0.1:17999/mcp?label=$ZVEC_ARGUMENT_SENTINEL"}
    launcher = tmp_path / "launcher"
    launcher.write_text(engine.launcher_script(home, config), encoding="utf-8")
    launcher.chmod(0o700)
    args = ["query", "two words", "", "$literal", "*.md", "semi;colon", "quote'\"", "漢字"]

    result = subprocess.run([str(launcher), *args], stdin=subprocess.DEVNULL,
                            capture_output=True, text=True, timeout=5, env=dict(os.environ))

    assert result.returncode == 0, result.stderr
    observed = json.loads(result.stdout)
    assert observed["argv"] == [str(engine.entry_path(home, config)), *args]
    assert observed["env"] == {
        "ZVEC_GREP_HOME": str(probe_dir / "engine"),
        "ZVEC_GREP_MODEL_CACHE": str(probe_dir / "cache/models"),
        "ZVEC_GREP_MODE": "auto",
        "ZVEC_GREP_SERVER_URL": config["server_url"],
        "ZVEC_GREP_SERVER_TOKEN_FILE": str(probe_dir / "engine/server.token"),
        "ZVEC_GREP_SERVER_TOKEN": None,
        "ZVEC_GREP_API_KEY": None,
        "ZVEC_GREP_ENDPOINT": None,
        "DASHSCOPE_API_KEY": None,
        "QWEN_API_KEY": None,
    }
