"""Real filesystem regressions for managed generation ownership and collisions."""
import errno
import os
import stat

import pytest

from test_engine import config_for, default_node, fake_runtime, load_engine


@pytest.mark.parametrize("member", ["launcher", "unit", "manifest", "managed-server.mjs",
                                    "managed-policy.mjs", "managed-server-LICENSE.txt"])
@pytest.mark.parametrize("kind", ["file", "hardlink", "symlink", "dangling", "directory"])
def test_old_temp_conflict_preserves_all_members(tmp_path, isolated_home, default_node, member, kind):
    engine = load_engine()
    fake_runtime(tmp_path)
    config = config_for(tmp_path)
    artifacts = engine._artifact_set(isolated_home, config, node=default_node, version="0.2.2")
    target = next(path for name, path, _, _ in artifacts if name == member)
    pending = target.with_name(target.name + ".zvec-tmp")
    pending.parent.mkdir(parents=True, exist_ok=True)
    outside = tmp_path / "operator"
    outside.write_bytes(b"operator bytes\n")
    outside.chmod(0o640)
    if kind == "file":
        pending.write_bytes(b"pending bytes\n")
        pending.chmod(0o640)
    elif kind == "hardlink":
        os.link(outside, pending)
    elif kind == "symlink":
        pending.symlink_to(outside)
    elif kind == "dangling":
        pending.symlink_to("missing")
    else:
        pending.mkdir()
    before = pending.lstat()
    contents = pending.read_bytes() if kind in ("file", "hardlink") else None
    with pytest.raises(RuntimeError, match="Managed artifact conflict"):
        engine.ensure_engine(isolated_home, config)
    after = pending.lstat()
    assert (after.st_dev, after.st_ino, after.st_mode, after.st_size) == (
        before.st_dev, before.st_ino, before.st_mode, before.st_size)
    if contents is not None:
        assert pending.read_bytes() == contents
    assert outside.read_bytes() == b"operator bytes\n"
    assert stat.S_IMODE(outside.stat().st_mode) == 0o640
    for _, path, _, _ in artifacts:
        assert not path.exists() and not path.is_symlink()


def test_racing_final_is_not_replaced_and_prior_siblings_stay_unchanged(
        tmp_path, isolated_home, default_node, monkeypatch):
    engine = load_engine()
    fake_runtime(tmp_path)
    config = config_for(tmp_path)
    engine.ensure_engine(isolated_home, config)
    assets = engine._artifact_set(isolated_home, config, node=default_node, version="0.2.2")
    _, target, _, _ = assets[-1]
    target.unlink()
    siblings = {p: (p.read_bytes(), p.stat().st_mode, p.stat().st_mtime_ns)
                for _, p, _, _ in assets[:-1]}
    link = engine._link_unnamed
    def race(fd, directory_fd, name):
        if name == target.name:
            target.write_bytes(b"concurrent bytes\n")
            target.chmod(0o640)
        return link(fd, directory_fd, name)
    monkeypatch.setattr(engine, "_link_unnamed", race)
    with pytest.raises(FileExistsError):
        engine.ensure_engine(isolated_home, config)
    assert target.read_bytes() == b"concurrent bytes\n"
    assert stat.S_IMODE(target.stat().st_mode) == 0o640
    assert {p: (p.read_bytes(), p.stat().st_mode, p.stat().st_mtime_ns) for p in siblings} == siblings
    assert not target.with_name(target.name + ".zvec-tmp").exists()


