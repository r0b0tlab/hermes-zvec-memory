"""Filesystem-backed journal validation and containment regressions."""
from collections import Counter
from pathlib import Path

import pytest

from test_provider import make_provider


@pytest.fixture
def provider(tmp_path):
    p = make_provider(tmp_path)
    try:
        yield p
    finally:
        p.shutdown()


def journal(p, count=3):
    facts = p._vault / "facts"
    staging = p._vault / ".mirror-staging"
    staging.mkdir(exist_ok=True)
    records = {}
    creates = []
    deletes = []
    for i in range(count):
        name = f"record-{i}.md"
        (facts / name).write_text(f"fact {i}")
        records[str(i)] = {"target": "user", "content": f"fact {i}", "path": f"facts/{name}"}
        (facts / f"delete-{i}.md").write_text("delete")
        deletes.append(f"facts/delete-{i}.md")
        (staging / f"create-{i}.md").write_text("create")
        creates.append({"path": f"facts/create-{i}.md", "staged": f".mirror-staging/create-{i}.md"})
    state = {"records": records, "pending_deletes": deletes,
             "pending_creates": creates, "refresh_required": True}
    p._save_mirror_state(state)
    return state


def test_validation_resolves_each_root_once_but_every_candidate_afresh(provider, monkeypatch):
    p = provider
    state = journal(p)
    calls = Counter()
    resolve = Path.resolve

    def counted(path, *args, **kwargs):
        calls[path] += 1
        return resolve(path, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", counted)
    for _ in range(2):
        calls.clear()
        assert p._mirror_state() == state
        assert calls[p._vault / "facts"] == 1
        assert calls[p._vault / ".mirror-staging"] == 1
        candidates = [r["path"] for r in state["records"].values()]
        candidates += state["pending_deletes"]
        candidates += [i[k] for i in state["pending_creates"] for k in ("path", "staged")]
        for relative in candidates:
            assert calls[p._vault / relative] == 1


@pytest.mark.parametrize("directory", ["facts", ".mirror-staging"])
@pytest.mark.parametrize("inside", [False, True])
def test_root_symlink_replacement_after_initialization_is_rejected(provider, tmp_path, directory, inside):
    p = provider
    journal(p)
    original = p._vault / directory
    destination = (p._vault if inside else tmp_path) / "retargeted"
    original.rename(destination)
    original.symlink_to(destination, target_is_directory=True)
    before = {f.name: f.read_bytes() for f in destination.iterdir()}
    with pytest.raises(ValueError, match="symlink|escapes"):
        p._recover_mirrors()
    assert {f.name: f.read_bytes() for f in destination.iterdir()} == before


def test_vault_ancestor_retarget_cannot_move_containment_roots_outside(provider, tmp_path):
    p = provider
    journal(p)
    original = p._vault
    destination = tmp_path / "outside-vault"
    original.rename(destination)
    original.symlink_to(destination, target_is_directory=True)
    with pytest.raises(ValueError, match="escapes"):
        p._mirror_state()


@pytest.mark.parametrize("slot", ["record", "delete", "create", "staged"])
@pytest.mark.parametrize("invalid", ["traversal", "suffix", "file-symlink", "ancestor-symlink", "type"])
def test_invalid_later_entry_prevents_all_publication_and_deletion(provider, tmp_path, slot, invalid):
    p = provider
    state = journal(p)
    root = ".mirror-staging" if slot == "staged" else "facts"
    outside = tmp_path / "outside"
    outside.mkdir()
    victim = outside / "victim.md"
    victim.write_text("outside protected")
    if invalid == "traversal":
        bad = "../outside/victim.md"
    elif invalid == "suffix":
        bad = f"{root}/wrong.txt"
    elif invalid == "file-symlink":
        (p._vault / root / "escape.md").symlink_to(victim)
        bad = f"{root}/escape.md"
    elif invalid == "ancestor-symlink":
        (p._vault / root / "escape").symlink_to(outside, target_is_directory=True)
        bad = f"{root}/escape/victim.md"
    else:
        bad = None
    if slot == "record":
        state["records"]["2"]["path"] = bad
    elif slot == "delete":
        state["pending_deletes"][-1] = bad
    else:
        state["pending_creates"][-1]["staged" if slot == "staged" else "path"] = bad
    p._save_mirror_state(state)
    before = (p._vault / ".mirror-map.json").read_bytes()
    with pytest.raises(ValueError):
        p._recover_mirrors()
    assert (p._vault / ".mirror-map.json").read_bytes() == before
    assert not p._mirror_ready
    assert victim.read_text() == "outside protected"
    for i in range(3):
        assert (p._vault / "facts" / f"delete-{i}.md").read_text() == "delete"
        assert (p._vault / ".mirror-staging" / f"create-{i}.md").read_text() == "create"
        assert not (p._vault / "facts" / f"create-{i}.md").exists()


@pytest.mark.parametrize("root", ["facts", ".mirror-staging"])
def test_identical_journal_bytes_do_not_cache_candidate_ancestor_resolution(provider, tmp_path, root):
    p = provider
    state = journal(p)
    nested = p._vault / root / "nested"
    nested.mkdir()
    (nested / "candidate.md").write_text("safe")
    if root == "facts":
        state["records"]["2"]["path"] = "facts/nested/candidate.md"
    else:
        state["pending_creates"][-1]["staged"] = ".mirror-staging/nested/candidate.md"
    p._save_mirror_state(state)
    before = (p._vault / ".mirror-map.json").read_bytes()
    assert p._mirror_state() == state
    outside = tmp_path / "retargeted-nested"
    nested.rename(outside)
    nested.symlink_to(outside, target_is_directory=True)
    assert (p._vault / ".mirror-map.json").read_bytes() == before
    with pytest.raises(ValueError, match="escapes"):
        p._recover_mirrors()
    assert (outside / "candidate.md").read_text() == "safe"
    assert (p._vault / "facts/delete-0.md").exists()
    assert not (p._vault / "facts/create-0.md").exists()


@pytest.mark.parametrize("operation", ["delete", "publish-source", "publish-destination"])
@pytest.mark.parametrize("retarget_root", [False, True])
def test_actual_use_revalidates_after_full_validation(provider, tmp_path, operation, retarget_root):
    p = provider
    state = journal(p, count=1)
    if operation == "delete":
        state["pending_creates"] = []
    else:
        state["pending_deletes"] = []
    p._save_mirror_state(state)
    validated = p._mirror_state()
    directory = ".mirror-staging" if operation == "publish-source" else "facts"
    outside = tmp_path / "outside"
    if retarget_root:
        original = p._vault / directory
        original.rename(outside)
        original.symlink_to(outside, target_is_directory=True)
    else:
        outside.mkdir()
        victim = outside / "victim.md"
        victim.write_text("protected")
        name = "delete-0.md" if operation == "delete" else "create-0.md"
        candidate = p._vault / directory / name
        candidate.unlink(missing_ok=True)
        candidate.symlink_to(victim)
    before = {f.name: f.read_bytes() for f in outside.iterdir()}
    with pytest.raises(ValueError, match="symlink|escapes"):
        p._finish_mirror_deletes(validated)
    assert {f.name: f.read_bytes() for f in outside.iterdir()} == before


def test_valid_journal_publishes_and_deletes_expected_files(provider):
    p = provider
    journal(p)
    p._recover_mirrors()
    state = p._mirror_state()
    assert state["pending_creates"] == state["pending_deletes"] == []
    for i in range(3):
        assert (p._vault / "facts" / f"record-{i}.md").read_text() == f"fact {i}"
        assert (p._vault / "facts" / f"create-{i}.md").read_text() == "create"
        assert not (p._vault / "facts" / f"delete-{i}.md").exists()
    assert list((p._vault / ".mirror-staging").iterdir()) == []
