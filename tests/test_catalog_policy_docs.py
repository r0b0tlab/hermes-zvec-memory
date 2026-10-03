"""Keep active submission guidance from reinstating the retired age gate."""
from pathlib import Path
import re

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("name", ["catalog-submission.md", "maintenance.md"])
def test_active_catalog_guidance_uses_current_update_policy(name):
    text = " ".join((ROOT / "docs" / name).read_text().lower().split())
    assert not re.search(r"\b(?:two|2)\s+weeks?\s+old\b", text), (
        "The plugin commit-age admission gate was replaced, not supplemented"
    )
    assert "no self-updating" in text
    assert "exact" in text and "sha" in text
