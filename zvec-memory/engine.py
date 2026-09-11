"""Lay out the local zvec-grep engine runtime, launcher and systemd unit.

Everything in this module is declarative and idempotent, and the module never
touches the network: ``ensure_engine`` re-lays the files it owns only when their
content differs, and ``install_engine`` is the one explicit, user-invoked command
that fetches the pinned npm package.

``post_setup`` is the hook ``hermes memory setup`` calls on the provider package
(``hermes_cli/memory_setup.py::_post_setup_hook``). Because that hook makes this
provider own activation, ``post_setup`` must also persist ``memory.provider``.
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

from .hostio import atomic_json_write

logger = logging.getLogger(__name__)

ENGINE_PACKAGE = "@zvec/zvec-grep"
PINNED_VERSION = "0.2.2"
NODE_BIN = "/usr/bin/node"
NPM_BIN = "/usr/bin/npm"
LAUNCHER_NAME = "zg-default"
UNIT_NAME = "hermes-zvec-memory.service"
PROVIDER_NAME = "zvec-memory"
# Below ~150 the native engine aborts instead of degrading; measured peak during
# warm churn is 138-150 tasks, so declare a headroom floor rather than inheriting
# the user manager's ambient DefaultTasksMax.
TASKS_MAX = 1024
SERVER_URL = "http://127.0.0.1:17999/mcp"
MODEL_CACHE = "~/.cache/hermes-zvec-memory"
RUNTIME_DIR = "~/.local/share/hermes-zvec-memory"
ENGINE_HOME_NAME = "zvec-runtime"
ENTRY_RELATIVE = Path("runtime/node_modules/@zvec/zvec-grep/dist/cli/index.js")
MANIFEST_NAME = "manifest.json"
DEFAULT_EMBEDDING = "local/potion-retrieval-32m"


def _expand(raw, hermes_home: Path) -> Path:
    text = str(raw).replace("$HERMES_HOME", str(hermes_home)).replace("${HERMES_HOME}", str(hermes_home))
    path = Path(text).expanduser()
    return (path if path.is_absolute() else Path(hermes_home) / path).resolve()


def plugin_block(config) -> dict:
    """The plugin's own settings out of the host config (or the block itself)."""
    if not isinstance(config, dict):
        return {}
    block = config.get("plugins")
    if isinstance(block, dict) and isinstance(block.get(PROVIDER_NAME), dict):
        return dict(block[PROVIDER_NAME])
    if any(key in config for key in ("plugins", "memory", "model", "providers")):
        return {}
    return dict(config)


def runtime_root(hermes_home, config=None) -> Path:
    raw = ((config or {}).get("runtime_dir")
           or os.environ.get("HERMES_ZVEC_RUNTIME_DIR") or RUNTIME_DIR)
    return _expand(raw, Path(hermes_home))


def engine_home(hermes_home, config=None) -> Path:
    raw = (config or {}).get("engine_home") or f"$HERMES_HOME/{ENGINE_HOME_NAME}"
    return _expand(raw, Path(hermes_home))


def launcher_path(hermes_home, config=None) -> Path:
    return runtime_root(hermes_home, config) / LAUNCHER_NAME


def entry_path(hermes_home, config=None) -> Path:
    return runtime_root(hermes_home, config) / ENTRY_RELATIVE


def package_json_path(hermes_home, config=None) -> Path:
    return runtime_root(hermes_home, config) / "runtime/node_modules/@zvec/zvec-grep/package.json"


def unit_path() -> Path:
    base = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return base / "systemd/user" / UNIT_NAME


def server_url(config=None) -> str:
    return str((config or {}).get("server_url") or SERVER_URL)


def listen_address(config=None) -> str:
    parts = urlsplit(server_url(config))
    return f"{parts.hostname or '127.0.0.1'}:{parts.port or 17999}"


def model_cache(config=None) -> Path:
    raw = ((config or {}).get("model_cache")
           or os.environ.get("HERMES_ZVEC_MODEL_CACHE") or MODEL_CACHE)
    return _expand(raw, Path.home())


def token_file(hermes_home, config=None) -> Path:
    return engine_home(hermes_home, config) / "server.token"


