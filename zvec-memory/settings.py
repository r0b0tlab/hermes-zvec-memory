"""Cold, profile-explicit settings. Native JSON presence is authoritative."""
import json
import math
from pathlib import Path

from .hostio import cfg_get, read_user_config_raw

DEFAULT_EMBEDDING = "local/potion-retrieval-32m"
PINNED_ENGINE_VERSION = "0.2.2"


def validate_core_config(config, *, engine_version=PINNED_ENGINE_VERSION):
    """Reject unsupported supplied selections, without resolving paths or I/O.

    Missing embedding uses the supported default; explicit null/empty/false is
    not a request for the default. Do not include operator values in errors.
    """
    if "embedding" in config and config["embedding"] != DEFAULT_EMBEDDING:
        raise ValueError("Reduced core supports only the pinned embedding model")
    if engine_version != PINNED_ENGINE_VERSION:
        raise ValueError("Reduced core engine requires the exact pinned version 0.2.2")


def load_settings(home, *, legacy=None):
    """Read without mutation; only absent JSON permits the legacy fallback."""
    home = Path(home)
    path = home / "zvec-memory" / "config.json"
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        value = legacy if legacy is not None else cfg_get(
            read_user_config_raw(home / "config.yaml"),
            "plugins", "zvec-memory", default={})
    else:
        value = json.loads(text)
    if not isinstance(value, dict):
        raise ValueError("zvec-memory configuration must contain an object")
    return dict(value)


def finite_number(value, default, *, minimum=0, maximum=None, integer=False):
    """Finite settings only; invalid types use the caller's safe default."""
    try:
        if isinstance(value, bool):
            raise ValueError("boolean is not numeric configuration")
        number = float(value)
        if not math.isfinite(number) or (integer and not number.is_integer()):
            raise ValueError("non-finite or fractional integer")
        number = int(number) if integer else number
    except (TypeError, ValueError, OverflowError):
        number = default
    number = max(minimum, number)
    return min(maximum, number) if maximum is not None else number
