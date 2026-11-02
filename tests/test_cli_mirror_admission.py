"""Bounded mirror admission exercised through timeout-bounded CLI dispatch."""
import hashlib
import json
import os
import socket
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from source_support import PROVIDER_ROOT
from test_cli import healthy_vault, load_cli


# Run the real parser/dispatcher in a fresh process. Only target selection and
# external engine/systemctl are substituted; fault hooks run at real I/O seams.
CHILD = r'''
import argparse, importlib.util, os, sys
from pathlib import Path
sys.path.insert(0, os.environ["HERMES_AGENT_DIR"])
spec = importlib.util.spec_from_file_location("zvec_cli_child", sys.argv[1])
cli = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = cli
spec.loader.exec_module(cli)
vault = Path(sys.argv[2])
command, fault = sys.argv[3:5]
cli._configured = lambda: (vault, {"zg_bin": "/zg"})
cli._default_runner = lambda args, timeout=15: (0, "0.2.2" if "--version" in args else "Workspace index is ready", "")
real_open, real_read = os.open, os.read
fds = set()
count = [0]
def opening(path, flags, *args, **kwargs):
    if str(path) == str(vault / ".mirror-map.json"):
        if fault == "replace_open":
            count[0] += 1
            p = Path(path)
            data = p.read_bytes()
            p.rename(vault / ("held-open-" + str(count[0])))
            p.write_bytes(data)
        if fault == "symlink_open":
            p = Path(path)
            p.rename(vault / "held-symlink")
            p.symlink_to(vault.parent / "secret-map")
        if fault == "fifo_open":
            p = Path(path)
            p.rename(vault / "held-fifo")
            os.mkfifo(p)
        fd = real_open(path, flags, *args, **kwargs)
        fds.add(fd)
        return fd
    return real_open(path, flags, *args, **kwargs)
def reading(fd, n):
    if fd in fds:
        if fault == "short":
            return b""
        if fault == "oversize_tripwire":
            raise RuntimeError("oversize map was read")
        data = real_read(fd, n)
        if fault == "touch_read" and data:
            count[0] += 1
            p = vault / ".mirror-map.json"
            s = p.stat()
            os.utime(p, ns=(s.st_atime_ns, s.st_mtime_ns + 1000000))
            (vault / ("touch-" + str(count[0]))).write_text("seen")
        if fault == "replace_read" and data:
            count[0] += 1
            p = vault / ".mirror-map.json"
            old = p.read_bytes()
            p.rename(vault / ("held-read-" + str(count[0])))
            p.write_bytes(old)
        return data
    return real_read(fd, n)
os.open, os.read = opening, reading
parser = argparse.ArgumentParser()
cli.register_cli(parser)
args = parser.parse_args([command, "--json"])
sys.exit(args.func(args))
'''


def digest(path):
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.digest()


