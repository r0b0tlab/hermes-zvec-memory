import hashlib
import json
from pathlib import Path
import re
import subprocess

HOST_CONTRACT = (
    "agent/memory_provider.py",
    "agent/memory_manager.py",
    "agent/inline_tool_executors.py",
    "tools/memory_tool_store.py",
    "plugins/memory/__init__.py",
)


def host_provenance(root):
    root = Path(root).resolve()
    files = {}
    for name in HOST_CONTRACT:
        path = root / name
        if not path.is_file():
            raise ValueError(f"missing host contract file: {name}")
        files[name] = hashlib.sha256(path.read_bytes()).hexdigest()

    digest = hashlib.sha256(
        json.dumps(files, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    result = {
        "kind": "installed_snapshot",
        "content_sha256": digest,
        "files": files,
    }

    # A checkout/worktree root has a .git directory or worktree pointer.
    # An enclosing unrelated repository is not this host's provenance.
    if (root / ".git").exists():
        def git(*args):
            return subprocess.check_output(
                ["git", "-C", str(root), *args],
                text=True, timeout=5, stderr=subprocess.PIPE,
            ).strip()

        if Path(git("rev-parse", "--show-toplevel")).resolve() != root:
            raise ValueError("host is not the selected Git root")
        sha = git("rev-parse", "HEAD")
        if not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", sha):
            raise ValueError("invalid host Git revision")
        result.update(
            kind="git",
            git_sha=sha,
            dirty=bool(git("status", "--porcelain")),
        )

    result["identity"] = (
        f"git:{result['git_sha']}:{digest}"
        if result["kind"] == "git"
        else f"installed-snapshot:sha256:{digest}"
    )
    return result


def provider_source(default, selected=None):
    """Select only the measured product; never silently fall back to candidate."""
    import os
    root = Path(selected if selected is not None else os.environ.get("ZVEC_TEST_PROVIDER_ROOT", default)).resolve()
    for name in ("__init__.py", "plugin.yaml"):
        if not (root / "zvec-memory" / name).is_file():
            raise ValueError("incomplete selected provider source")
    return root


def tree_identity(root, sections, optional=()):
    root = Path(root).resolve()
    paths = []
    for name in sections:
        section = root / name
        if not section.is_dir():
            raise ValueError("missing fingerprinted source section")
        paths.append(section)
        paths.extend(section.rglob("*"))
    paths.extend(root / name for name in optional if (root / name).exists())
    files = {}
    for path in sorted(paths):
        relative = path.relative_to(root)
        if "__pycache__" in relative.parts or path.suffix in {".pyc", ".pyo"}:
            continue
        if path.is_symlink():
            raise ValueError("symlink in fingerprinted source")
        if path.is_file():
            with path.open("rb") as stream:
                files[relative.as_posix()] = hashlib.file_digest(stream, "sha256").hexdigest()
        elif not path.is_dir():
            raise ValueError("non-regular fingerprinted source")
    if not files:
        raise ValueError("empty source snapshot")
    digest = hashlib.sha256(json.dumps(files, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    result = {"kind":"source_snapshot", "content_sha256":digest, "identity":"sha256:"+digest, "files":files}
    if (root / ".git").exists():
        def git(*args):
            return subprocess.check_output(["git", "-C", str(root), *args], text=True,
                                           timeout=5, stderr=subprocess.PIPE).strip()
        if Path(git("rev-parse", "--show-toplevel")).resolve() != root:
            raise ValueError("source is not the selected Git root")
        sha = git("rev-parse", "HEAD")
        if not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", sha):
            raise ValueError("invalid source Git revision")
        result.update(kind="git", git_sha=sha,
                      dirty=bool(git("status", "--porcelain", "--", *sections, *optional)))
    return result


def product_identity(root):
    return tree_identity(provider_source(root, root), ("zvec-memory",))


def harness_identity(root):
    return tree_identity(root, ("scripts", "tests"), ("pytest.ini", "pyproject.toml", "requirements-dev.txt"))


def source_import_name(prefix, source):
    import os
    identity = product_identity(source)
    scope = [str(Path(source).resolve()), identity["content_sha256"], os.environ.get("HERMES_HOME")]
    return prefix + "_" + hashlib.sha256(json.dumps(scope).encode()).hexdigest()


def stop_direct(child, timeout=2):
    """Only the unreaped Popen retained by its sole owning parent."""
    errors = []
    try:
        if child.returncode is None:
            child.kill()
    except ProcessLookupError:
        pass
    except BaseException as exc:
        errors.append(exc)
    try:
        child.wait(timeout=timeout)
    except BaseException as exc:
        errors.append(exc)
    if errors:
        raise RuntimeError(
            "direct-child cleanup incomplete: "
            + ",".join(type(exc).__name__ for exc in errors)
        )


def new_workload_run(runs, prefix):
    """Create one exclusive output directory, optionally bound to a controller."""
    import os
    import tempfile
    attempt = os.environ.get("ZVEC_CAMPAIGN_ATTEMPT_ID")
    if attempt is not None and not re.fullmatch(r"hermes-zvec-stress-[a-f0-9]{32}", attempt):
        raise ValueError("invalid campaign attempt identity")
    runs = Path(runs)
    runs.mkdir(parents=True, exist_ok=True)
    if runs.is_symlink() or runs.absolute() != runs.resolve():
        raise ValueError("aliased workload output root")
    if attempt is None:
        run = Path(tempfile.mkdtemp(prefix=prefix + "-", dir=runs))
    else:
        run = runs / (prefix + "-" + attempt)
        run.mkdir(mode=0o700)  # refusal on reuse, even an apparently empty directory
    return run, attempt
