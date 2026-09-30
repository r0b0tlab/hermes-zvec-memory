"""Runtime reliability contracts; native boundaries are deliberately offline."""
import json
from pathlib import Path

import pytest

from test_provider import ZvecMemoryProvider, make_provider, _mod


@pytest.fixture
def quiet_provider(tmp_path, monkeypatch):
    p = make_provider(tmp_path)
    monkeypatch.setattr(p, "_maybe_reindex", lambda *a, **k: None)
    try:
        yield p
    finally:
        p.shutdown()


@pytest.mark.parametrize("action", ["replace", "remove"])
def test_authoritative_exact_identity(quiet_provider, action):
    p = quiet_provider
    selected = "Uses the blue interface."
    other = "A quote: " + selected + " Independent fact."
    for content in (selected, other):
        p._apply_mirror("add", "memory", content, {})
    p._apply_mirror(action, "memory", "Uses the green interface.",
                    {"old_text": selected, "previous_content": selected})
    wanted = {other, "Uses the green interface."} if action == "replace" else {other}
    assert {r["content"] for r in p._mirror_state()["records"].values()} == wanted


@pytest.mark.parametrize("previous", [None, "", False, [], {}])
def test_invalid_authoritative_never_falls_back(quiet_provider, previous):
    p = quiet_provider
    p._apply_mirror("add", "user", "old fact", {})
    before = (p._vault / ".mirror-map.json").read_bytes()
    p._mirror_inbox.append(["remove", "user", "", {"old_text": "old fact", "previous_content": previous}])
    with pytest.raises(ValueError, match="previous|identity|content"):
        p._recover_mirrors()
    assert p._mirror_inbox.pending()
    assert (p._vault / ".mirror-map.json").read_bytes() == before
    assert p.prefetch("old fact details") == ""


@pytest.mark.parametrize("count", [1, 2])
def test_legacy_substring_remains_pending(quiet_provider, count):
    p = quiet_provider
    for i in range(count):
        p._apply_mirror("add", "user", f"shared preference {i}", {})
    before = (p._vault / ".mirror-map.json").read_bytes()
    p._mirror_inbox.append(["remove", "user", "", {"old_text": "shared preference"}])
    with pytest.raises(ValueError, match="Legacy selector"):
        p._recover_mirrors()
    assert p._mirror_inbox.pending()
    assert (p._vault / ".mirror-map.json").read_bytes() == before
    assert p.prefetch("shared preference details") == ""


def test_unowned_authoritative_replacement_mirrors_only_new_entry(quiet_provider):
    p = quiet_provider
    explicit = p._write_fact("unowned old", "general", "")
    p._apply_mirror("add", "user", "selector lookalike", {})
    p._apply_mirror("replace", "user", "committed new", {
        "old_text": "selector lookalike", "previous_content": "unowned old"})
    assert explicit.exists()
    assert {r["content"] for r in p._mirror_state()["records"].values()} == {
        "selector lookalike", "committed new"}


def test_exact_legacy_and_wrong_target_controls(quiet_provider):
    p = quiet_provider
    explicit = p._write_fact("same", "general", "")
    p._apply_mirror("add", "user", "same", {})
    p._apply_mirror("remove", "memory", "", {"previous_content": "same"})
    assert len(p._mirror_state()["records"]) == 1
    p._apply_mirror("remove", "user", "", {"previous_content": "missing", "old_text": "same"})
    assert len(p._mirror_state()["records"]) == 1
    p._apply_mirror("remove", "user", "", {"old_text": "same"})
    assert not p._mirror_state()["records"]
    assert explicit.exists()


@pytest.mark.parametrize("directory", ["facts", ".mirror-staging"])
def test_fact_writer_rechecks_root(quiet_provider, tmp_path, directory):
    p = quiet_provider
    root = p._vault / directory
    root.mkdir(exist_ok=True)
    outside = tmp_path / "outside"
    root.rename(outside)
    root.symlink_to(outside, target_is_directory=True)
    before = {f.name: f.read_bytes() for f in outside.iterdir()}
    if directory == "facts":
        assert json.loads(p._handle_store({"content": "refuse me"})).get("error")
    else:
        with pytest.raises(ValueError):
            p._write_fact("refuse me", "general", "", directory=directory)
    assert {f.name: f.read_bytes() for f in outside.iterdir()} == before


