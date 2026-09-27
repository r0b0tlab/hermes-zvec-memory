"""Host provenance is actual selected content, not a borrowed checkout SHA."""
import importlib.util
from pathlib import Path
import subprocess
import pytest

ROOT = Path(__file__).resolve().parents[1]
FILES = ("agent/memory_provider.py", "agent/memory_manager.py",
         "agent/inline_tool_executors.py", "tools/memory_tool_store.py",
         "plugins/memory/__init__.py")


def support():
    path = ROOT / "scripts/harness_support.py"
    assert path.is_file(), "missing explicit installed-snapshot provenance"
    spec = importlib.util.spec_from_file_location("harness_support_test", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def fixture_host(path):
    for name in FILES:
        file = path / name
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text("# synthetic contract\n")
    return path


def test_installed_snapshot_identity_is_content_bound(tmp_path):
    module = support()
    root = fixture_host(tmp_path / "host")
    first = module.host_provenance(root)
    assert first["kind"] == "installed_snapshot"
    assert first == module.host_provenance(root)
    assert "git_sha" not in first
    assert set(first["files"]) == set(FILES)
    (root / FILES[0]).write_text("# changed contract\n")
    assert first["identity"] != module.host_provenance(root)["identity"]


def test_incomplete_snapshot_refuses(tmp_path):
    module = support()
    root = fixture_host(tmp_path / "host")
    (root / FILES[-1]).unlink()
    with pytest.raises(ValueError, match="missing host contract"):
        module.host_provenance(root)


def test_real_git_host_keeps_revision_and_dirty_state(tmp_path):
    module = support()
    root = fixture_host(tmp_path / "host")
    for command in (["init", "-q"], ["add", "."],
                    ["-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
                     "commit", "-qm", "fixture"]):
        subprocess.run(["git", "-C", str(root), *command], check=True,
                       capture_output=True, timeout=10)
    value = module.host_provenance(root)
    assert value["kind"] == "git" and len(value["git_sha"]) == 40
    assert value["dirty"] is False
    (root / FILES[0]).write_text("changed\n")
    assert module.host_provenance(root)["dirty"] is True
