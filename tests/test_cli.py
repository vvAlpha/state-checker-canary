from __future__ import annotations

import json

import pytest

from canary import __main__ as cli
from canary import issue
from canary.check import Checker, Report


def _fake_run(ok):
    def run(self, state, *, all_cases=False):
        r = Report(state.code.upper(), state.name, "2026-01-01T00:00:00+00:00", served=True)
        if not ok:
            r.problems.append("200: expected a verified answer, got technical (X)")
        r.warnings.append("a warning")
        return r

    return run


@pytest.mark.parametrize("ok, code", [(True, 0), (False, 1)])
def test_check_writes_report_summary_and_annotations(tmp_path, monkeypatch, capsys, ok, code):
    monkeypatch.setenv("CHECKER_BASE_URL", "https://checker.invalid")
    monkeypatch.setattr(Checker, "run", _fake_run(ok))
    out, summary = tmp_path / "r.json", tmp_path / "s.md"
    assert cli.main(["check", "--state", "AZ", "--json", str(out), "--summary", str(summary)]) == code
    assert json.loads(out.read_text())["ok"] is ok
    assert summary.read_text().startswith("## ")
    printed = capsys.readouterr().out
    assert "::add-mask::checker.invalid" in printed
    assert "::warning title=AZ check::a warning" in printed
    assert ("::error title=AZ check::" in printed) is (not ok)


def test_check_refuses_a_bad_base_url(monkeypatch):
    monkeypatch.setenv("CHECKER_BASE_URL", "http://plain.invalid")
    with pytest.raises(SystemExit):
        cli.main(["check", "--state", "az"])


def test_unknown_state_exits():
    with pytest.raises(SystemExit):
        cli.main(["check", "--state", "zz"])


@pytest.mark.parametrize("bad", ["maybe", "2"])
def test_all_cases_must_be_a_boolean(bad):
    with pytest.raises(SystemExit):
        cli.main(["check", "--state", "az", "--all-cases", bad])


def _capture_sync(monkeypatch):
    seen = {}

    def sync(gh, state, *, failed, body, run_url, assignee=None):
        seen.update(failed=failed, body=body, state=state.code)
        return "ok"

    monkeypatch.setattr(issue, "sync", sync)
    monkeypatch.setenv("GITHUB_REPOSITORY", "o/r")
    monkeypatch.setenv("GH_TOKEN", "tok")
    return seen


def test_issue_with_a_missing_report_counts_as_failed(tmp_path, monkeypatch):
    seen = _capture_sync(monkeypatch)
    assert (
        cli.main(["issue", "--state", "az", "--report", str(tmp_path / "none.json"), "--check-outcome", "failure"]) == 0
    )
    assert seen["failed"] is True and "did not write a usable report" in seen["body"]


@pytest.mark.parametrize(
    "ok, outcome, failed", [(True, "success", False), (False, "failure", True), (True, "failure", True)]
)
def test_issue_follows_the_report_and_the_step_outcome(tmp_path, monkeypatch, ok, outcome, failed):
    seen = _capture_sync(monkeypatch)
    report = Report("AZ", "Arizona", "t", served=True)
    if not ok:
        report.problems.append("p")
    path = tmp_path / "r.json"
    path.write_text(json.dumps(report.to_dict()))
    cli.main(["issue", "--state", "az", "--report", str(path), "--check-outcome", outcome])
    assert seen["failed"] is failed
    assert seen["body"].startswith("## ")


def test_check_redacts_the_host_from_report_summary_and_output(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("CHECKER_BASE_URL", "https://secret-host.invalid")

    def run(self, state, *, all_cases=False):
        r = Report(state.code.upper(), state.name, "t", served=True)
        r.problems.append(
            "no answer: certificate is not valid for 'secret-host.invalid' at https://secret-host.invalid"
        )
        return r

    monkeypatch.setattr(Checker, "run", run)
    out, summary = tmp_path / "r.json", tmp_path / "s.md"
    cli.main(["check", "--state", "az", "--json", str(out), "--summary", str(summary)])
    printed = capsys.readouterr().out
    assert "secret-host" not in out.read_text() and "secret-host" not in summary.read_text()
    assert "<checker>" in out.read_text()
    assert printed.count("secret-host") == 1 and "::add-mask::secret-host.invalid" in printed


def test_check_prints_the_summary_with_commands_off(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("CHECKER_BASE_URL", "https://checker.invalid")
    monkeypatch.setattr(Checker, "run", _fake_run(False))
    cli.main(["check", "--state", "az"])
    lines = capsys.readouterr().out.splitlines()
    start = next(i for i, line in enumerate(lines) if line.startswith("::stop-commands::"))
    token = lines[start].split("::")[2]
    end = lines.index(f"::{token}::")
    assert any(line.startswith("## ") for line in lines[start:end])
    assert any(line.startswith("::error ") for line in lines[end:])  # annotations after commands resume


@pytest.mark.parametrize(
    "data",
    [
        {"cases": [None], "problems": [], "warnings": []},
        {"cases": [], "problems": "x", "warnings": []},
        [],
        {"cases": [], "problems": [], "warnings": [], "state": "AZ"},  # missing name, started_at
    ],
)
def test_issue_with_a_malformed_report_counts_as_failed(tmp_path, monkeypatch, data):
    seen = _capture_sync(monkeypatch)
    path = tmp_path / "r.json"
    path.write_text(json.dumps(data))
    cli.main(["issue", "--state", "az", "--report", str(path), "--check-outcome", "success"])
    assert seen["failed"] is True and "did not write a usable report" in seen["body"]


def test_issue_ignores_a_stored_ok_that_contradicts_the_problems(tmp_path, monkeypatch):
    seen = _capture_sync(monkeypatch)
    report = Report("AZ", "Arizona", "t", served=True, problems=["p"])
    path = tmp_path / "r.json"
    path.write_text(json.dumps({**report.to_dict(), "ok": True}))
    cli.main(["issue", "--state", "az", "--report", str(path), "--check-outcome", "success"])
    assert seen["failed"] is True


def test_issue_assigns_the_repository_owner(tmp_path, monkeypatch):
    seen = {}

    def sync(gh, state, *, failed, body, run_url, assignee):
        seen["assignee"] = assignee
        return "ok"

    monkeypatch.setattr(issue, "sync", sync)
    monkeypatch.setenv("GITHUB_REPOSITORY", "o/r")
    monkeypatch.setenv("GH_TOKEN", "tok")
    monkeypatch.setenv("GITHUB_REPOSITORY_OWNER", "someone")
    cli.main(["issue", "--state", "az", "--report", str(tmp_path / "none"), "--check-outcome", "failure"])
    assert seen["assignee"] == "someone"


@pytest.mark.parametrize(
    "extra",
    [
        {"cases": [{"eid": "1", "expect": "dead", "problem": "wrong"}]},
        {"round_trip": {"checked": True, "passed": False}},
    ],
)
def test_issue_rejects_a_report_whose_parts_failed_without_problems(tmp_path, monkeypatch, extra):
    seen = _capture_sync(monkeypatch)
    report = Report("AZ", "Arizona", "t", served=True)
    path = tmp_path / "r.json"
    path.write_text(json.dumps({**report.to_dict(), **extra}))
    cli.main(["issue", "--state", "az", "--report", str(path), "--check-outcome", "success"])
    assert seen["failed"] is True
