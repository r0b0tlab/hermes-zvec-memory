"""Independent-review regressions: command-aware offline engine, no native proof."""
import json
import threading

import pytest

from test_provider import ZvecMemoryProvider, make_provider, _mod


class ModelEngine:
    """Existing indexes retain their model unless explicitly rebuilt."""
    def __init__(self, model="local/old"):
        self.model = model
        self.calls = []

    def __call__(self, args, timeout):
        self.calls.append(list(args))
        if args == ["--version"]:
            return 0, "fixture-1", ""
        if args[0] == "index":
            if "--rebuild" in args and "--embedding" in args:
                self.model = args[args.index("--embedding") + 1]
            return 0, "", ""
        assert args[0] == "query"
        return 0, "indexed with " + self.model, ""


def test_l1_bound_ack_requires_identity_but_legacy_api_is_preserved(tmp_path):
    from zvec_memory_provider.maintenance import request_rebuild, read_request, acknowledge_request
    identity = {"plugin_schema": 1, "embedding": "local/new", "zg_bin": "zg"}
    path = request_rebuild(tmp_path, identity=identity)
    request = read_request(tmp_path)
    assert not acknowledge_request(tmp_path, request)
    assert not acknowledge_request(tmp_path, request, identity={**identity, "embedding": "local/old"})
    assert path.read_bytes() == request
    assert acknowledge_request(tmp_path, request, identity=identity)
    request_rebuild(tmp_path)
    assert acknowledge_request(tmp_path, read_request(tmp_path))


def rebuild(p):
    from zvec_memory_provider.maintenance import request_rebuild
    request_rebuild(p._vault)
    p._maybe_reindex(force=True)
    assert p._index_worker.drain(5)
    assert not (p._vault / _mod.REINDEX_REQUEST_FILE).exists()


@pytest.fixture
def peers(tmp_path, monkeypatch):
    p = make_provider(tmp_path, embedding="local/old")
    q = make_provider(tmp_path, embedding="local/old")
    engine = ModelEngine()
    for provider in (p, q):
        monkeypatch.setattr(provider, "_run_zg", engine)
        monkeypatch.setattr(provider, "is_available", lambda: True)
    try:
        yield p, q, engine
    finally:
        p.shutdown()
        q.shutdown()