@pytest.mark.parametrize("failure", ["write", "file-fsync", "link", "close"])
def test_failures_leave_no_named_temp_and_preserve_foreign_file(tmp_path, monkeypatch, failure):
    engine = load_engine()
    tmp_path = tmp_path / "artifacts"
    tmp_path.mkdir()
    target = tmp_path / "artifact"
    foreign = tmp_path / "artifact.zvec-tmp"
    foreign.write_bytes(b"keep me")
    foreign.chmod(0o640)
    old_close = os.close
    def fail(*args, **kwargs):
        raise OSError(errno.EIO, "injected " + failure)
    if failure == "write":
        monkeypatch.setattr(os, "write", fail)
    elif failure == "file-fsync":
        monkeypatch.setattr(os, "fsync", fail)
    elif failure == "link":
        monkeypatch.setattr(engine, "_link_unnamed", fail)
    else:
        def delayed_close(fd):
            old_close(fd)
            if stat.S_ISREG(os.stat(target).st_mode):
                raise OSError(errno.EIO, "injected close")
        monkeypatch.setattr(os, "close", delayed_close)
    with pytest.raises(OSError, match="injected"):
        engine._write_if_changed(target, "generated", 0o700)
    assert foreign.read_bytes() == b"keep me"
    assert stat.S_IMODE(foreign.stat().st_mode) == 0o640
    if failure != "close":
        assert not target.exists()
    else:
        # Publication has already happened; never unlink a name after a close
        # error, because another actor may have substituted it.
        assert target.read_bytes() == b"generated"
    assert set(p.name for p in tmp_path.iterdir()) <= {"artifact", "artifact.zvec-tmp"}


@pytest.mark.parametrize("kind", ["file", "hardlink", "symlink", "dangling", "directory"])
def test_real_linkat_refuses_every_existing_target_kind(tmp_path, kind):
    engine = load_engine()
    folder = tmp_path / "targets"
    folder.mkdir()
    target, outside = folder / "artifact", folder / "outside"
    outside.write_bytes(b"outside bytes")
    outside.chmod(0o640)
    if kind == "file":
        target.write_bytes(b"target bytes")
        target.chmod(0o640)
    elif kind == "hardlink":
        os.link(outside, target)
    elif kind == "symlink":
        target.symlink_to(outside)
    elif kind == "dangling":
        target.symlink_to("absent")
    else:
        target.mkdir()
    before = target.lstat()
    with pytest.raises(FileExistsError):
        engine._write_if_changed(target, "must not install")
    after = target.lstat()
    assert (after.st_dev, after.st_ino, after.st_mode, after.st_size) == (
        before.st_dev, before.st_ino, before.st_mode, before.st_size)
    if kind == "file":
        assert target.read_bytes() == b"target bytes"
    assert outside.read_bytes() == b"outside bytes"
    assert stat.S_IMODE(outside.stat().st_mode) == 0o640
    assert {p.name for p in folder.iterdir()} == {"artifact", "outside"}


def test_directory_fsync_failure_is_reported_after_publication(tmp_path, monkeypatch):
    engine = load_engine()
    target = tmp_path / "artifact"
    fsync = os.fsync
    def fail_directory(fd):
        if stat.S_ISDIR(os.fstat(fd).st_mode):
            raise OSError(errno.EIO, "directory fsync")
        return fsync(fd)
    monkeypatch.setattr(os, "fsync", fail_directory)
    with pytest.raises(OSError, match="directory fsync"):
        engine._write_if_changed(target, "published")
    assert target.read_bytes() == b"published"
    assert not target.with_name("artifact.zvec-tmp").exists()


def test_concurrent_old_temp_is_preserved_and_reported(tmp_path, isolated_home, default_node, monkeypatch):
    engine = load_engine()
    fake_runtime(tmp_path)
    config = config_for(tmp_path)
    engine.ensure_engine(isolated_home, config)
    assets = engine._artifact_set(isolated_home, config, node=default_node, version="0.2.2")
    target = assets[-1][1]
    target.unlink()
    siblings = {p: (p.read_bytes(), p.stat().st_mode, p.stat().st_mtime_ns)
                for _, p, _, _ in assets[:-1]}
    temp = target.with_name(target.name + ".zvec-tmp")
    link = engine._link_unnamed
    def race(fd, directory_fd, name):
        temp.write_bytes(b"concurrent temp")
        temp.chmod(0o640)
        return link(fd, directory_fd, name)
    monkeypatch.setattr(engine, "_link_unnamed", race)
    with pytest.raises(RuntimeError, match="artifact conflict"):
        engine.ensure_engine(isolated_home, config)
    assert temp.read_bytes() == b"concurrent temp"
    assert stat.S_IMODE(temp.stat().st_mode) == 0o640
    assert {p: (p.read_bytes(), p.stat().st_mode, p.stat().st_mtime_ns) for p in siblings} == siblings


