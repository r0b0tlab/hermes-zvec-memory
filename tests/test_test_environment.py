"""The test suite must refuse ambient roots before collection-time imports."""
import os
from pathlib import Path
import subprocess
import sys

import pytest

CONFTEXT = Path(__file__).with_name("conftest.py")
ROOT_KEYS = ("HOME", "HERMES_HOME", "XDG_CONFIG_HOME", "XDG_CACHE_HOME",
             "XDG_DATA_HOME", "XDG_STATE_HOME", "TMPDIR",
             "HERMES_ZVEC_RUNTIME_DIR", "HERMES_ZVEC_MODEL_CACHE")


@pytest.mark.parametrize("missing", ["ZVEC_TEST_ROOT", "HERMES_AGENT_DIR", *ROOT_KEYS])
def test_missing_outer_root_refused_before_import(missing):
    env = dict(os.environ)
    env.pop(missing, None)
    result = subprocess.run([sys.executable, "-I", "-B", str(CONFTEXT)],
                            env=env, capture_output=True, text=True, timeout=5)
    assert result.returncode != 0, missing
    assert "RuntimeError" in result.stderr


@pytest.mark.parametrize("key", ROOT_KEYS)
def test_escaping_outer_root_refused_before_import(key):
    env = dict(os.environ)
    env[key] = str(Path(env["ZVEC_TEST_ROOT"]).parent / "outside-test-root")
    result = subprocess.run([sys.executable, "-I", "-B", str(CONFTEXT)],
                            env=env, capture_output=True, text=True, timeout=5)
    assert result.returncode != 0, key
    assert "escapes ZVEC_TEST_ROOT" in result.stderr


def test_each_fixture_redirects_all_roots(tmp_path):
    for key in ROOT_KEYS:
        assert Path(os.environ[key]).resolve().is_relative_to(tmp_path), key
