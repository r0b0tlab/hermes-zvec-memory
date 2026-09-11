"""Vendored host helpers must behave exactly like the host versions they replaced."""
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HOST = os.environ.get("HERMES_AGENT_DIR", str(Path.home() / ".hermes/hermes-agent"))
sys.path.insert(0, HOST)

spec = importlib.util.spec_from_file_location("zvec_hostio", ROOT / "zvec-memory/hostio.py")
assert spec is not None and spec.loader is not None
hostio = importlib.util.module_from_spec(spec)
sys.modules["zvec_hostio"] = hostio
spec.loader.exec_module(hostio)

VALUES = [None, True, False, 0, 1, 2, -1, "", " ", "yes", "YES", "no", "on", "off", "true",
          "false", "1", "0", "enabled", "disabled", "maybe", [], [1], {}, {"a": 1}, 0.0, 0.5]


def test_is_truthy_value_matches_host():
    from utils import is_truthy_value as host
    for value in VALUES:
        assert hostio.is_truthy_value(value) == host(value), value
        assert hostio.is_truthy_value(value, default=True) == host(value, default=True), value


def test_truthy_strings_set_matches_host():
    from utils import TRUTHY_STRINGS
    assert hostio.TRUTHY_STRINGS == frozenset(TRUTHY_STRINGS)


def test_tool_error_matches_host_including_the_bound():
    from tools.registry import tool_error as host
    for message in ("short", "", "x" * 50, "x" * 4096, "unicode ✓ error"):
        assert json.loads(hostio.tool_error(message)) == json.loads(host(message)), len(message)
    assert json.loads(hostio.tool_error("m", status="failed")) == json.loads(host("m", status="failed"))


def test_cfg_get_matches_host():
    from hermes_cli.config import cfg_get as host
    cfg = {"a": {"b": {"c": 1, "none": None}}, "empty": {}}
    for keys in (("a",), ("a", "b"), ("a", "b", "c"), ("a", "b", "none"), ("a", "zz"), ("zz",),
                 ("empty", "x")):
        assert hostio.cfg_get(cfg, *keys) == host(cfg, *keys), keys
        assert hostio.cfg_get(cfg, *keys, default="d") == host(cfg, *keys, default="d"), keys
    assert hostio.cfg_get(None, "a") is host(None, "a")


def test_atomic_json_write_is_atomic_and_private(tmp_path):
    path = tmp_path / "state.json"
    hostio.atomic_json_write(path, {"a": 1}, mode=0o600)
    assert json.loads(path.read_text()) == {"a": 1}
    assert (path.stat().st_mode & 0o777) == 0o600
    assert not [item for item in tmp_path.iterdir() if item.suffix == ".tmp"], \
        "no temp file may be left behind"
    hostio.atomic_json_write(path, {"a": 2}, mode=0o600)
    assert json.loads(path.read_text()) == {"a": 2}


def test_read_user_config_raw_matches_host(tmp_path):
    from hermes_cli.config import read_user_config_raw as host
    path = tmp_path / "config.yaml"
    path.write_text("memory:\n  provider: zvec-memory\nnested:\n  a: 1\n")
    assert hostio.read_user_config_raw(path) == host(path)
    missing = tmp_path / "nope.yaml"
    assert hostio.read_user_config_raw(missing) == host(missing) == {}


PROBE = r'''
import importlib.util, sys
sys.path.insert(0, {host!r})
# The supported host surface, loaded before the guard so its own transitive
# needs cannot be mistaken for ours.
import agent.memory_provider          # the documented ABC
import hermes_constants               # documented canonical path helpers
import plugins.memory.config_schema   # host-provided schema helpers

FORBIDDEN = {forbidden!r}


class Guard:
    def find_spec(self, name, path=None, target=None):
        if name in FORBIDDEN or name.split(".")[0] in FORBIDDEN:
            raise RuntimeError("forbidden host import: " + name)
        return None


sys.meta_path.insert(0, Guard())
spec = importlib.util.spec_from_file_location("zvec_probe", {provider!r})
module = importlib.util.module_from_spec(spec)
sys.modules["zvec_probe"] = module
spec.loader.exec_module(module)
print("ok")
'''


def test_runtime_path_needs_no_forbidden_host_modules():
    """The provider must load without hermes_cli/tools.registry/utils/compressor."""
    code = PROBE.format(host=HOST,
                        forbidden=("hermes_cli.config", "tools.registry", "utils",
                                   "agent.context_compressor"),
                        provider=str(ROOT / "zvec-memory/__init__.py"))
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert result.stdout.strip() == "ok", (result.stdout, result.stderr[-2000:])