def test_fact_ack_requires_flushed_data(quiet_provider, monkeypatch):
    p = quiet_provider
    import os
    observed = []
    real = os.fsync
    def sync(fd):
        # Read via the real open descriptor while it remains owned by writer.
        observed.append((Path(os.readlink(f"/proc/self/fd/{fd}")),
                         Path(f"/proc/self/fd/{fd}").read_text()))
        return real(fd)
    monkeypatch.setattr(_mod.os, "fsync", sync)
    result = json.loads(p._handle_store({"content": "durable body"}))
    assert result["status"] == "stored"
    source = Path(result["path"])
    assert [body for path, body in observed if path == source] == [source.read_text()]
    assert observed[0][0] == source, "source fsync must precede durable refresh admission"


@pytest.mark.parametrize("kind", ["root", "leaf", "dangling", "directory"])
def test_session_refuses_redirected_or_nonregular_target(quiet_provider, tmp_path, kind):
    p = quiet_provider
    sessions = p._vault / "sessions"
    path = sessions / f"{_mod._today()}.md"
    sentinel = p._vault / "facts/sentinel.md"
    sentinel.write_text("keep")
    if kind == "root":
        outside = tmp_path / "outside"
        sessions.rename(outside)
        sessions.symlink_to(outside, target_is_directory=True)
    elif kind == "directory":
        path.mkdir()
    else:
        path.symlink_to(sentinel if kind == "leaf" else tmp_path / "absent.md")
    with pytest.raises((ValueError, OSError)):
        p._append_turn("user", "reply", "session")
    assert sentinel.read_text() == "keep"
    assert not (tmp_path / "absent.md").exists()
    if kind == "root":
        assert list(outside.iterdir()) == []


@pytest.mark.parametrize("exists", [False, True])
def test_session_append_enforces_private_regular_file(quiet_provider, exists):
    p = quiet_provider
    path = p._vault / "sessions" / f"{_mod._today()}.md"
    if exists:
        path.write_text("prior")
        path.chmod(0o644)
    p._append_turn("user", "reply", "session")
    assert path.stat().st_mode & 0o777 == 0o600
    assert "**user:** user" in path.read_text()
    if exists:
        assert path.read_text().startswith("prior")


def test_explicit_settings_read_is_cold(tmp_path, monkeypatch):
    import builtins
    path = tmp_path / "zvec-memory/config.json"
    path.parent.mkdir()
    path.write_text('{"auto_extract": false, "context_chars": 0, "unknown": "keep"}')
    calls = []
    real = builtins.__import__
    def tracked(name, *args, **kwargs):
        if name == "hermes_constants" or name.startswith(("agent.", "hermes_cli.")):
            calls.append(name)
        return real(name, *args, **kwargs)
    monkeypatch.setattr(builtins, "__import__", tracked)
    assert _mod._load_plugin_config(tmp_path) == {
        "auto_extract": False, "context_chars": 0, "unknown": "keep"}
    assert calls == [], "explicit-home settings must not consult host runtime"


@pytest.mark.parametrize("text", ["{}", "{broken", "[]", "null"])
def test_runtime_settings_authority_preserves_bytes(tmp_path, text):
    (tmp_path / "config.yaml").write_text("plugins:\n  zvec-memory:\n    recall_limit: 19\n")
    path = tmp_path / "zvec-memory/config.json"
    path.parent.mkdir()
    path.write_text(text)
    if text == "{}":
        assert _mod._load_plugin_config(tmp_path) == {}
    else:
        with pytest.raises(ValueError):
            _mod._load_plugin_config(tmp_path)
    assert path.read_text() == text


