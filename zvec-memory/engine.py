"""Lay out the local zvec-grep engine runtime, launcher and systemd unit.

``ensure_engine`` admits a verified default-profile runtime and a matching
complete artifact generation, creating only missing members. Operator edits
are refused before mutation. ``install_engine`` is the one explicit,
user-invoked command that fetches the pinned npm package; setup never fetches.

``post_setup`` is the hook ``hermes memory setup`` calls on the provider package
(``hermes_cli/memory_setup.py::_post_setup_hook``). Because that hook makes this
provider own activation, ``post_setup`` must also persist ``memory.provider``.
"""
from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
from pathlib import Path
from urllib.parse import urlsplit

from .hostio import atomic_json_write

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
    if is_host_config(config):
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
        metadata = json.loads(package_json_path(hermes_home, config).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(metadata, dict) or metadata.get("name") != ENGINE_PACKAGE:
        return None
    version = metadata.get("version")
    if not isinstance(version, str) or not version or version != version.strip():
        return None
    return version


def launcher_script(hermes_home, config=None, *, node=None) -> str:
    """Render only; managed callers supply the admitted absolute interpreter."""
    node = NODE_BIN if node is None else node
    return (
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n"
        "# Dedicated, authenticated local engine for the default Hermes profile.\n"
        f'export ZVEC_GREP_HOME={shlex.quote(str(engine_home(hermes_home, config)))}\n'
        f'export ZVEC_GREP_MODEL_CACHE={shlex.quote(str(model_cache(config) / "models"))}\n'
        'export ZVEC_GREP_MODE="auto"\n'
        f'export ZVEC_GREP_SERVER_URL={shlex.quote(server_url(config))}\n'
        'export ZVEC_GREP_SERVER_TOKEN_FILE="$ZVEC_GREP_HOME/server.token"\n'
        "unset ZVEC_GREP_SERVER_TOKEN ZVEC_GREP_API_KEY ZVEC_GREP_ENDPOINT DASHSCOPE_API_KEY QWEN_API_KEY\n"
        f'exec {shlex.quote(str(node))} {shlex.quote(str(entry_path(hermes_home, config)))} "$@"\n'
    )


def _systemd_arg(value) -> str:
    """Quote the supported systemd grammar, without expansion or escapes."""
    text = str(value)
    if any(char in "\"'\\$%" or ord(char) < 32 or ord(char) == 127 for char in text):
        raise ValueError("Unsupported character in systemd argument")
    return f'"{text}"'


def unit_template(hermes_home, config=None) -> str:
    return (
        "[Unit]\n"
        "Description=Local authenticated zvec memory engine for Hermes default profile\n"
        "After=network.target\n"
        "\n"
        "[Service]\n"
        "Type=simple\n"
        f"ExecStart={_systemd_arg(launcher_path(hermes_home, config))} server run"
        f" --listen {_systemd_arg(listen_address(config))}"
        f" --token-file {_systemd_arg(token_file(hermes_home, config))}\n"
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


def _require_managed_scope(hermes_home):
    home = Path(hermes_home).expanduser().resolve()
    if home != (Path.home() / ".hermes").resolve():
        raise ValueError("Managed engine setup supports only the default ~/.hermes profile; no artifacts changed")


def _require_managed_executable(hermes_home, config):
    custom = config.get("zg_bin")
    if custom and custom != str(launcher_path(hermes_home, config)):
        raise RuntimeError("Custom zg_bin selected; managed setup will not replace it")


def _artifact_set(hermes_home, config, *, node, version):
    """Render the complete default-profile generation before any mutation."""
    launcher, unit = launcher_path(hermes_home, config), unit_path()
    manifest = {"package": ENGINE_PACKAGE, "version": version, "node": str(node),
                "entry": str(entry_path(hermes_home, config)),
                "engine_home": str(engine_home(hermes_home, config)),
                "server_url": server_url(config), "launcher": str(launcher),
                "unit": str(unit), "unit_status": "generated", "tasks_max": TASKS_MAX}
    return [("launcher", launcher, launcher_script(hermes_home, config, node=node), 0o700),
            ("unit", unit, unit_template(hermes_home, config), 0o600),
            ("manifest", runtime_root(hermes_home, config) / MANIFEST_NAME,
             json.dumps(manifest, indent=2, ensure_ascii=False), 0o600)]


def _preflight_artifacts(artifacts):
    """Refuse edits, unsafe file kinds and permissions across the entire set."""
    for name, path, text, mode in artifacts:
        try:
            if path.is_symlink():
                raise ValueError("symlink")
            if not path.exists():
                continue
            if not path.is_file() or path.stat().st_mode & 0o7777 != mode:
                raise ValueError("file kind or mode")
            current = path.read_text(encoding="utf-8")
            if name == "manifest":
                # Older generated manifests had a volatile timestamp. Admit a
                # matching generation without rewriting its existing bytes.
                data = json.loads(current)
                if not isinstance(data, dict):
                    raise ValueError("manifest object")
                if "updated" in data:
                    if not isinstance(data["updated"], str):
                        raise ValueError("manifest timestamp")
                    data.pop("updated")
                matches = data == json.loads(text)
            else:
                matches = current == text
            if not matches:
                raise ValueError("content")
        except (OSError, ValueError) as exc:
            raise RuntimeError(f"Managed artifact conflict: {name} at {path}") from exc


def ensure_engine(hermes_home, config=None, *, expected_version: str = PINNED_VERSION) -> dict:
    """Idempotently lay out launcher + unit for the verified local runtime."""
    _require_managed_scope(hermes_home)
    hermes_home = Path(hermes_home)
    config = dict(config or {})
    _require_managed_executable(hermes_home, config)
    unit_template(hermes_home, config)
    root = runtime_root(hermes_home, config)
    version = installed_version(hermes_home, config)
    if not entry_path(hermes_home, config).is_file():
        return {"status": "missing-engine", "runtime_root": str(root), "version": version,
                "detail": (f"{ENGINE_PACKAGE} runtime not found under {root}/runtime; "
                           f"run 'hermes {PROVIDER_NAME} engine install'")}
    if version is None:
        return {"status": "invalid-engine-metadata", "runtime_root": str(root),
                "detail": "Engine package metadata must identify the expected package and a version"}
    if version != expected_version:
        return {"status": "version-mismatch", "expected": expected_version, "found": version,
                "runtime_root": str(root), "detail": "Installed engine does not match the requested version"}

    node = verified_node(config)
    try:
        proc = subprocess.run([str(node), str(entry_path(hermes_home, config)), "--version"],
                              stdin=subprocess.DEVNULL, capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=10)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"status": "runtime-verification-failed", "runtime_root": str(root),
                "detail": "Engine version could not execute: " + type(exc).__name__}
    found = proc.stdout.strip()
    if proc.returncode or found != version:
        return {"status": "executable-version-mismatch", "expected": version, "found": found,
                "runtime_root": str(root), "detail": "Engine executable does not match package metadata"}

    artifacts = _artifact_set(hermes_home, config, node=node, version=version)
    _preflight_artifacts(artifacts)
    launcher, unit = launcher_path(hermes_home, config), unit_path()
    unit_status = "current" if unit.exists() else "created"
    writes = []
    for name, path, text, mode in artifacts:
        if not path.exists():
            _write_if_changed(path, text, mode=mode)
            writes.append(name)

    # Read every member back; a successful write call is not readiness.
    if any(not path.is_file() for _, path, _, _ in artifacts):
        raise RuntimeError("Managed artifact readback failed: missing member")
    _preflight_artifacts(artifacts)
    return {"status": "updated" if writes else "present", "version": version,
            "zg_bin": str(launcher), "unit": str(unit), "unit_status": unit_status,
            "runtime_root": str(root), "writes": writes}


def verified_node(config) -> Path:
    """Select an executable absolute interpreter, never an ambient fallback."""
    raw = config.get("node_bin") or shutil.which("node")
    if not raw:
        raise ValueError("Node.js >=22 is required")
    node = Path(raw).expanduser()
    if not node.is_absolute() or not node.is_file() or not os.access(node, os.X_OK):
        raise ValueError("node_bin must be an executable absolute path")
    node = node.resolve()
    try:
        proc = subprocess.run([str(node), "--version"], stdin=subprocess.DEVNULL,
                              capture_output=True, text=True, encoding="utf-8", errors="replace",
                              timeout=10)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ValueError("Node.js >=22 verification failed: " + type(exc).__name__) from None
    match = re.fullmatch(r"v([0-9]+)\.[0-9]+\.[0-9]+", proc.stdout.strip())
    if proc.returncode or not match or int(match.group(1)) < 22:
        raise ValueError("Node.js >=22 verification failed")
    return node


def install_engine(hermes_home, config=None, *, version: str = PINNED_VERSION,
                   npm: str | None = None) -> dict:
    """Fetch the pinned engine package, then lay the runtime out. Explicit only."""
    _require_managed_scope(hermes_home)
    hermes_home = Path(hermes_home)
    config = dict(config or {})
    _require_managed_executable(hermes_home, config)
    # Validate systemd-bound values before npm or any filesystem mutation.
    unit_template(hermes_home, config)
    # Only a literal package version may reach npm: no tags, ranges, or token
    # repair. The engine pin is separate from the plugin's release version.
    exact_version = (r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)"
                     r"(?:-[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?"
                     r"(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?")
    if not isinstance(version, str) or re.fullmatch(exact_version, version) is None:
        raise ValueError("An exact engine package version is required")
    package = package_json_path(hermes_home, config)
    if package.parent.exists() or package.parent.is_symlink():
        found = installed_version(hermes_home, config)
        if found is None:
            return {"status": "invalid-engine-metadata",
                    "detail": "Existing engine package metadata is invalid; refusing replacement"}
        if found != version:
            return {"status": "migration-required", "expected": version, "found": found,
                    "detail": "Changing an installed engine version requires separately approved migration"}
        return ensure_engine(hermes_home, config, expected_version=version)
    node = verified_node(config)
    _preflight_artifacts(_artifact_set(hermes_home, config, node=node, version=version))
    prefix = runtime_root(hermes_home, config) / "runtime"
    npm_bin = shutil.which(npm or "npm")
    if npm_bin is None and npm is None:
        npm_bin = shutil.which(NPM_BIN)
    if npm_bin is None:
        return {"status": "no-npm", "detail": "npm not found; install Node.js >= 22"}
    npm_bin = str(Path(npm_bin).resolve())
    prefix.mkdir(parents=True, exist_ok=True)
    # Execute npm's CLI with the exact admitted interpreter; PATH also pins its
    # ordinary env-node children. No later resolver may switch this installation.
    environment = {**os.environ, "PATH": str(node.parent) + os.pathsep + os.environ.get("PATH", ""),
                   "SHARP_IGNORE_GLOBAL_LIBVIPS": os.environ.get("SHARP_IGNORE_GLOBAL_LIBVIPS", "1")}
    proc = subprocess.run([str(node), npm_bin, "install", "--prefix", str(prefix), "--no-audit", "--no-fund",
                           "--save-exact", f"{ENGINE_PACKAGE}@{version}"],
                          capture_output=True, text=True, timeout=900, env=environment)
    if proc.returncode != 0:
        return {"status": "install-failed", "detail": (proc.stderr or proc.stdout).strip()[-400:]}
    found = installed_version(hermes_home, config)
    if found != version:
        return {"status": "version-mismatch", "expected": version, "found": found}
    config["node_bin"] = str(node)
    return ensure_engine(hermes_home, config, expected_version=version)


