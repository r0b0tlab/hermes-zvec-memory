"""Query dispatch must not bypass managed mutation publication accounting."""
import json

import pytest

from test_runtime_review import peers


@pytest.mark.parametrize("entry", ["prefetch", "search", "queued"])
def test_query_does_not_schedule_untracked_server_refresh(peers, monkeypatch, entry):
    p, q, engine = peers
    requested_refreshes = []
    def server_policy(args, timeout):
        if args[0] == "query":
            # Pinned 0.2.2 client/search-policy.js maps any non-off server
            # refresh to autoUpdate=true. This is a command-policy oracle,
            # not a claim to have exercised native background work.
            refresh = args[args.index("--refresh") + 1]
            if refresh != "off":
                requested_refreshes.append(refresh)
        return engine(args, timeout)
    monkeypatch.setattr(q, "_run_zg", server_policy)
    before = q._engine_state_path().read_bytes()
    if entry == "prefetch":
        assert "local/old" in q.prefetch("which fact was stored?")
    elif entry == "search":
        assert "results" in json.loads(q._handle_search({"query": "which fact was stored?"}))
    else:
        q.queue_prefetch("which fact was stored?")
        assert q._disk_worker.drain(5)
        assert "local/old" in q._cached_prefetch("which fact was stored?")
    assert not requested_refreshes, "query bypasses the serialized mutation/generation publisher"
    assert q._engine_state_path().read_bytes() == before