def test_shared_settings_legacy_only_when_absent(tmp_path, monkeypatch):
    from zvec_memory_provider.settings import load_settings
    legacy = {"auto_extract": False, "context_chars": 0, "custom": [1]}
    before = set(tmp_path.rglob("*"))
    assert load_settings(tmp_path, legacy=legacy) == legacy
    assert load_settings(tmp_path, legacy=legacy) is not legacy
    assert set(tmp_path.rglob("*")) == before
    path = tmp_path / "zvec-memory/config.json"
    path.parent.mkdir()
    path.write_text("{}")
    assert load_settings(tmp_path, legacy=legacy) == {}
    original = Path.read_text
    def denied(self, *args, **kwargs):
        if self == path:
            raise PermissionError("fixture")
        return original(self, *args, **kwargs)
    with monkeypatch.context() as m:
        m.setattr(Path, "read_text", denied)
        with pytest.raises(PermissionError):
            load_settings(tmp_path, legacy=legacy)
    assert path.read_text() == "{}"


@pytest.mark.parametrize("bad", [True, False, float("inf"), float("nan"), "Infinity", "NaN", {}, [], None, 10**400])
def test_numeric_settings_have_finite_defaults(tmp_path, bad):
    p = make_provider(tmp_path, recall_limit=bad, context_chars=bad,
                      prefetch_cache_seconds=bad, reindex_min_seconds=bad)
    try:
        assert p._recall_limit() == 5
        assert p._prefetch_cache_ttl() == 120.0
        assert len(p._cap("x" * 3000)) == 2000
        p._last_reindex = _mod.time.monotonic()
        result = json.loads(p._handle_store({"content": "persist once"}))
        assert result["status"] == "stored"
        assert Path(result["path"]).is_file()
        assert p._index_requested and not p._index_running
    finally:
        p.shutdown()


@pytest.mark.parametrize("value,expected", [(0, 1), (-2, 1), ("7", 7), ("7.0", 7), (1.5, 5), ("1.5", 5), (51, 50)])
def test_recall_integer_policy(value, expected):
    assert ZvecMemoryProvider({"recall_limit": value})._recall_limit() == expected


def test_explicit_search_limit_never_evaluates_default(quiet_provider, monkeypatch):
    p = quiet_provider
    def broken():
        raise AssertionError("default should be lazy")
    monkeypatch.setattr(p, "_recall_limit", broken)
    assert "error" not in json.loads(p._handle_search({"query": "fact details", "limit": 3}))


def test_stored_fact_survives_scheduling_failure(quiet_provider, monkeypatch):
    p = quiet_provider
    def fail():
        raise RuntimeError("submission fault")
    monkeypatch.setattr(p, "_maybe_reindex", fail)
    result = json.loads(p._handle_store({"content": "already persisted"}))
    assert result["status"] == "stored"
    assert result["reindex_pending"] is True
    files = list((p._vault / "facts").glob("*.md"))
    assert files == [Path(result["path"])]
    assert files[0].read_text().endswith("\n\nalready persisted\n")


@pytest.mark.parametrize("field,bad", [
    ("schema_version", True), ("schema_version", False), ("schema_version", 0),
    ("schema_version", -1), ("schema_version", 1.0), ("schema_version", "1"), ("schema_version", 2),
    ("last_notification", True), ("last_notification", -1), ("last_notification", 1.5),
    ("last_notification", "1"), ("last_notification", {}), ("last_notification", None),
    ("last_notification", float("inf")), ("last_notification", float("nan")),
])
def test_bad_journal_scalar_preserves_pending_work(quiet_provider, field, bad):
    from test_mirror_validation import journal
    p = quiet_provider
    state = journal(p, count=1)
    state[field] = bad
    p._save_mirror_state(state)
    before = (p._vault / ".mirror-map.json").read_bytes()
    p._mirror_inbox.append(["add", "memory", "queued fact", {}])
    with pytest.raises(ValueError):
        p._recover_mirrors()
    assert (p._vault / ".mirror-map.json").read_bytes() == before
    assert p._mirror_inbox.pending()
    assert (p._vault / "facts/delete-0.md").exists()
    assert not (p._vault / "facts/create-0.md").exists()
    assert p.prefetch("what is queued?") == ""


def test_impossible_watermark_refuses_before_publication(quiet_provider):
    from test_mirror_validation import journal
    p = quiet_provider
    p._mirror_inbox.append(["add", "memory", "queued fact", {}])
    number = p._mirror_inbox.first()[0]
    state = journal(p, count=1)
    state["last_notification"] = number + 99999
    p._save_mirror_state(state)
    before = (p._vault / ".mirror-map.json").read_bytes()
    with pytest.raises(ValueError, match="history|watermark"):
        p._recover_mirrors()
    assert p._mirror_inbox.first()[0] == number
    assert (p._vault / ".mirror-map.json").read_bytes() == before
    assert (p._vault / "facts/delete-0.md").exists()
    assert not (p._vault / "facts/create-0.md").exists()


