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
    if len(sections) == 1:
        # One query is sent per harness probe. Absence is evidence only when
        # the pinned engine explicitly completes that group with zero hits.
        empty = re.fullmatch(r"query groups \(1\):\nQ1 \[(?:primary|supplemental)\]: ([^\r\n]+)"
                             r"\nhits: 0\n\nNo matches\.\n?", text)
        if not empty or not empty[1].strip():
            raise ValueError("missing complete zero-hit result envelope")
        return
    # Pinned CLI context.js counts group.items.length, i.e. the ranked items
    # it renders, not all search candidates. A clipped list proves no absence.
    envelope = re.fullmatch(r"query groups \(1\):\nQ1 \[(?:primary|supplemental)\]: ([^\r\n]+)"
                            r"\nhits: ([1-5])\n\n", sections[0])
    if (not envelope or not envelope[1].strip()
            or int(envelope[2]) != len(sections) - 1):
        raise ValueError("incomplete ranked result envelope")
    rank = 0
    for section in sections[1:]:
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
        for line in re.split(r"\r?\n", source):
            match = re.fullmatch(r"([1-9]\d*)\t(.*)", line)
            # The pinned formatter includes the newline-split terminal empty
            # entry. Provider .strip() removes its tab only at response EOF.
            if (not match and section == sections[-1] and source.endswith("\n" + line)
                    and line == str(end)):
                match = re.fullmatch(r"([1-9]\d*)\t(.*)", line + "\t")
            if match:
                number, value = int(match[1]), match[2]
                if not start <= number <= end or (lines and number <= lines[-1][0]):
                    raise ValueError("invalid source line number")
                lines.append((number, value))
            elif line:
                raise ValueError("invalid source preview line")
        # A valid short/windowed preview can still hide the forbidden answer.
        # Strictly increasing in-range numbers must cover the entire citation,
        # including the terminal empty entry; count equality alone is not enough.
        if len(lines) != end - start + 1:
            raise ValueError("incomplete source preview")
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
        actual = re.split(r"\r?\n", text)
        if not 1 <= start <= end <= len(actual):
            raise ValueError("citation outside source")
        for number, value in lines:
            if value != actual[number - 1]:
                raise ValueError("returned line differs from source")
            observed.append(value)
    return observed