def test_early_failure_keeps_original_error_when_close_also_fails(tmp_path, monkeypatch):
    engine = load_engine()
    target = tmp_path / "artifact"
    close = os.close
    def failed_write(*args):
        raise OSError(errno.ENOSPC, "primary write")
    def failed_close(fd):
        close(fd)
        raise OSError(errno.EIO, "secondary close")
    with monkeypatch.context() as patch:
        patch.setattr(os, "write", failed_write)
        patch.setattr(os, "close", failed_close)
        with pytest.raises(OSError, match="primary write") as caught:
            engine._write_if_changed(target, "content")
    assert len(caught.value.__notes__) == 2
    assert all("secondary close" in note for note in caught.value.__notes__)
    assert not target.exists()


@pytest.mark.parametrize("failed_kinds", [("file",), ("directory",), ("file", "directory")])
@pytest.mark.parametrize("in_outer_except", [False, True])
def test_outer_exception_does_not_suppress_delayed_close_failures(
        tmp_path, monkeypatch, failed_kinds, in_outer_except):
    engine = load_engine()
    tmp_path = tmp_path / "artifacts"
    tmp_path.mkdir()
    target = tmp_path / "artifact"
    foreign = tmp_path / "foreign"
    foreign.write_bytes(b"foreign")
    real_close = os.close
    witness_fd = os.open(foreign, os.O_RDONLY)
    reused = {}
    calls = []
    errors = {kind: OSError(code, f"{kind} close") for kind, code in
              (("file", errno.ENOSPC), ("directory", errno.EIO))}

    def delayed_close(fd):
        kind = "directory" if stat.S_ISDIR(os.fstat(fd).st_mode) else "file"
        calls.append((kind, fd))
        real_close(fd)
        if kind in failed_kinds:
            # Model a delayed error after release and immediate descriptor reuse.
            os.dup2(witness_fd, fd)
            reused[kind] = fd
            raise errors[kind]

    outer = RuntimeError("unrelated caller error")
    try:
        with monkeypatch.context() as patch:
            patch.setattr(os, "close", delayed_close)
            if in_outer_except:
                try:
                    raise outer
                except RuntimeError:
                    with pytest.raises(OSError) as caught:
                        engine._write_if_changed(target, "published", 0o700)
            else:
                with pytest.raises(OSError) as caught:
                    engine._write_if_changed(target, "published", 0o700)
        assert caught.value is errors[failed_kinds[0]]
        assert caught.value.errno == errors[failed_kinds[0]].errno
        assert getattr(outer, "__notes__", []) == []
        if len(failed_kinds) == 2:
            assert len(caught.value.__notes__) == 1
            assert "directory close" in caught.value.__notes__[0]
        assert [kind for kind, _ in calls] == ["file", "directory"]
        assert len({fd for _, fd in calls}) == 2
        for fd in reused.values():
            assert os.read(fd, 7) == b"foreign"
            os.lseek(witness_fd, 0, os.SEEK_SET)
        assert target.read_bytes() == b"published"
        assert stat.S_IMODE(target.stat().st_mode) == 0o700
        assert {p.name for p in tmp_path.iterdir()} == {"artifact", "foreign"}
    finally:
        for fd in reused.values():
            real_close(fd)
        real_close(witness_fd)