@pytest.mark.parametrize("rc", [1, 124, 127])
def test_request_survives_failed_index_then_success(tmp_path, monkeypatch, rc):
    p = make_provider(tmp_path)
    marker = p._vault / _mod.REINDEX_REQUEST_FILE
    marker.write_text("request-A")
    monkeypatch.setattr(p, "_run_zg", lambda *a, **k: (rc, "", "fault"))
    try:
        assert p._consume_reindex_request()
        p._maybe_reindex(force=True)
        assert p._index_worker.drain(5)
        assert marker.read_text() == "request-A"
        monkeypatch.setattr(p, "_run_zg", lambda *a, **k: (0, "", ""))
        p._maybe_reindex(force=True)
        assert p._index_worker.drain(5)
        assert not marker.exists()
    finally:
        p.shutdown()


def test_identity_mismatch_dispatches_rebuild_and_model(tmp_path, monkeypatch):
    p = make_provider(tmp_path, embedding="local/old")
    prior = p._engine_state_path().read_bytes()
    p._config["embedding"] = "local/new"
    calls = []
    monkeypatch.setattr(p, "_run_zg", lambda args, timeout: (calls.append(args) or (1, "", "fault")))
    try:
        p._ensure_engine_identity()
        assert p._index_worker.drain(5)
        command = [c for c in calls if c[0] == "index"][-1]
        assert "--rebuild" in command
        assert command[command.index("--embedding") + 1] == "local/new"
        assert p._engine_state_path().read_bytes() == prior
        assert (p._vault / _mod.REINDEX_REQUEST_FILE).exists()
        assert p._recall_token() is None
        calls.clear()
        monkeypatch.setattr(p, "_run_zg", lambda args, timeout: (calls.append(args) or (0, "", "")))
        p._maybe_reindex(force=True)
        assert p._index_worker.drain(5)
        command = [c for c in calls if c[0] == "index"][-1]
        assert "--rebuild" in command and "local/new" in command
        assert p._engine_state()["embedding"] == "local/new"
        assert not (p._vault / _mod.REINDEX_REQUEST_FILE).exists()
    finally:
        p.shutdown()


def test_initial_marker_is_closed_and_explicit_even_with_manifest(tmp_path, monkeypatch):
    seed = make_provider(tmp_path)
    seed.shutdown()
    marker = seed._vault / _mod.REINDEX_REQUEST_FILE
    marker.write_text("request-A")
    calls = []
    p = ZvecMemoryProvider({"vault": str(seed._vault)})
    monkeypatch.setattr(p, "_maybe_reindex", lambda **kwargs: calls.append(kwargs))
    try:
        p.initialize("restart", hermes_home=str(tmp_path))
        assert p._recall_token() is None
        assert not p._mirror_ready
        assert calls[-1] == {"force": True, "extra_args": ["--rebuild", "--embedding", _mod.DEFAULT_EMBEDDING]}
        assert marker.read_text() == "request-A"
        assert p._engine_identity()["embedding"] == _mod.DEFAULT_EMBEDDING
    finally:
        p.shutdown()


def test_queued_rebuild_cannot_be_downgraded(tmp_path, monkeypatch):
    p = make_provider(tmp_path)
    monkeypatch.setattr(p._index_worker, "submit", lambda *args: False)
    try:
        p._maybe_reindex(force=True, extra_args=["--rebuild", "--embedding", "local/old"])
        p._maybe_reindex(force=True)
        p._maybe_reindex(force=True, extra_args=["--embedding", "local/new"])
        assert p._index_extra_args == ["--rebuild", "--embedding", "local/new"]
        assert p._index_requested and not p._index_running
    finally:
        p.shutdown()


