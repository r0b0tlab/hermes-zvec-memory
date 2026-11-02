"""Process-persistent gates for managed first/plain/rebuild mutations.

FileEngine models only command selection/model retention, not native semantics.
"""
import json
import multiprocessing
import os
import threading
from pathlib import Path

import pytest

from test_provider import ZvecMemoryProvider, make_provider, _mod
from test_runtime_review import peers
from zvec_memory_provider.maintenance import request_rebuild


class FileEngine:
    def __init__(self, vault, fail=None):
        self.vault = Path(vault)
        self.fail = fail
        self.calls = []

    def __call__(self, args, timeout):
        self.calls.append(list(args))
        root = self.vault / ".zvec-grep"
        model = root / "fixture-built-model.txt"
        if args == ["--version"]:
            return 0, "", ""  # Deliberately unknown, consistently across handles.
        if args[0] == "query":
            return 0, model.read_text() if model.exists() else "local/old", ""
        assert args[0] == "index"
        root.mkdir(exist_ok=True)
        if not (root / "manifest.json").exists() or "--rebuild" in args:
            model.write_text(args[args.index("--embedding") + 1])
        (root / "manifest.json").write_text('{"fixture_only":true}')
        if self.fail == "exit-native":
            os._exit(23)
        if self.fail == "native":
            return 1, "", "fixture native failure after mutation"
        return 0, "", ""


def attempt(vault, kind, fault):
    """Used inside the guarded pytest child too; all engine calls are fake."""
    p = ZvecMemoryProvider({"vault": str(vault), "embedding": "local/old"})
    p._zg_version_cache = ""
    engine = FileEngine(vault, fault)
    p._run_zg = engine
    schedule = p._maybe_reindex
    p._maybe_reindex = lambda **kwargs: None
    p.initialize("fault-owner", hermes_home=str(Path(vault).parent))
    p._maybe_reindex = schedule
    original = p._record_engine_state

    def publication(identity=None):
        if fault in {"after", "exit-after"}:
            original(identity)
        if fault.startswith("exit-"):
            os._exit(23)
        raise OSError("fixture generation publication fault")

    if fault not in {"native", "exit-native"}:
        p._record_engine_state = publication
    if kind == "rebuild":
        request_rebuild(vault)
    if kind == "first":
        p._build_index()
    else:
        p._maybe_reindex(force=True)
    assert p._index_worker.drain(5)
    assert p._index_requested and not p._index_running
    p.shutdown()
    return engine.calls