def test_writer_primary_survives_both_delayed_close_errors_inside_outer_except(tmp_path, monkeypatch):
    engine = load_engine()
    tmp_path = tmp_path / "artifacts"
    tmp_path.mkdir()
    target = tmp_path / "artifact"
    foreign = tmp_path / "foreign"
    foreign.write_bytes(b"foreign")
    real_close = os.close
    witness_fd = os.open(foreign, os.O_RDONLY)
    primary = OSError(errno.ENOSPC, "primary write")
    errors = [OSError(errno.EIO, "file close"), OSError(errno.EBADF, "directory close")]
    closed, reused = [], []

    def failed_write(*args):
        raise primary

    def delayed_close(fd):
        kind = "directory" if stat.S_ISDIR(os.fstat(fd).st_mode) else "file"
        closed.append((kind, fd))
        real_close(fd)
        os.dup2(witness_fd, fd)
        reused.append(fd)
        raise errors[len(closed) - 1]

    outer = RuntimeError("unrelated caller error")
    try:
        with monkeypatch.context() as patch:
            patch.setattr(os, "write", failed_write)
            patch.setattr(os, "close", delayed_close)
            try:
                raise outer
            except RuntimeError:
                with pytest.raises(OSError) as caught:
                    engine._write_if_changed(target, "content")
        assert caught.value is primary and caught.value.errno == errno.ENOSPC
        assert len(primary.__notes__) == 2
        assert "file close" in primary.__notes__[0]
        assert "directory close" in primary.__notes__[1]
        assert getattr(outer, "__notes__", []) == []
        assert [kind for kind, _ in closed] == ["file", "directory"]
        assert len({fd for _, fd in closed}) == 2
        for fd in reused:
            assert os.read(fd, 7) == b"foreign"
            os.lseek(witness_fd, 0, os.SEEK_SET)
        assert not target.exists()
        assert {p.name for p in tmp_path.iterdir()} == {"foreign"}
    finally:
        for fd in reused:
            real_close(fd)
        real_close(witness_fd)


def test_tmpfile_open_errno_survives_directory_close_and_no_fallback(tmp_path, monkeypatch):
    engine = load_engine()
    folder = tmp_path / "artifacts"
    folder.mkdir()
    target = folder / "artifact"
    foreign = tmp_path / "foreign"
    foreign.write_bytes(b"foreign")
    real_open, real_close = os.open, os.close
    witness_fd = real_open(foreign, os.O_RDONLY)
    primary = OSError(errno.EOPNOTSUPP, "unnamed files unsupported")
    opens, closes, reused = [], [], []

    def fail_tmpfile(path, flags, *args, **kwargs):
        opens.append((path, flags, kwargs.get("dir_fd")))
        if flags & os.O_TMPFILE == os.O_TMPFILE:
            raise primary
        return real_open(path, flags, *args, **kwargs)

    def delayed_close(fd):
        closes.append(fd)
        real_close(fd)
        os.dup2(witness_fd, fd)
        reused.append(fd)
        raise OSError(errno.EIO, "directory close")

    try:
        with monkeypatch.context() as patch:
            patch.setattr(os, "open", fail_tmpfile)
            patch.setattr(os, "close", delayed_close)
            with pytest.raises(OSError) as caught:
                engine._write_if_changed(target, "content")
        assert caught.value is primary and caught.value.errno == errno.EOPNOTSUPP
        assert len(primary.__notes__) == 1 and "directory close" in primary.__notes__[0]
        assert len(opens) == 2 and opens[1][0] == "."
        assert opens[1][2] == closes[0] and len(closes) == 1
        assert os.read(reused[0], 7) == b"foreign"
        assert list(folder.iterdir()) == []
    finally:
        for fd in reused:
            real_close(fd)
        real_close(witness_fd)


def test_success_is_durable_and_no_named_temp(tmp_path, monkeypatch):
    engine = load_engine()
    tmp_path = tmp_path / "artifacts"
    tmp_path.mkdir()
    real_fsync = os.fsync
    synced = []
    def fsync(fd):
        synced.append(stat.S_IFMT(os.fstat(fd).st_mode))
        return real_fsync(fd)
    monkeypatch.setattr(os, "fsync", fsync)
    target = tmp_path / "generated"
    assert engine._write_if_changed(target, "actual content", 0o700)
    assert target.read_bytes() == b"actual content"
    assert stat.S_IMODE(target.stat().st_mode) == 0o700
    assert synced == [stat.S_IFREG, stat.S_IFDIR]
    assert list(tmp_path.iterdir()) == [target]
