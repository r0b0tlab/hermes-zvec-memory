"""A pending managed-generation candidate blocks setup without mutation."""
import os

import pytest

from test_engine import config_for, default_node, fake_runtime, load_engine


@pytest.mark.parametrize("operation", ["ensure_engine", "install_engine"])
@pytest.mark.parametrize("member", ["launcher", "unit", "manifest", "managed-server.mjs",
                                    "managed-policy.mjs", "managed-server-LICENSE.txt"])
@pytest.mark.parametrize("kind", ["file", "dangling-symlink", "directory"])
def test_pending_candidate_is_preserved_before_any_artifact_write(
        tmp_path, isolated_home, default_node, operation, member, kind):
    engine = load_engine()
    fake_runtime(tmp_path)
    config = config_for(tmp_path)
    artifacts = engine._artifact_set(isolated_home, config, node=default_node, version="0.2.2")
    target = next(path for name, path, _, _ in artifacts if name == member)
    pending = target.with_name(target.name + ".new")
    pending.parent.mkdir(parents=True, exist_ok=True)
    if kind == "file":
        pending.write_bytes(b"operator candidate\n")
    elif kind == "directory":
        pending.mkdir()
    else:
        pending.symlink_to("absent-operator-target")
    before = pending.lstat()
    with pytest.raises(RuntimeError, match="Managed artifact conflict"):
        getattr(engine, operation)(isolated_home, config)
    after = pending.lstat()
    assert (before.st_dev, before.st_ino, before.st_mode, before.st_size) == (
        after.st_dev, after.st_ino, after.st_mode, after.st_size)
    if kind == "file":
        assert pending.read_bytes() == b"operator candidate\n"
    elif kind == "dangling-symlink":
        assert os.readlink(pending) == "absent-operator-target"
    for _, path, _, _ in artifacts:
        assert not path.exists() and not path.is_symlink()