def installed_version(hermes_home, config=None):
    try:
        return json.loads(package_json_path(hermes_home, config).read_text(encoding="utf-8")).get("version")
    except (OSError, ValueError):
        return None


def launcher_script(hermes_home, config=None) -> str:
    """The engine wrapper: every engine environment variable lives in one file."""
    return (
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n"
        "# Dedicated, authenticated local engine for the default Hermes profile.\n"
        f'export ZVEC_GREP_HOME="{engine_home(hermes_home, config)}"\n'
        f'export ZVEC_GREP_MODEL_CACHE="{model_cache(config)}/models"\n'
        'export ZVEC_GREP_MODE="auto"\n'
        f'export ZVEC_GREP_SERVER_URL="{server_url(config)}"\n'
        'export ZVEC_GREP_SERVER_TOKEN_FILE="$ZVEC_GREP_HOME/server.token"\n'
        "unset ZVEC_GREP_SERVER_TOKEN ZVEC_GREP_API_KEY ZVEC_GREP_ENDPOINT DASHSCOPE_API_KEY QWEN_API_KEY\n"
        f'exec {NODE_BIN} {entry_path(hermes_home, config)} "$@"\n'
    )


def unit_template(hermes_home, config=None) -> str:
    return (
        "[Unit]\n"
        "Description=Local authenticated zvec memory engine for Hermes default profile\n"
        "After=network.target\n"
        "\n"
        "[Service]\n"
        "Type=simple\n"
        f"ExecStart={launcher_path(hermes_home, config)} server run --listen {listen_address(config)}"
        f" --token-file {token_file(hermes_home, config)}\n"
        "Restart=on-failure\n"
        "RestartSec=3\n"
        "TimeoutStopSec=20\n"
        "# The native engine does not degrade when it cannot create threads: it aborts\n"
        '# ("terminate called without an active exception") and recall goes dark. Measured\n'
        "# peak is 138-150 tasks during warm churn, so declare a headroom floor instead of\n"
        "# inheriting the user manager's ambient DefaultTasksMax.\n"
        f"TasksMax={TASKS_MAX}\n"
        "UMask=0077\n"
        "NoNewPrivileges=true\n"
        "\n"
        "[Install]\n"
        "WantedBy=default.target\n"
    )


def _write_if_changed(path: Path, text: str, mode: int = 0o600) -> bool:
    try:
        if path.exists() and path.read_text(encoding="utf-8") == text:
            return False
    except OSError:
        pass
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".zvec-tmp")
    tmp.write_text(text, encoding="utf-8")
    os.chmod(tmp, mode)
    os.replace(tmp, path)
    return True


def ensure_engine(hermes_home, config=None) -> dict:
    """Idempotently lay out launcher + unit for the verified local runtime."""
    hermes_home = Path(hermes_home)
    config = dict(config or {})
    root = runtime_root(hermes_home, config)
    version = installed_version(hermes_home, config)
    if not entry_path(hermes_home, config).is_file():
        return {"status": "missing-engine", "runtime_root": str(root), "version": version,
                "detail": (f"{ENGINE_PACKAGE} runtime not found under {root}/runtime; "
                           f"run 'hermes {PROVIDER_NAME} engine install'")}

    launcher, unit = launcher_path(hermes_home, config), unit_path()
    writes = []
    if _write_if_changed(launcher, launcher_script(hermes_home, config), mode=0o700):
        writes.append("launcher")

    wanted_unit = unit_template(hermes_home, config)
    unit_status = "current"
    if not unit.exists():
        _write_if_changed(unit, wanted_unit)
        unit_status = "created"
        writes.append("unit")
    elif unit.read_text(encoding="utf-8") != wanted_unit:
        # Never clobber a hand-edited unit: leave it and report the conflict.
        _write_if_changed(unit.with_name(unit.name + ".new"), wanted_unit)
        unit_status = "conflict"

    manifest_path = root / MANIFEST_NAME
    manifest = {"package": ENGINE_PACKAGE, "version": version, "node": NODE_BIN,
                "entry": str(entry_path(hermes_home, config)),
                "engine_home": str(engine_home(hermes_home, config)),
                "server_url": server_url(config), "launcher": str(launcher),
                "unit": str(unit),
                # Steady-state, so a second run does not rewrite the manifest.
                "unit_status": "conflict" if unit_status == "conflict" else "generated",
                "tasks_max": TASKS_MAX}
    previous = {}
    if manifest_path.exists():
        try:
            previous = json.loads(manifest_path.read_text(encoding="utf-8"))
        except ValueError:
            previous = {}
    if writes or {k: v for k, v in previous.items() if k != "updated"} != manifest:
        atomic_json_write(manifest_path, {**manifest, "updated": datetime.now(timezone.utc).isoformat()},
                          mode=0o600)
        writes.append("manifest")

    return {"status": "updated" if writes else "present", "version": version,
            "zg_bin": str(launcher), "unit": str(unit), "unit_status": unit_status,
            "runtime_root": str(root), "writes": writes}