def provider_config_path(hermes_home) -> Path:
    return Path(hermes_home) / PROVIDER_NAME / "config.json"


def _host_config_destination(hermes_home):
    """Setup alone may use the host's scoped configuration API, never runtime."""
    from hermes_constants import get_hermes_home
    from hermes_cli.config import get_config_path

    home = Path(hermes_home).resolve()
    if Path(get_hermes_home()).resolve() != home:
        raise RuntimeError("Refusing activation in a different active profile")
    path = Path(get_config_path())
    if path.resolve() != home / "config.yaml":
        raise RuntimeError("Host config destination does not match setup home")
    return path


def _save_host_config(hermes_home):
    """Merge selection into CURRENT host settings, then verify persistence."""
    from hermes_cli.config import save_config
    from .hostio import read_user_config_raw

    path = _host_config_destination(hermes_home)
    try:
        save_config({"memory": {"provider": PROVIDER_NAME}}, merge_existing=True)
        saved = read_user_config_raw(path)
        if not isinstance(saved.get("memory"), dict) or saved["memory"].get("provider") != PROVIDER_NAME:
            raise RuntimeError("Host did not persist provider selection")
    except Exception as exc:
        raise RuntimeError("Host provider activation failed") from exc


def is_host_config(config) -> bool:
    """True when this is the whole Hermes config, not just our own block."""
    return isinstance(config, dict) and any(
        key in config for key in ("memory", "plugins", "model", "providers", "agent", "web"))


