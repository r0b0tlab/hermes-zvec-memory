"""Bounded real CLI dispatch for mirror JSON decoder recursion failures."""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from source_support import PROVIDER_ROOT
from test_cli import healthy_vault


# The real argparse handler and target selection run in a fresh process. Only
# external command execution and the *selected* map decoder call are replaced.
CHILD = r'''
import argparse, importlib.util, json, os, sys, types
from pathlib import Path
sys.path.insert(0, os.environ['HERMES_AGENT_DIR'])
spec = importlib.util.spec_from_file_location('recursion_cli', sys.argv[1])
cli = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = cli
spec.loader.exec_module(cli)
vault, command, mode, output, receipt = Path(sys.argv[2]), *sys.argv[3:]
cli._default_runner = lambda args, timeout=15: (0, '0.2.2' if '--version' in args else 'ready', '')
real_loads = json.loads
map_text = (vault / '.mirror-map.json').read_text()
if mode == 'natural':
    try:
        real_loads(map_text)
    except RecursionError:
        pass
    else:
        raise AssertionError('natural input did not reach the decoder recursion limit')
calls = 0
def loads(text, *args, **kwargs):
    global calls
    if text != map_text:
        return real_loads(text, *args, **kwargs)
    # cli.json is private to this module; settings/journal remain real.
    calls += 1
    Path(receipt).write_text(str(calls))
    if (mode == 'inject_first' and calls == 1) or (mode == 'inject_second' and calls == 2):
        raise RecursionError('PRIVATE_RECURSION_EXCEPTION_CANARY')
    return real_loads(text, *args, **kwargs)
cli.json = types.SimpleNamespace(loads=loads, dumps=json.dumps)
parser = argparse.ArgumentParser()
cli.register_cli(parser)
argv = [command, '--vault', str(vault)]
if output == 'json':
    argv.append('--json')
args = parser.parse_args(argv)
sys.exit(args.func(args))
'''


@pytest.mark.parametrize("command", ["doctor", "status", "reindex"])
@pytest.mark.parametrize("output", ["json", "human"])
@pytest.mark.parametrize("mode", ["natural", "inject_first", "inject_second", "valid_nested", "corrupt"])
def test_map_decoder_recursion_dispatch(tmp_path, command, output, mode):
    vault = healthy_vault(tmp_path)
    mapping = vault / ".mirror-map.json"
    base = mapping.read_text()
    # Unknown map metadata is deliberately nested: it is ignored by the
    # journal validator but traversed by the actual JSON parser.
    if mode == "natural":
        # Python 3.14's C decoder accepts 10,000 levels; 100,000 reaches
        # its actual decoder limit while remaining a small (~200 KiB) input.
        text = base[:-1] + ', "extra": ' + '[' * 100000 + '0' + ']' * 100000 + '}'
    elif mode == "valid_nested":
        text = base[:-1] + ', "extra": {"nest": [[{"x": 1}]]}}'
        assert json.loads(text)["extra"]["nest"][0][0]["x"] == 1
    elif mode == "corrupt":
        text = base[:-1] + ', "extra": "PRIVATE_CORRUPT_MAP_CANARY"'
    else:
        text = base
    mapping.write_text(text)
    home = Path(os.environ["HERMES_HOME"])
    builtin = home / "memories" / "MEMORY.md"
    builtin.parent.mkdir(parents=True)
    builtin.write_text("# Memory\n§\nsomething worth recalling\n")
    config = home / "zvec-memory" / "config.json"
    config.parent.mkdir(parents=True)
    config.write_text(json.dumps({"zg_bin": "/zg"}))
    protected = [mapping, builtin, config, vault / "facts" / "one.md",
                 vault / ".mirror-inbox.sqlite3", vault / ".zvec-grep" / "manifest.json",
                 vault / ".zvec-grep" / ".zvec-memory-state.json"]
    before = {p: p.read_bytes() for p in protected}
    receipt = tmp_path / "decoder-calls"
    result = subprocess.run([sys.executable, "-B", "-c", CHILD,
                             str(PROVIDER_ROOT / "zvec-memory" / "cli.py"), str(vault),
                             command, mode, output, str(receipt)],
                            env=os.environ.copy(), capture_output=True, text=True, timeout=8)
    assert result.stderr == ""
    assert receipt.read_text() == "2"  # both independent parser paths reached
    assert "PRIVATE_RECURSION_EXCEPTION_CANARY" not in result.stdout
    assert "PRIVATE_CORRUPT_MAP_CANARY" not in result.stdout
    assert "Traceback" not in result.stdout
    healthy = mode == "valid_nested"
    assert result.returncode == (1 if command == "doctor" and not healthy else 0)
    if output == "json":
        payload = json.loads(result.stdout)
        assert payload["checked"] is True
        assert payload["ok"] is healthy
        checks = {check["name"]: check for check in payload["checks"]}
        assert checks["mirror"]["ok"] is (mode in ("valid_nested", "inject_first"))
        assert checks["builtin_mirror"]["ok"] is (mode in ("valid_nested", "inject_second"))
        if mode in ("natural", "inject_first"):
            assert "RecursionError" in checks["builtin_mirror"]["detail"]
        if mode in ("natural", "inject_second"):
            assert "RecursionError" in checks["mirror"]["detail"]
        assert payload["action"]["status"] == ("requested" if command == "reindex" else "checked")
        if command == "reindex":
            assert payload["action"]["completed"] is False
            assert payload["action"]["request"] == str(vault / ".reindex-request.json")
    else:
        assert ("  healthy\n" in result.stdout) is healthy
        assert ("FAIL mirror" in result.stdout) is (mode in ("natural", "inject_second", "corrupt"))
        assert ("FAIL builtin_mirror" in result.stdout) is (mode in ("natural", "inject_first", "corrupt"))
        if mode.startswith("inject_") or mode == "natural":
            assert "RecursionError" in result.stdout
        if command == "reindex":
            assert "Rebuild requested" in result.stdout and "not complete" in result.stdout
            assert str(vault / ".reindex-request.json") in result.stdout
    marker = vault / ".reindex-request.json"
    assert marker.is_file() is (command == "reindex")
    assert not (home / "zvec-memory" / ".reindex-request.json").exists()
    if command == "reindex":
        assert json.loads(marker.read_text())["by"] == "hermes zvec-memory reindex"
    assert {p: p.read_bytes() for p in protected} == before
