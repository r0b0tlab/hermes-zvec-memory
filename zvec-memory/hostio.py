"""Local equivalents of the few host helpers this plugin used to import.

The runtime path (this package, ``inbox``, ``workers``, ``transactions``) must
not depend on Hermes internals that move between releases. These helpers are
small, and ``tests/test_hostio_equivalence.py`` locks them to the host
behaviour so drift shows up as a failing test instead of as a broken provider.

The host modules these mirror are ``hermes_cli.config``, ``tools.registry`` and
``utils``; ``hermes_constants`` remains a documented, supported import and is
deliberately not vendored.
"""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any, Dict, Optional, Union

# Copied from utils.TRUTHY_STRINGS; the equivalence test fails on drift.
TRUTHY_STRINGS = frozenset({"1", "on", "true", "yes"})

# Copied from tools/registry.py (_MAX_TOOL_ERROR_CHARS, _TOOL_ERROR_TRUNCATION_MARKER).
MAX_ERROR_CHARS = 2048
ERROR_TRUNCATION_MARKER = "… [truncated]"


def cfg_get(cfg: Optional[Dict[str, Any]], *keys: str, default: Any = None) -> Any:
    """Traverse nested dict keys safely, returning ``default`` on any miss.

    Explicit ``None`` values are returned as-is (``dict.get`` semantics:
    ``default`` only applies when the key is absent).
    """
    if not isinstance(cfg, dict):
        return default
    node: Any = cfg
    for key in keys:
        if not isinstance(node, dict) or key not in node:
            return default
        node = node[key]
    return node


def is_truthy_value(value: Any, default: bool = False) -> bool:
    """Coerce bool-ish values using the project's shared truthy string set."""
    if value is None:
        return default
    if isinstance(value, str):
        return value.strip().lower() in TRUTHY_STRINGS
    return bool(value)


def tool_error(message: Any, **extra: Any) -> str:
    """``'{"error": "<message>", **extra}'`` with the host's context bound."""
    text = str(message)
    if len(text) > MAX_ERROR_CHARS:
        text = text[:MAX_ERROR_CHARS] + ERROR_TRUNCATION_MARKER
    return json.dumps({"error": text, **extra}, ensure_ascii=False)


def atomic_json_write(path: Union[str, Path], data: Any, *, indent: int = 2,
                      mode: Optional[int] = None, **dump_kwargs: Any) -> None:
    """Write JSON atomically: temp file in the target directory, fsync, replace."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    if mode is None and target.exists():
        mode = target.stat().st_mode & 0o777
    fd, temporary = tempfile.mkstemp(dir=str(target.parent), prefix=f".{target.stem}_",
                                     suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=indent, ensure_ascii=False, **dump_kwargs)
            handle.flush()
            os.fsync(handle.fileno())
        if mode is not None:
            os.chmod(temporary, mode)
        os.replace(temporary, target)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def read_user_config_raw(config_path: Union[str, Path]) -> Dict[str, Any]:
    """Read a user ``config.yaml`` exactly as written; ``{}`` when absent/invalid.

    Only legal for the legacy read-only fallback in ``_load_plugin_config``;
    behavioural reads belong to ``config.json`` under the Hermes home.
    """
    import yaml  # supplied by the host process; never needed on the runtime path

    try:
        with open(config_path, encoding="utf-8") as handle:
            data = yaml.safe_load(handle) or {}
    except FileNotFoundError:
        return {}
    return data if isinstance(data, dict) else {}