def post_setup(hermes_home, config=None) -> dict:
    """`hermes memory setup`: verify local engine/artifacts and own activation.

    The host hook calls this with the whole config dict and then stops, so this
    function is responsible for persisting ``memory.provider``. A caller that
    hands us only our own settings block gets the engine laid out and nothing
    else written.
    """
    from .settings import load_settings

    _require_managed_scope(hermes_home)
    hermes_home = Path(hermes_home)
    config = config if isinstance(config, dict) else {}
    owns_config = is_host_config(config)
    block = load_settings(hermes_home, legacy=plugin_block(config)) if owns_config else dict(config)
    if owns_config:
        _host_config_destination(hermes_home)
    result = ensure_engine(hermes_home, block)
    if result.get("status") not in {"present", "updated"} or result.get("unit_status") == "conflict":
        raise RuntimeError("Engine setup incomplete: " + str(result.get("status", "unknown")))
    if not owns_config:
        return {**result, "owns_config": False, "recall_readiness": "not_checked"}

    path = provider_config_path(hermes_home)
    merged = {**block, "zg_bin": result["zg_bin"]}
    if not path.exists() or load_settings(hermes_home) != merged:
        atomic_json_write(path, merged, mode=0o600)

    if not path.exists() or load_settings(hermes_home) != merged:
        raise RuntimeError("Provider configuration readback failed")
    _save_host_config(hermes_home)
    return {**result, "config": str(path), "provider": PROVIDER_NAME, "owns_config": owns_config,
            "recall_readiness": "not_checked"}
