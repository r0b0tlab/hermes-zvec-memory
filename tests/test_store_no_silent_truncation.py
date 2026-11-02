"""Explicit memory-store success must never mean partial content was stored."""
import json
from pathlib import Path

import pytest

from test_provider import ZvecMemoryProvider, _mod, make_provider


def test_store_rejects_oversize_before_creating_fact(tmp_path):
    provider = make_provider(tmp_path)
    try:
        facts = tmp_path / "vault" / "facts"
        before = set(facts.glob("*.md")) if facts.exists() else set()
        result = json.loads(provider.handle_tool_call(
            "memory_store", {"content": "A" * _mod.MAX_STORED_CHARS + "UNIQUE_TAIL"}
        ))
        assert "error" in result
        assert "2000" in str(result["error"])
        assert (set(facts.glob("*.md")) if facts.exists() else set()) == before
    finally:
        provider.shutdown()


@pytest.mark.parametrize("content", [
    "x" * 2000,
    "  leading and trailing whitespace \n\t",
    "a" * 1997 + "🦊雪!",
])
def test_store_success_retains_exact_content(tmp_path, content):
    provider = make_provider(tmp_path)
    try:
        result = json.loads(provider.handle_tool_call("memory_store", {"content": content}))
        assert result["status"] == "stored"
        data = Path(result["path"]).read_text(encoding="utf-8")
        assert data.endswith("\n\n" + content + "\n")
    finally:
        provider.shutdown()


def test_whitespace_only_is_refused(tmp_path):
    provider = make_provider(tmp_path)
    try:
        assert "error" in json.loads(provider.handle_tool_call("memory_store", {"content": "  \n\t"}))
    finally:
        provider.shutdown()
