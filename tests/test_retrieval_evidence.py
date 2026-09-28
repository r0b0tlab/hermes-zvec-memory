"""Pinned real-format regressions; fixtures are sanitized synthetic native data."""
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from retrieval_evidence import capture_sources, cited_lines
from stress_memory import query_evidence


def native_fixture(kind="positive"):
    return json.loads((ROOT / "tests/fixtures/zg-0.2.2-synthetic-retrieval.json").read_text())[kind]


def put_sources(root, sources):
    for name, text in sources.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)


@pytest.mark.parametrize("format", ["provider_strip", "engine_tab", "crlf_source"])
def test_retained_native_trailing_empty_source_is_exactly_validated(tmp_path, format):
    fixture = native_fixture()
    response, sources = fixture["response"], fixture["sources"]
    if format == "engine_tab": response["results"] += "\t\n"
    if format == "crlf_source": sources = {name: text.replace("\n", "\r\n") for name, text in sources.items()}
    put_sources(tmp_path, sources)
    observed = capture_sources(response, tmp_path)
    lines = cited_lines(response, sources)
    assert lines[-1] == ""
    assert lines[-2] == "For key stressSYNTHETIC001, the verified answer is payloadSYNTHETIC001."
    assert query_evidence(SimpleNamespace(_vault=tmp_path), response)["valid"] is True
    assert cited_lines(response, observed) == lines


@pytest.mark.parametrize("damage", ["middle_bare", "wrong_number", "extra_bare", "nonempty_final", "missing_newline", "malformed"])
def test_empty_terminal_convention_does_not_accept_malformed_lines(damage):
    fixture = native_fixture()
    response, sources = fixture["response"], fixture["sources"]
    if damage == "middle_bare": response["results"] = response["results"].replace("2\t\n", "2\n")
    elif damage == "wrong_number": response["results"] = response["results"][:-1] + "9"
    elif damage == "extra_bare": response["results"] += "\n9"
    elif damage == "nonempty_final": sources["facts/SYNTHETIC.md"] += "not empty"
    elif damage == "missing_newline": sources["facts/SYNTHETIC.md"] = sources["facts/SYNTHETIC.md"].rstrip("\n")
    else: response["results"] += " nonsense"
    with pytest.raises(ValueError): cited_lines(response, sources)


INVALID_ABSENCES = [
    "", " \n\t", "unrecognized SYNTHETIC response", "No matches.", "hits: 0",
    "query groups (1):\nQ1 [supplemental]: SYNTHETIC\nhits: 0",
    "query groups (1):\nQ1 [supplemental]: SYNTHETIC\nhits: 1\n\nNo matches.",
    "query groups (2):\nQ1 [supplemental]: SYNTHETIC\nhits: 0\n\nNo matches.",
    "query groups (1):\nQ1 [supplemental]: \nhits: 0\n\nNo matches.",
]


@pytest.mark.parametrize("body", INVALID_ABSENCES)
def test_producer_rejects_unrecognized_or_incomplete_absence(tmp_path, body):
    from stress_memory import native_queries
    response = {"results": body}
    result = {"errors": [], "queries": []}
    reader = SimpleNamespace(_vault=tmp_path, handle_tool_call=lambda *args: json.dumps(response))
    native_queries(reader, [], result, forbidden=["SYNTHETIC forbidden content"])
    assert result["errors"]
    assert result["negative_queries"][0]["valid"] is False
    assert result["negative_queries"][0]["response"] == response
    with pytest.raises(ValueError): cited_lines(response, {})


@pytest.mark.parametrize("body", INVALID_ABSENCES)
@pytest.mark.parametrize("lane", ["stress", "soak"])
def test_export_rejects_unrecognized_or_incomplete_absence(body, lane):
    from test_report_stress import complete_native_report, soak_report, tool
    raw = complete_native_report() if lane == "stress" else soak_report()
    query = (raw["recovery"]["negative_queries"][0] if lane == "stress" else
             next(q for q in raw["worker"]["queries"] if q["absent"]))
    query.update(response={"results": body}, sources={}, chars=len(body))
    with pytest.raises(ValueError, match="incomplete receipt"):
        tool().aggregate([raw])


def test_retained_complete_zero_hit_envelope_is_accepted(tmp_path):
    from stress_memory import native_queries
    from test_report_stress import complete_native_report, soak_report, tool
    fixture = native_fixture("zero_hit")
    result = {"errors": [], "queries": []}
    reader = SimpleNamespace(_vault=tmp_path, handle_tool_call=lambda *args: json.dumps(fixture["response"]))
    native_queries(reader, [], result, forbidden=["SYNTHETIC forbidden content"])
    assert not result["errors"] and result["negative_queries"][0]["valid"] is True
    assert cited_lines(fixture["response"], {}) == []
    assert tool().aggregate([complete_native_report(), soak_report()])["passed"] is True


def test_export_does_not_count_explicitly_invalid_soak_attempt():
    from test_report_stress import soak_report, tool
    raw = soak_report()
    raw["worker"]["queries"][0]["valid"] = False
    with pytest.raises(ValueError, match="incomplete receipt"):
        tool().aggregate([raw])


