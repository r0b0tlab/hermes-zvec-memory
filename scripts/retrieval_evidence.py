"""Private bounded evidence: returned numbered lines against owned source reads.

No provider imports. Snapshots are query-time observations, not oracle hashes;
exporters validate them independently of producer outcome flags. Never export
these private source/response fields in the public metrics allowlist.
"""
from pathlib import Path, PurePosixPath
import re


def response_text(response):
    if (not isinstance(response, dict) or "error" in response
            or response.get("success") is False
            or not isinstance(response.get("results"), str)
            or len(response["results"]) > 2000):
        raise ValueError("invalid or oversized retrieval response")
    return response["results"]


def cited_sections(text):
    sections = re.split(r"(?m)(?=^#\d+ )", text)
    rank = 0
    for section in sections:
        if not re.match(r"^#\d+ ", section):
            continue  # query echoes are never evidence
        header = re.match(r"^#([1-5]) (?:matchedBy=\S+ )?(facts/[^\s:]+\.md):"
                          r"([1-9]\d*)(?:-([1-9]\d*))?[^\n]*\n", section)
        rank += 1
        if not header or int(header[1]) != rank:
            raise ValueError("invalid ranked citation")
        name = header[2]
        parts = PurePosixPath(name).parts
        if any(p in {".", "..", ""} for p in name.split("/")) or "\\" in name or parts[0] != "facts":
            raise ValueError("invalid source path")
        start, end = int(header[3]), int(header[4] or header[3])
        _, marker, source = ("\n" + section[header.end():]).partition("\nsource:\n")
        if not marker or start > end:
            raise ValueError("missing numbered source preview")
        lines = []
        for line in source.splitlines():
            match = re.fullmatch(r"([1-9]\d*)\t(.*)", line)
            if match:
                number, value = int(match[1]), match[2]
                if not start <= number <= end or (lines and number <= lines[-1][0]):
                    raise ValueError("invalid source line number")
                lines.append((number, value))
            elif line and line not in {"...", "…"}:
                raise ValueError("invalid source preview line")
        if not lines:
            raise ValueError("empty source preview")
        yield name, start, end, lines


def capture_sources(response, vault):
    sources = {}
    for name, *_ in cited_sections(response_text(response)):
        root = Path(vault).resolve()
        path = root / name
        if (any(p.is_symlink() for p in [path, *path.parents] if p != root)
                or not path.resolve().is_relative_to(root)):
            raise ValueError("source path escapes owned vault")
        with path.open(encoding="utf-8") as source:
            text = source.read(65537)
        if len(text) > 65536:
            raise ValueError("oversized source snapshot")
        sources[name] = text
    return sources


def cited_lines(response, sources):
    if not isinstance(sources, dict):
        raise ValueError("missing source observations")
    observed = []
    for name, start, end, lines in cited_sections(response_text(response)):
        text = sources.get(name)
        if not isinstance(text, str) or len(text) > 65536:
            raise ValueError("missing source snapshot")
        actual = text.splitlines()
        if not 1 <= start <= end <= len(actual):
            raise ValueError("citation outside source")
        for number, value in lines:
            if value != actual[number - 1]:
                raise ValueError("returned line differs from source")
            observed.append(value)
    return observed
