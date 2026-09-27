import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "retired_zvec_upgrade", ROOT / "scripts/upgrade.py"
)
assert spec is not None and spec.loader is not None
upgrade = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = upgrade
spec.loader.exec_module(upgrade)


@pytest.mark.parametrize("argv", [
    [], ["--dry-run"], ["--rollback"],
    ["--dry-run", "--rollback"], ["--only", "deploy"],
    ["--rollback", "--backup", "/not-a-backup"],
])
def test_retired_updater_never_runs_or_changes_state(
    tmp_path, monkeypatch, capsys, argv
):
    def forbidden(*args, **kwargs):
        pytest.fail("retired updater attempted a subprocess")

    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "selected-profile"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))

    for relative in (
        ".hermes/config.yaml",
        ".config/systemd/user/hermes-zvec-memory.service",
        "selected-profile/plugins/zvec-memory/__init__.py",
    ):
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"do not change\n")

    def snapshot():
        return {
            str(p.relative_to(tmp_path)): (p.read_bytes(), p.stat().st_mode)
            for p in tmp_path.rglob("*") if p.is_file()
        }

    before = snapshot()
    assert upgrade.main(argv) == 2
    assert "Retired:" in capsys.readouterr().err
    assert snapshot() == before