def install_engine(hermes_home, config=None, *, version: str = PINNED_VERSION,
                   npm: str = NPM_BIN) -> dict:
    """Fetch the pinned engine package, then lay the runtime out. Explicit only."""
    hermes_home = Path(hermes_home)
    config = dict(config or {})
    prefix = runtime_root(hermes_home, config) / "runtime"
    if shutil.which(npm) is None:
        return {"status": "no-npm", "detail": f"{npm} not found; install Node.js >= 22"}
    prefix.mkdir(parents=True, exist_ok=True)
    proc = subprocess.run([npm, "install", "--prefix", str(prefix), "--no-audit", "--no-fund",
                           "--save-exact", f"{ENGINE_PACKAGE}@{version}"],
                          capture_output=True, text=True, timeout=900)
    if proc.returncode != 0:
        return {"status": "install-failed", "detail": (proc.stderr or proc.stdout).strip()[-400:]}
    found = installed_version(hermes_home, config)
    if found != version:
        return {"status": "version-mismatch", "expected": version, "found": found}
    return ensure_engine(hermes_home, config)


def provider_config_path(hermes_home) -> Path:
    return Path(hermes_home) / PROVIDER_NAME / "config.json"


def _save_host_config(config: dict) -> bool:
    """Persist config.yaml through the host API when there is one."""
    try:
        from hermes_cli.config import save_config

        save_config(config)
        return True
    except Exception:
        logger.warning("zvec-memory: host config API unavailable; not writing config.yaml", exc_info=True)
        return False


def is_host_config(config) -> bool:
    """True when this is the whole Hermes config, not just our own block."""
    return isinstance(config, dict) and any(
        key in config for key in ("memory", "plugins", "model", "providers", "agent", "web"))


def post_setup(hermes_home, config=None) -> dict:
    """`hermes memory setup` entry point: install the engine and own activation.

    The host hook calls this with the whole config dict and then stops, so this
    function is responsible for persisting ``memory.provider``. A caller that
    hands us only our own settings block gets the engine laid out and nothing
    else written.
    """
    hermes_home = Path(hermes_home)
    config = config if isinstance(config, dict) else {}
    owns_config = is_host_config(config)
    block = plugin_block(config) if owns_config else dict(config)
    result = ensure_engine(hermes_home, block)

    path = provider_config_path(hermes_home)
    existing = {}
    if path.is_file():
        try:
            existing = json.loads(path.read_text(encoding="utf-8"))
        except ValueError:
            existing = {}
    merged = {"vault": existing.get("vault") or "$HERMES_HOME/" + PROVIDER_NAME,
              "embedding": existing.get("embedding") or block.get("embedding") or DEFAULT_EMBEDDING,
              "engine_home": existing.get("engine_home") or block.get("engine_home")
              or "$HERMES_HOME/" + ENGINE_HOME_NAME,
              **existing}
    if result.get("zg_bin"):
        merged["zg_bin"] = result["zg_bin"]
    if existing != merged:
        atomic_json_write(path, merged, mode=0o600)

    if owns_config:
        config.setdefault("memory", {})["provider"] = PROVIDER_NAME
        plugins = config.setdefault("plugins", {})
        if isinstance(plugins, dict):
            plugins.setdefault(PROVIDER_NAME, {})["vault"] = merged["vault"]
        _save_host_config(config)
    return {**result, "config": str(path), "provider": PROVIDER_NAME, "owns_config": owns_config}