def test_export_accepts_retained_positive_source_shape():
    from test_report_stress import complete_native_report, tool
    raw = complete_native_report()
    query = raw["recovery"]["queries"][0]
    fixture = native_fixture()
    answer = "For key stressSYNTHETIC001, the verified answer is payloadSYNTHETIC001."
    query["response"] = {**fixture["response"], "results": fixture["response"]["results"].replace(answer, query["key"])}
    query["sources"] = {name: text.replace(answer, query["key"]) for name, text in fixture["sources"].items()}
    query["chars"] = len(query["response"]["results"])
    assert tool().aggregate([raw])["passed"] is True


RANKED_CONTROLS = ["one", "two", "primary", "engine_tab"]
RANKED_COUNT_DAMAGE = ["missing_hit", "undercount", "zero_count", "missing_count",
                       "malformed_count", "negative_count", "decimal_count", "duplicate_count",
                       "missing_envelope", "wrong_group"]
RANKED_PREVIEW_DAMAGE = ["incomplete_final", "ellipsis_final", "source_gap", "leading_gap",
                         "missing_final_source", "partial_final_line"]


def ranked_negative_fixture(case, key="SYNTHETIC forbidden query"):
    """Complete pinned CLI syntax, then one deliberate completeness defect."""
    count = 2 if case in {"two", "undercount", *RANKED_PREVIEW_DAMAGE} else 1
    sources = {f"facts/ranked-safe-{n}.md": f"SYNTHETIC safe control {n}\n"
               for n in range(1, count + 1)}
    sections = [f"#{n} matchedBy=fts {name}:1-2\nsource:\n1\t{text.rstrip()}\n2\t"
                for n, (name, text) in enumerate(sources.items(), 1)]
    body = (f"query groups (1):\nQ1 [supplemental]: {key}\nhits: {count}\n\n"
            + "\n\n".join(sections)).strip()
    if case == "missing_hit": body = body.replace("hits: 1", "hits: 2")
    elif case == "undercount": body = body.replace("hits: 2", "hits: 1")
    elif case == "zero_count": body = body.replace("hits: 1", "hits: 0")
    elif case == "missing_count": body = body.replace("hits: 1\n", "")
    elif case == "malformed_count": body = body.replace("hits: 1", "hits: unknown")
    elif case == "negative_count": body = body.replace("hits: 1", "hits: -1")
    elif case == "decimal_count": body = body.replace("hits: 1", "hits: 1.0")
    elif case == "duplicate_count": body = body.replace("hits: 1", "hits: 1\nhits: 2")
    elif case == "missing_envelope": body = body.split("\n\n", 1)[1]
    elif case == "wrong_group": body = body.replace("query groups (1)", "query groups (2)")
    elif case == "primary": body = body.replace("[supplemental]", "[primary]")
    elif case == "engine_tab": body += "\t\n"
    elif case == "incomplete_final": body = body.rsplit("\n", 1)[0]
    elif case == "ellipsis_final": body = body.rsplit("\n", 1)[0] + "\n..."
    elif case in {"source_gap", "leading_gap"}:
        body = body.replace("ranked-safe-2.md:1-2", "ranked-safe-2.md:1-3")
        body = body.rsplit("\n", 1)[0] + "\n3"
        if case == "source_gap": sources["facts/ranked-safe-2.md"] += "SYNTHETIC forbidden content\n"
        else:
            sources["facts/ranked-safe-2.md"] = "SYNTHETIC forbidden content\n" + sources["facts/ranked-safe-2.md"]
            body = body.replace("1\tSYNTHETIC safe control 2", "2\tSYNTHETIC safe control 2")
    elif case == "missing_final_source": body = body.rsplit("\nsource:", 1)[0]
    elif case == "partial_final_line": body = body.rsplit(" control 2", 1)[0]
    return {"results": body}, sources


@pytest.mark.parametrize("case", RANKED_CONTROLS + RANKED_COUNT_DAMAGE + RANKED_PREVIEW_DAMAGE)
def test_producer_requires_complete_ranked_negative(tmp_path, case):
    from stress_memory import native_queries
    response, sources = ranked_negative_fixture(case)
    put_sources(tmp_path, sources)
    result = {"errors": [], "queries": []}
    reader = SimpleNamespace(_vault=tmp_path, handle_tool_call=lambda *args: json.dumps(response))
    native_queries(reader, [], result, forbidden=["SYNTHETIC forbidden content"])
    row = result["negative_queries"][0]
    assert row["response"] == response
    assert row["valid"] is (case in RANKED_CONTROLS)
    assert bool(result["errors"]) is (case not in RANKED_CONTROLS)
    assert row["stale"] is False


@pytest.mark.parametrize("case", RANKED_CONTROLS + RANKED_COUNT_DAMAGE + RANKED_PREVIEW_DAMAGE)
@pytest.mark.parametrize("lane", ["stress", "soak"])
def test_export_requires_complete_ranked_negative(case, lane):
    from test_report_stress import complete_native_report, soak_report, tool
    raw = complete_native_report() if lane == "stress" else soak_report()
    query = (raw["recovery"]["negative_queries"][0] if lane == "stress" else
             next(q for q in raw["worker"]["queries"] if q["absent"]))
    response, sources = ranked_negative_fixture(case, query["key"])
    query.update(response=response, sources=sources, chars=len(response["results"]))
    if case in RANKED_CONTROLS:
        assert tool().aggregate([raw])["passed"] is True
    else:
        with pytest.raises(ValueError, match="incomplete receipt"):
            tool().aggregate([raw])