def test_old_failure_retains_new_model_and_old_rebuild(tmp_path, monkeypatch):
    p = make_provider(tmp_path)
    def failed(args, timeout):
        p._maybe_reindex(force=True, extra_args=["--embedding", "local/new"])
        return 1, "", "fault"
    monkeypatch.setattr(p, "_run_zg", failed)
    try:
        p._maybe_reindex(force=True, extra_args=["--rebuild", "--embedding", "local/old"])
        assert p._index_worker.drain(5)
        assert p._index_extra_args == ["--rebuild", "--embedding", "local/new"]
    finally:
        p.shutdown()


def test_submit_exception_releases_index_latch(tmp_path, monkeypatch):
    p = make_provider(tmp_path)
    def reject(*args):
        raise RuntimeError("queue fault")
    try:
        with monkeypatch.context() as m:
            m.setattr(p._index_worker, "submit", reject)
            # Scheduling is best effort; the fact/marker remains the truth.
            p._maybe_reindex(force=True, extra_args=["--rebuild"])
        assert not p._index_running and p._index_requested
        assert p._index_extra_args == ["--rebuild"]
        p._maybe_reindex(force=True)
        assert p._index_worker.drain(5)
        assert not p._index_running and not p._index_requested
    finally:
        p.shutdown()


@pytest.mark.parametrize("stage", ["version", "identity-write", "journal-write", "acknowledge"])
def test_bookkeeping_failure_keeps_retry_and_rebuild(tmp_path, monkeypatch, stage):
    p = make_provider(tmp_path)
    marker = p._vault / _mod.REINDEX_REQUEST_FILE
    marker.write_text("request-A")
    state = p._mirror_state()
    state["refresh_required"] = True
    p._save_mirror_state(state)
    prior = p._engine_state_path().read_bytes()
    def broken(*args, **kwargs):
        raise OSError("bookkeeping fault")
    target, name = {
        "version": (p, "_zg_version"), "identity-write": (_mod, "atomic_json_write"),
        "journal-write": (p, "_save_mirror_state"), "acknowledge": (_mod, "acknowledge_request"),
    }[stage]
    try:
        with monkeypatch.context() as m:
            m.setattr(target, name, broken)
            p._maybe_reindex(force=True)
            assert p._index_worker.drain(5)
        assert not p._index_running
        assert p._index_requested and "--rebuild" in p._index_extra_args
        assert marker.read_text() == "request-A"
        if stage in {"version", "identity-write"}:
            assert p._engine_state_path().read_bytes() == prior
        assert not p._mirror_ready
        assert p._recall_token() is None
        p._maybe_reindex(force=True)
        assert p._index_worker.drain(5)
        assert not marker.exists()
        assert not p._index_running and not p._index_requested
        assert p._recall_token() is not None
    finally:
        p.shutdown()


def test_new_marker_keeps_pending_after_old_completion(tmp_path, monkeypatch):
    p = make_provider(tmp_path)
    marker = p._vault / _mod.REINDEX_REQUEST_FILE
    marker.write_text("request-A")
    def native(args, timeout):
        marker.write_text("request-B")
        monkeypatch.setattr(p._index_worker, "submit", lambda *args: False)
        return 0, "", ""
    monkeypatch.setattr(p, "_run_zg", native)
    try:
        p._maybe_reindex(force=True)
        assert p._index_worker.drain(5)
        assert marker.read_text() == "request-B"
        assert p._index_requested and not p._index_running
        assert not p._mirror_ready and "--rebuild" in p._index_extra_args
    finally:
        p.shutdown()


@pytest.mark.parametrize("method", ["_record_engine_state", "_zg_version"])
def test_initialize_defers_failed_identity_adoption(tmp_path, monkeypatch, method):
    vault = tmp_path / "vault"
    (vault / ".zvec-grep").mkdir(parents=True)
    (vault / ".zvec-grep/manifest.json").write_text("{}")
    p = ZvecMemoryProvider({"vault": str(vault)})
    def fault(*args, **kwargs):
        raise OSError("adoption fault")
    try:
        with monkeypatch.context() as m:
            m.setattr(p, method, fault)
            p.initialize("faulted", hermes_home=str(tmp_path))
            assert p._index_worker.drain(5)
            assert not p._mirror_ready and p._index_requested
            assert not p._index_running
            assert (vault / _mod.REINDEX_REQUEST_FILE).exists()
            assert not p._engine_state_path().exists()
        p._maybe_reindex(force=True)
        assert p._index_worker.drain(5)
        assert p._recall_token() is not None
    finally:
        p.shutdown()


