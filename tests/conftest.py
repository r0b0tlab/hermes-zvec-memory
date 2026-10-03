import os
from pathlib import Path

_raw_root = os.environ.get("ZVEC_TEST_ROOT")
if not _raw_root:
    raise RuntimeError("Run tests through the isolated verification runner")
_test_root = Path(_raw_root).resolve(strict=True)

for _key in (
    "HOME", "HERMES_HOME", "XDG_CONFIG_HOME", "XDG_CACHE_HOME",
    "XDG_DATA_HOME", "XDG_STATE_HOME", "TMPDIR",
    "HERMES_ZVEC_RUNTIME_DIR", "HERMES_ZVEC_MODEL_CACHE",
):
    _raw = os.environ.get(_key)
    if not _raw or not Path(_raw).is_absolute():
        raise RuntimeError(f"{_key} must be an explicit isolated absolute path")
    if not Path(_raw).resolve().is_relative_to(_test_root):
        raise RuntimeError(f"{_key} escapes ZVEC_TEST_ROOT")

if not os.environ.get("HERMES_AGENT_DIR"):
    raise RuntimeError("HERMES_AGENT_DIR must explicitly identify the test host")


import pytest


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    user = tmp_path / "user-home"
    home = user / ".hermes"
    paths = {"HOME": user, "HERMES_HOME": home,
             "XDG_CONFIG_HOME": user / "config", "XDG_CACHE_HOME": user / "cache",
             "XDG_DATA_HOME": user / "data", "XDG_STATE_HOME": user / "state",
             "TMPDIR": user / "tmp", "HERMES_ZVEC_RUNTIME_DIR": user / "runtime",
             "HERMES_ZVEC_MODEL_CACHE": user / "models"}
    for key, path in paths.items():
        path.mkdir(parents=True, exist_ok=True)
        monkeypatch.setenv(key, str(path))
    return home


@pytest.fixture(autouse=True)
def offline_engine(request, monkeypatch):
    if request.node.get_closest_marker("integration"):
        return
    # Never launch native/model work in the offline lane, even if zg exists.
    from test_provider import ZvecMemoryProvider
    monkeypatch.setattr(ZvecMemoryProvider, "_run_zg", lambda *a, **k: (0, "", ""))


@pytest.fixture(autouse=True)
def no_production_engine(tmp_path, monkeypatch):
    """No test may lay out files in the developer's real engine runtime.

    A test that forgot to pass ``runtime_dir`` once rewrote the installed
    launcher; the default root is now redirectable and always redirected here.
    """
    monkeypatch.setenv("HERMES_ZVEC_RUNTIME_DIR", str(tmp_path / "engine-root-guard"))
    monkeypatch.setenv("HERMES_ZVEC_MODEL_CACHE", str(tmp_path / "engine-cache-guard"))
