"""Test-only product selection; fingerprint before importing provider code."""
from pathlib import Path
import sys

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))
from harness_support import provider_source, product_identity

PROVIDER_ROOT = provider_source(REPO_ROOT)
PRODUCT_IDENTITY = product_identity(PROVIDER_ROOT)