@pytest.mark.parametrize("command", ["doctor", "status", "reindex"])
@pytest.mark.parametrize("kind", ["valid", "absent", "absent_empty", "valid_symlink", "dangling", "fifo", "directory", "socket", "oversized", "replace_open", "symlink_open", "fifo_open", "replace_read", "touch_read", "short"])
def test_dispatch_map_admission(tmp_path, command, kind):
    cli = load_cli()
    vault = healthy_vault(tmp_path)
    mapping = vault / ".mirror-map.json"
    initial = mapping.read_bytes()
    # Real builtin comparison is enabled, with a value contained in the map.
    builtin = Path(os.environ["HERMES_HOME"]) / "memories" / "MEMORY.md"
    builtin.parent.mkdir(parents=True)
    builtin.write_text("# Memory\n§\nsomething worth recalling\n")
    builtin_bytes = builtin.read_bytes()
    canary = "MAP_PRIVATE_CANARY_9726"
    target = tmp_path / "secret-map"
    secret = json.loads(initial)
    secret["records"]["a"]["content"] = canary
    target.write_text(json.dumps(secret))
    target_before = target.read_bytes()
    sock = None
    fault = kind if kind in ("replace_open", "symlink_open", "fifo_open", "replace_read", "touch_read", "short") else "none"
    if kind in ("absent", "absent_empty", "valid_symlink", "dangling", "fifo", "directory", "socket"):
        mapping.unlink()
    if kind == "absent_empty":
        builtin.unlink()
    if kind == "valid_symlink":
        mapping.symlink_to(target)
    elif kind == "dangling":
        mapping.symlink_to(tmp_path / "missing")
    elif kind == "fifo":
        os.mkfifo(mapping)
    elif kind == "directory":
        mapping.mkdir()
    elif kind == "socket":
        sock = socket.socket(socket.AF_UNIX)
        directory_fd = os.open(vault, os.O_RDONLY | os.O_DIRECTORY)
        try:
            sock.bind(f"/proc/self/fd/{directory_fd}/.mirror-map.json")
        finally:
            os.close(directory_fd)
    elif kind == "oversized":
        with mapping.open("wb") as stream:
            stream.write(canary.encode())
            stream.truncate(cli.MIRROR_MAP_MAX_BYTES + 1)
        fault = "oversize_tripwire"
    before = digest(mapping) if kind == "oversized" else None
    try:
        result = subprocess.run([sys.executable, "-B", "-c", CHILD,
                                 str(PROVIDER_ROOT / "zvec-memory/cli.py"), str(vault), command, fault],
                                capture_output=True, text=True, timeout=8, env=os.environ.copy())
    finally:
        if sock:
            sock.close()
    assert canary not in result.stdout + result.stderr
    assert result.stderr == ""
    payload = json.loads(result.stdout)
    # A missing map is optional, but a real builtin is unmirrored in that case.
    if kind in ("valid", "absent_empty"):
        assert result.returncode == 0 and payload["ok"] is True
    elif kind == "absent":
        assert (result.returncode, payload["ok"]) == (1 if command == "doctor" else 0, False)
        assert "mirror" not in payload["failures"] and "builtin_mirror" in payload["failures"]
    else:
        assert result.returncode == (1 if command == "doctor" else 0)
        assert payload["ok"] is False and payload["checked"] is True
        assert {"mirror", "builtin_mirror"} <= set(payload["failures"])
    assert payload["action"]["status"] == ("requested" if command == "reindex" else "checked")
    if command == "reindex":
        assert payload["action"]["completed"] is False
    assert target.read_bytes() == target_before
    if kind == "absent_empty":
        assert not builtin.exists()
    else:
        assert builtin.read_bytes() == builtin_bytes
    if kind in ("valid", "replace_open", "replace_read", "touch_read", "short"):
        assert mapping.read_bytes() == initial
    if kind == "touch_read":
        assert len(list(vault.glob("touch-*"))) == 2
    if kind == "symlink_open":
        assert mapping.is_symlink() and (vault / "held-symlink").read_bytes() == initial
    if kind == "fifo_open":
        assert (vault / "held-fifo").read_bytes() == initial
        assert stat.S_ISFIFO(mapping.lstat().st_mode)
    if kind in ("replace_open", "replace_read"):
        held = list(vault.glob("held-*-*"))
        assert len(held) == 2 and all(p.read_bytes() == initial for p in held)
    if kind == "oversized":
        assert digest(mapping) == before
    if kind in ("valid_symlink", "dangling"):
        assert mapping.is_symlink()


@pytest.mark.parametrize("missing_flag", ["O_NOFOLLOW", "O_NONBLOCK"])
def test_admission_without_required_primitives_and_short_chunks(tmp_path, monkeypatch, missing_flag):
    cli = load_cli()
    vault = healthy_vault(tmp_path)
    expected = cli.read_mirrored_contents(vault)
    read = os.read
    monkeypatch.setattr(cli.os, "read", lambda fd, n: read(fd, min(n, 3)))
    assert cli.read_mirrored_contents(vault) == expected
    monkeypatch.delattr(cli.os, missing_flag)
    with pytest.raises(OSError):
        cli.read_mirrored_contents(vault)
    assert cli.vault_state(vault)["mirror_error"] == "OSError"


def test_small_boundary_and_truly_absent_control(tmp_path, monkeypatch):
    cli = load_cli()
    vault = healthy_vault(tmp_path)
    mapping = vault / ".mirror-map.json"
    original = mapping.read_bytes()
    monkeypatch.setattr(cli, "MIRROR_MAP_MAX_BYTES", len(original))
    assert cli._read_mirror_map(vault).encode() == original
    mapping.write_bytes(original + b" ")
    with pytest.raises(ValueError):
        cli._read_mirror_map(vault)
    mapping.unlink()
    assert cli._read_mirror_map(vault) is None
    assert cli.read_mirrored_contents(vault) == set()
    assert "mirror_error" not in cli.vault_state(vault)
