"""Opt-in native zg contract tests; use synthetic data and isolated homes."""
import json
import os
from pathlib import Path
import shutil
import subprocess

import pytest

pytestmark = [pytest.mark.integration, pytest.mark.skipif(
    os.environ.get("ZVEC_RUN_NATIVE") != "1", reason="set ZVEC_RUN_NATIVE=1 for native tests")]


@pytest.fixture(scope="module")
def native(tmp_path_factory):
    root = tmp_path_factory.mktemp("native-zg")
    vault = root / "vault"
    facts = vault / "facts"
    sessions = vault / "sessions"
    facts.mkdir(parents=True)
    sessions.mkdir()
    (facts / "deploy.md").write_text(
        "# Deployment policy\nProduction deployment requires the canary gate.\n"
        "The special deployment label is --allow-remote.\n", encoding="utf-8")
    (facts / "unicode.md").write_text(
        "# Café preference\nUser prefers café coffee and concise replies.\n", encoding="utf-8")
    (sessions / "note.md").write_text("# Office\nOffice plants are watered Tuesday.\n", encoding="utf-8")
    (vault / "config.json").write_text(json.dumps({"canary": "do-not-index-cobalt-otter"}))
    env = os.environ.copy()
    for key in list(env):
        if key.startswith("ZVEC_GREP_"):
            env.pop(key)
    home = root / "home"
    home.mkdir()
    env.update(HOME=str(home), HERMES_HOME=str(home / ".hermes"))
    env["ZVEC_GREP_MODEL_CACHE"] = os.environ.get(
        "ZVEC_TEST_MODEL_CACHE", str(root / "models"))
    binary = os.environ.get("ZVEC_TEST_BIN") or shutil.which("zg")
    assert binary, "Native tests requested but zg not installed"

    def run(*args, timeout=120):
        result = subprocess.run([binary, *args], cwd=vault, env=env,
                                capture_output=True, text=True, timeout=timeout)
        assert result.returncode == 0, result.stderr + result.stdout
        return result.stdout

    run("index", str(vault), "--mode", "direct", "--embedding",
        "local/potion-retrieval-32m", "-g", "**/*.md", timeout=900)
    run("status", str(vault), "--check-ready")
    return vault, run, {"binary": binary, "env": env}


@pytest.mark.parametrize("mode", ["hybrid", "fts", "vector"])
def test_native_retrieval_modes(native, mode):
    _, run, _ = native
    out = run("query", "--mode", "direct", "--preview", "short",
              "--limit", "5", f"--{mode}=production deployment canary gate")
    assert "canary gate" in out
    assert "facts/deploy.md" in out


def test_native_scope_unicode_and_option_like_query(native):
    _, run, _ = native
    out = run("query", "--mode", "direct", "--preview", "short",
              "--fts=café", "-g", "facts/**")
    assert "unicode.md" in out
    assert "sessions/note.md" not in out
    out = run("query", "--mode", "direct", "--preview", "short",
              "--hybrid=--allow-remote")
    assert "Usage:" not in out
    assert "deploy.md" in out


def test_native_markdown_selection_excludes_config(native):
    _, run, _ = native
    out = run("query", "--mode", "direct", "--preview", "full",
              "--fts=do-not-index-cobalt-otter")
    assert "config.json" not in out


def test_native_refresh_removes_deleted_fact(native):
    vault, run, _ = native
    path = vault / "facts" / "erasable.md"
    path.write_text("# Erasable\nvermillion narwhal retirement policy\n")
    run("index", str(vault), "--mode", "direct", timeout=900)
    before = run("query", "--mode", "direct", "--preview", "short",
                 "--fts=vermillion narwhal")
    assert "erasable.md" in before
    path.unlink()
    run("index", str(vault), "--mode", "direct", timeout=900)
    after = run("query", "--mode", "direct", "--preview", "short",
                "--fts=vermillion narwhal")
    assert "erasable.md" not in after


def test_native_concurrent_refresh_reports_lock_or_success(native):
    vault, run, settings = native
    command = [settings["binary"], "index", str(vault), "--mode", "direct"]
    processes = [subprocess.Popen(command, cwd=vault, env=settings["env"],
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                  text=True) for _ in range(2)]
    outputs = [p.communicate(timeout=120) for p in processes]
    assert any(p.returncode == 0 for p in processes), outputs
    for p, (out, err) in zip(processes, outputs):
        if p.returncode:
            print("Concurrent native index failure:\n" + out + err)
            assert any(code in err for code in (
                "ZVEC_GREP.ENGINE.LOCK.BUSY",
                "ZVEC_GREP.ENGINE.DAEMON_LEASE_ACTIVE",
            )), (out, err)
    run("status", str(vault), "--check-ready")
    assert "canary gate" in run("query", "--mode", "direct", "--preview", "short",
                                "--fts=canary gate")
