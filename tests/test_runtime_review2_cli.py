"""Real CLI admission participates in byte-conditional acknowledgment."""
import importlib
import json
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from test_cli import load_cli, PROVIDER_ROOT
from test_provider import _mod
from zvec_memory_provider.maintenance import request_rebuild, read_request, request_identity, acknowledge_request


@pytest.fixture(params=["standalone", "package"])
def cli(request):
    return load_cli() if request.param == "standalone" else importlib.import_module(_mod.__name__ + ".cli")


def test_generic_cli_retains_identity_and_new_request_generation(tmp_path, cli):
    identity = {"plugin_schema": 1, "embedding": "local/new", "zg_bin": "zg", "zg_version": "v2"}
    request_rebuild(tmp_path, identity=identity)
    first = read_request(tmp_path)
    path = cli._request_reindex(tmp_path)
    second = read_request(tmp_path)
    assert second != first
    assert request_identity(second) == identity
    assert not acknowledge_request(tmp_path, first, identity=identity)
    assert path.read_bytes() == second


@pytest.mark.parametrize("payload", [b'{"identity":', b'{"identity":null}', b'[]'])
def test_generic_cli_cannot_downgrade_malformed_pending_intent(tmp_path, cli, payload):
    path = tmp_path / _mod.REINDEX_REQUEST_FILE
    path.write_bytes(payload)
    with pytest.raises(ValueError):
        cli._request_reindex(tmp_path)
    assert path.read_bytes() == payload


def test_generic_protocol_retains_bound_intent_but_explicit_intent_supersedes(tmp_path):
    identity = {"plugin_schema": 1, "embedding": "local/new", "zg_bin": "zg"}
    request_rebuild(tmp_path, identity=identity)
    request_rebuild(tmp_path)
    assert request_identity(read_request(tmp_path)) == identity
    superseding = {**identity, "embedding": "local/newest"}
    request_rebuild(tmp_path, identity=superseding)
    assert request_identity(read_request(tmp_path)) == superseding


def test_cli_writer_cannot_land_between_ack_comparison_and_unlink(tmp_path, monkeypatch, cli):
    path = request_rebuild(tmp_path)
    first = read_request(tmp_path)
    entered, completed = threading.Event(), threading.Event()
    errors, during_unlink, written = [], [], []
    real_unlink = Path.unlink

    def writer():
        entered.set()
        try:
            cli._request_reindex(tmp_path)
            written.append(path.read_bytes())
        except BaseException as exc:
            errors.append(repr(exc))
        finally:
            completed.set()

    thread = threading.Thread(target=writer)

    def paused_unlink(self, *args, **kwargs):
        if self == path:
            # This point is after acknowledge_request compared A. The writer
            # owns a distinct thread; a reentrant callback cannot prove this.
            thread.start()
            assert entered.wait(5)
            during_unlink.append(completed.wait(0.2))
        return real_unlink(self, *args, **kwargs)

    try:
        with monkeypatch.context() as m:
            m.setattr(Path, "unlink", paused_unlink)
            assert acknowledge_request(tmp_path, first)
        thread.join(5)
        assert not thread.is_alive() and not errors, errors
        assert path.exists(), "old acknowledgment deleted the real CLI's replacement"
        assert path.read_bytes() == written[0] != first
        assert during_unlink == [False], "CLI publication did not wait for compare/unlink"
    finally:
        if thread.ident is not None:
            thread.join(5)


def test_real_cli_admission_never_waits_for_native_vault_lock(tmp_path, cli):
    from zvec_memory_provider.transactions import VaultLock
    tmp_path.mkdir(exist_ok=True)
    done, errors = threading.Event(), []

    def writer():
        try:
            cli._request_reindex(tmp_path)
        except BaseException as exc:
            errors.append(repr(exc))
        finally:
            done.set()

    thread = threading.Thread(target=writer)
    try:
        with VaultLock(tmp_path / ".mirror.lock"):
            thread.start()
            assert done.wait(1), "CLI admission waited for native work"
            assert not errors, errors
        assert read_request(tmp_path) is not None
    finally:
        thread.join(5)


def test_standalone_request_helper_stays_cold(tmp_path):
    code = f'''
import importlib.util, sys
from pathlib import Path
class Guard:
    def find_spec(self, name, path=None, target=None):
        if name.split('.')[0] in ('agent', 'hermes_cli', 'tools', 'yaml'):
            raise RuntimeError('not a cold import: ' + name)
sys.meta_path.insert(0, Guard())
spec = importlib.util.spec_from_file_location('cold_cli', {str(PROVIDER_ROOT / 'zvec-memory/cli.py')!r})
cli = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cli)
path = cli._request_reindex(Path({str(tmp_path / 'cold')!r}))
assert path.is_file()
assert not any(name.endswith('.engine') for name in sys.modules)
print('cold request written')
'''
    result = subprocess.run([sys.executable, '-I', '-B', '-c', code], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == 'cold request written'
