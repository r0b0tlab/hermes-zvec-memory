"""Parent reproductions of residual review findings; offline engine only."""
import json
import threading

import pytest

from test_cli import load_cli
from test_provider import ZvecMemoryProvider, make_provider, _mod
from test_runtime_review import ModelEngine, peers


def test_generic_cli_request_preserves_bound_model_obligation(tmp_path):
    from zvec_memory_provider.maintenance import request_rebuild, read_request, request_identity

    identity = {"plugin_schema": 1, "embedding": "local/new", "zg_bin": "zg"}
    request_rebuild(tmp_path, identity=identity)
    cli = load_cli()

    cli._request_reindex(tmp_path)

    assert request_identity(read_request(tmp_path)) == identity, "generic CLI erased model-bound intent"


def test_generic_cli_request_still_creates_work_when_no_bound_request(tmp_path):
    from zvec_memory_provider.maintenance import read_request

    marker = load_cli()._request_reindex(tmp_path)

    assert marker.exists() and read_request(tmp_path) is not None
