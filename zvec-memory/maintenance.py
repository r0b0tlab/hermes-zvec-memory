"""Short generation-safe rebuild transactions, independent of native work."""
import json
import uuid
from datetime import datetime, timezone
from pathlib import Path

from .hostio import atomic_json_write
from .transactions import VaultLock

REQUEST_FILE = ".reindex-request.json"
REFRESH_FILE = ".index-refresh.json"
_UNCONDITIONAL = object()


def _request_path(vault):
    path = Path(vault) / REQUEST_FILE
    if path.is_symlink():
        raise ValueError("Refusing symlinked rebuild request")
    return path


def read_request(vault):
    vault = Path(vault)
    with VaultLock(vault / ".reindex-request.lock"):
        path = _request_path(vault)
        try:
            return path.read_bytes()
        except FileNotFoundError:
            return None


def request_identity(request):
    """Optional identity bound to a request, never an executable override.

    Legacy opaque bytes / objects without identity mean rebuild the established
    index, not a model-change instruction. Malformed identity-bearing objects
    fail closed rather than being downgraded to that legacy contract.
    """
    if request is None:
        return None
    # Preserve old plain-text markers, but never reinterpret damaged JSON as
    # a legacy request: it may have carried a model-change obligation.
    text = request.decode("utf-8") if isinstance(request, bytes) else request
    try:
        value = json.loads(text)
    except ValueError:
        if text.lstrip().startswith(("{", "[", '"')):
            raise ValueError("Malformed structured rebuild request")
        return None
    if not isinstance(value, dict):
        raise ValueError("Invalid rebuild request object")
    if "identity" not in value:
        return None
    identity = value["identity"]
    if (not isinstance(identity, dict)
            or not {"plugin_schema", "embedding", "zg_bin"} <= set(identity)
            or set(identity) - {"plugin_schema", "embedding", "zg_bin", "zg_version"}
            or type(identity["plugin_schema"]) is not int
            or identity["plugin_schema"] != 1
            or any(not isinstance(identity[k], str) or not identity[k]
                   for k in ("embedding", "zg_bin"))
            or ("zg_version" in identity and
                (not isinstance(identity["zg_version"], str) or not identity["zg_version"]))):
        raise ValueError("Invalid rebuild request identity")
    return dict(identity)


def request_rebuild(vault, *, by="hermes zvec-memory reindex", identity=None,
                    expected=_UNCONDITIONAL):
    """Publish intent; optional expected bytes prevent stale worker admission.

    Only an explicit identity supersedes model/engine intent. Generic requests
    get a new request ID but carry forward any existing bound identity; damaged
    structured intent is refused, never silently downgraded. Conditional callers
    must read back the marker: superseding bytes remain intact.
    """
    bound = {} if identity is None else {"identity": identity}
    request_identity(json.dumps(bound))  # Validate before publishing any bytes.
    vault = Path(vault)
    vault.mkdir(parents=True, exist_ok=True)
    with VaultLock(vault / ".reindex-request.lock"):
        path = _request_path(vault)
        try:
            current = path.read_bytes()
        except FileNotFoundError:
            current = None
        if expected is not _UNCONDITIONAL and current != expected:
            return path
        if identity is None:
            pending = request_identity(current)
            if pending is not None:
                bound = {"identity": pending}
        atomic_json_write(path, {"request_id": uuid.uuid4().hex,
                                "requested": datetime.now(timezone.utc).isoformat(),
                                "by": by, **bound}, mode=0o600)
    return path


def acknowledge_request(vault, completed, *, identity=None):
    if completed is None:
        return False
    wanted = request_identity(completed)
    if wanted is not None and (identity is None or any(identity.get(k) != v for k, v in wanted.items())):
        return False
    vault = Path(vault)
    with VaultLock(vault / ".reindex-request.lock"):
        path = _request_path(vault)
        try:
            current = path.read_bytes()
        except FileNotFoundError:
            return False
        if current != completed:
            return False
        path.unlink()
        return True


def _refresh_path(vault):
    path = Path(vault) / REFRESH_FILE
    if path.is_symlink():
        raise ValueError("Refusing symlinked index refresh obligation")
    return path


def _refresh_value(raw):
    if raw is None:
        return {}
    value = json.loads(raw)
    if (not isinstance(value, dict) or
            not isinstance(value.get("request_id"), str) or not value["request_id"]):
        raise ValueError("Invalid index refresh obligation")
    identity = request_identity(raw)
    if identity is not None:
        if (value.get("operation") not in {"first", "refresh", "rebuild"} or
                not isinstance(value.get("mutation_id"), str) or not value["mutation_id"]):
            raise ValueError("Invalid index mutation obligation")
    elif "operation" in value or "mutation_id" in value:
        raise ValueError("Unbound index mutation obligation")
    return value


def read_refresh(vault):
    """Atomic, nonwaiting observation; writers share the short request lock."""
    try:
        raw = _refresh_path(vault).read_bytes()
    except FileNotFoundError:
        return None
    _refresh_value(raw)
    return raw


def request_refresh(vault):
    """Admit ordinary work without losing an interrupted mutation's identity.

    Unlike a rebuild request this does not change the established model. A new
    request ID survives a running job's completion, including rejected queues.
    """
    vault = Path(vault)
    with VaultLock(vault / ".reindex-request.lock"):
        value = _refresh_value(read_refresh(vault))
        atomic_json_write(_refresh_path(vault),
                          {**value, "request_id": uuid.uuid4().hex}, mode=0o600)


def begin_refresh(vault, identity, operation):
    """Bind every managed native mutation before dispatch (vault lock owned)."""
    vault = Path(vault)
    with VaultLock(vault / ".reindex-request.lock"):
        current = _refresh_value(read_refresh(vault))
        value = {"request_id": current.get("request_id", uuid.uuid4().hex),
                 "mutation_id": uuid.uuid4().hex, "identity": identity,
                 "operation": operation}
        _refresh_value(json.dumps(value))
        atomic_json_write(_refresh_path(vault), value, mode=0o600)
        return read_refresh(vault)


def acknowledge_refresh(vault, completed):
    """Clear only after identity/generation publication and mirror bookkeeping.

    Admission during native work must survive completion. Once the captured
    mutation is known published, newer ordinary work needs a refresh, not a
    repeat model rebuild. A different mutation is never rewritten here.
    """
    vault = Path(vault)
    with VaultLock(vault / ".reindex-request.lock"):
        current = read_refresh(vault)
        if current is None or completed is None:
            return False
        if current == completed:
            _refresh_path(vault).unlink()
            return True
        value, prior = _refresh_value(current), _refresh_value(completed)
        if value.get("mutation_id") == prior.get("mutation_id"):
            atomic_json_write(_refresh_path(vault),
                              {"request_id": value["request_id"]}, mode=0o600)
        return False
