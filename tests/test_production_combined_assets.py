"""Complete managed generation doctor checks must be read-only and fail closed."""
import os
from pathlib import Path

import pytest
from test_cli import load_cli
from test_cli_health_finish import managed_fixture

MEMBERS = ['launcher', 'unit', 'manifest', 'managed-server.mjs',
           'managed-policy.mjs', 'managed-server-LICENSE.txt']


@pytest.mark.parametrize('member', MEMBERS)
@pytest.mark.parametrize('fault', ['missing', 'content', 'mode', 'new', 'dangling-new', 'symlink'])
def test_doctor_complete_generation_drift_retains_all_bytes_and_modes(tmp_path, monkeypatch, member, fault):
    cli = load_cli()
    home, config, engine, values, run, calls = managed_fixture(cli, tmp_path, monkeypatch)
    assets = engine._artifact_set(home, config, node=engine.NODE_BIN, version='0.2.2')
    assert {name for name, _, _, _ in assets} == set(MEMBERS)
    for _, path, text, mode in assets:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        path.chmod(mode)
    path = next(path for name, path, _, _ in assets if name == member)
    if fault == 'missing':
        path.unlink()
    elif fault == 'content':
        path.write_text('retained private operator edit\n')
    elif fault == 'mode':
        path.chmod(0o755 if member == 'launcher' else 0o644)
    elif fault in ('new', 'dangling-new'):
        conflict = path.with_name(path.name + '.new')
        if fault == 'new':
            conflict.write_text('retained private conflict\n')
        else:
            conflict.symlink_to(tmp_path / 'absent')
    else:
        target = path.with_name(path.name + '.retained')
        path.rename(target)
        path.symlink_to(target)
    def snapshot():
        return {str(p): (p.lstat().st_mode, os.readlink(p) if p.is_symlink() else p.read_bytes())
                for p in tmp_path.rglob('*') if p.is_symlink() or p.is_file()}
    before = snapshot()
    ok, detail, managed = cli._service_diagnostics(config, run)
    assert managed is True
    assert ok is False, (member, fault, detail)
    assert 'retained private' not in detail
    assert snapshot() == before
    assert all(c[:3] == ['systemctl', '--user', 'show'] for c in calls)


def test_doctor_complete_generation_is_a_read_only_positive(tmp_path, monkeypatch):
    cli = load_cli()
    home, config, engine, values, run, calls = managed_fixture(cli, tmp_path, monkeypatch)
    assets = engine._artifact_set(home, config, node=engine.NODE_BIN, version='0.2.2')
    for _, path, text, mode in assets:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        path.chmod(mode)
    before = [(p.read_bytes(), p.stat().st_mode, p.stat().st_mtime_ns) for _, p, _, _ in assets]
    assert cli._service_diagnostics(config, run)[0] is True
    assert before == [(p.read_bytes(), p.stat().st_mode, p.stat().st_mtime_ns) for _, p, _, _ in assets]
