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
