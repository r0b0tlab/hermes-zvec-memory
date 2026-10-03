"""Health aggregation requires complete evidence, not just no failures."""
import argparse
import json

import pytest

from test_cli import load_cli


def healthy_checks(cli):
    return [{"name": name, "ok": True, "detail": "observed"}
            for name in cli.REQUIRED_CHECKS]


@pytest.mark.parametrize("coverage", ["empty", "missing", "duplicate", "unexpected"])
def test_report_refuses_incomplete_or_duplicate_coverage(coverage):
    cli = load_cli()
    checks = healthy_checks(cli)
    if coverage == "empty":
        checks = []
    elif coverage == "missing":
        checks.pop()
    elif coverage == "duplicate":
        checks[-1] = dict(checks[0])
    else:
        checks.append({"name": "not-a-required-check", "ok": True, "detail": "extra"})

    result = cli.report(checks)

    assert result["checked"] is False
    assert result["ok"] is False
    assert result["checks"] == checks
    assert result["failures"] == []


def test_report_accepts_complete_success_in_any_order():
    cli = load_cli()
    checks = list(reversed(healthy_checks(cli)))

    assert cli.report(checks) == {
        "ok": True, "checked": True, "checks": checks, "failures": []}


@pytest.mark.parametrize("status", [1, "true", [True], {"passed": True}],
                         ids=["integer", "string", "list", "object"])
def test_report_requires_literal_true_for_success(status):
    cli = load_cli()
    checks = healthy_checks(cli)
    checks[0]["ok"] = status

    result = cli.report(checks)

    assert result["checked"] is True
    assert result["ok"] is False
    assert result["failures"] == [checks[0]["name"]]
    assert result["checks"] == checks


@pytest.mark.parametrize("status", [False, None, 0, "", [], {}])
def test_report_preserves_explicit_unsuccessful_checks(status):
    cli = load_cli()
    checks = healthy_checks(cli)
    checks[-1]["ok"] = status

    result = cli.report(checks)

    assert result["checked"] is True
    assert result["ok"] is False
    assert result["failures"] == [checks[-1]["name"]]


@pytest.mark.parametrize("coverage", ["complete", "empty", "missing", "duplicate", "truthy"])
def test_doctor_json_and_exit_reflect_report_coverage(coverage, tmp_path, monkeypatch, capsys):
    cli = load_cli()
    checks = healthy_checks(cli)
    if coverage == "empty":
        checks = []
    elif coverage == "missing":
        checks.pop()
    elif coverage == "duplicate":
        checks[-1] = dict(checks[0])
    elif coverage == "truthy":
        checks[0]["ok"] = "true"
    monkeypatch.setattr(cli, "_configured", lambda: (tmp_path, {"zg_bin": "/not-executed"}))
    monkeypatch.setattr(cli, "collect_checks", lambda vault, config: checks)

    parser = argparse.ArgumentParser()
    cli.register_cli(parser)
    args = parser.parse_args(["doctor", "--json"])
    expected_ok = coverage == "complete"

    assert args.func(args) == (0 if expected_ok else 1)
    result = json.loads(capsys.readouterr().out)
    assert result["ok"] is expected_ok
    assert result["checked"] is (coverage in {"complete", "truthy"})
    assert result["checks"] == checks