def test_request_generations_and_alias_refusal(tmp_path):
    from zvec_memory_provider.maintenance import request_rebuild, read_request, acknowledge_request
    vault = tmp_path / "requests"
    path = request_rebuild(vault)
    first = read_request(vault)
    request_rebuild(vault)
    second = read_request(vault)
    assert second != first
    assert not acknowledge_request(vault, first)
    assert path.read_bytes() == second
    assert acknowledge_request(vault, second)
    assert not path.exists()
    sentinel = vault / "unrelated"
    sentinel.write_text("keep")
    path.symlink_to(sentinel)
    for action in (lambda: read_request(vault), lambda: request_rebuild(vault),
                   lambda: acknowledge_request(vault, b"keep")):
        with pytest.raises(ValueError, match="symlink"):
            action()
    assert sentinel.read_text() == "keep"


def test_older_completion_cannot_record_newer_model_identity(tmp_path, monkeypatch):
    p = make_provider(tmp_path, embedding="local/old")
    calls = []
    def engine(args, timeout):
        calls.append(args)
        if len(calls) == 1:
            p._config["embedding"] = "local/new"
            p._ensure_engine_identity()
            return 0, "", ""
        return 1, "", "new model failed"
    monkeypatch.setattr(p, "_run_zg", engine)
    try:
        p._maybe_reindex(force=True, extra_args=["--rebuild", "--embedding", "local/old"])
        assert p._index_worker.drain(5)
        assert len(calls) == 2
        assert "local/old" in calls[0] and "local/new" in calls[1]
        assert p._engine_state()["embedding"] == "local/old"
        assert p._index_requested and not p._mirror_ready
        assert (p._vault / _mod.REINDEX_REQUEST_FILE).exists()
    finally:
        p.shutdown()


def test_inbox_highwater_survives_acknowledgment(quiet_provider):
    inbox = quiet_provider._mirror_inbox
    assert inbox.high_watermark() == 0
    inbox.append(["add", "user", "one", {}])
    number = inbox.first()[0]
    inbox.acknowledge(number)
    assert not inbox.pending()
    assert inbox.high_watermark() == number
    inbox.append(["add", "user", "two", {}])
    assert inbox.first()[0] > number


def test_legacy_validator_does_not_mutate_input():
    from zvec_memory_provider.journal import validate_journal
    value = {"records": {}, "pending_deletes": []}
    result = validate_journal(value)
    assert "schema_version" not in value and "pending_creates" not in value
    result["pending_creates"].append({"path": "facts/a.md", "staged": ".mirror-staging/a.md"})
    assert validate_journal(value)["pending_creates"] == []


@pytest.mark.parametrize("args", [["--unknown"], ["--embedding"]])
def test_merge_refuses_unknown_internal_semantics(args):
    with pytest.raises(ValueError):
        ZvecMemoryProvider._merge_index_args([], args)


@pytest.mark.parametrize("reject", ["return-false", "raise", "shutdown"])
def test_continuation_rejection_retains_owned_pending_work(tmp_path, monkeypatch, reject):
    p = make_provider(tmp_path)
    submit = p._index_worker.submit
    def denied(*args):
        if reject == "raise":
            raise RuntimeError("reject continuation")
        return False
    def engine(args, timeout):
        p._maybe_reindex(force=True, extra_args=["--rebuild"])
        if reject == "shutdown":
            p._shutdown = True
        monkeypatch.setattr(p._index_worker, "submit", denied)
        return 0, "", ""
    monkeypatch.setattr(p, "_run_zg", engine)
    try:
        p._maybe_reindex(force=True)
        assert p._index_worker.drain(5)
        assert p._index_requested and not p._index_running
        assert p._index_extra_args == ["--rebuild"]
        monkeypatch.setattr(p._index_worker, "submit", submit)
        monkeypatch.setattr(p, "_run_zg", lambda *a, **k: (0, "", ""))
        if reject != "shutdown":
            p._maybe_reindex(force=True)
            assert p._index_worker.drain(5)
            assert not p._index_requested and not p._index_running
    finally:
        p.shutdown()
