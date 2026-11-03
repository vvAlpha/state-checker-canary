from __future__ import annotations

import json

import pytest

from canary import __main__ as cli
from canary import batch, issue, states
from canary.check import Checker, Report


def _fake_run(ok):
    def run(self, state, *, all_cases=False):
        r = Report(state.code.upper(), state.name, "2026-01-01T00:00:00+00:00", served=True)
        if not ok:
            r.problems.append("200: expected a verified answer, got technical (X)")
        r.warnings.append("a warning")
        return r

    return run


# plan


def test_plan_lists_every_state_in_order(capsys):
    cli.main(["plan"])
    plan = json.loads(capsys.readouterr().out)
    assert [p["code"] for p in plan] == [s.code for s in states.STATES]
    assert plan[0] == {"code": "ca", "name": "CA California"}


def test_plan_takes_a_comma_list_without_duplicates(capsys):
    cli.main(["plan", "--states", " OH, wa ,oh,"])
    assert [p["code"] for p in json.loads(capsys.readouterr().out)] == ["oh", "wa"]


def test_plan_refuses_an_unknown_state():
    with pytest.raises(SystemExit):
        cli.main(["plan", "--states", "oh,zz"])


# check


@pytest.mark.parametrize("ok, code", [(True, 0), (False, 1)])
def test_check_writes_report_summary_and_annotations(tmp_path, monkeypatch, capsys, ok, code):
    monkeypatch.setenv("CHECKER_BASE_URL", "https://checker.invalid")
    monkeypatch.setattr(Checker, "run", _fake_run(ok))
    out, summary = tmp_path / "out" / "r.json", tmp_path / "s.md"
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


def test_check_prints_the_summary_with_commands_off(monkeypatch, capsys):
    monkeypatch.setenv("CHECKER_BASE_URL", "https://checker.invalid")
    monkeypatch.setattr(Checker, "run", _fake_run(False))
    cli.main(["check", "--state", "az"])
    lines = capsys.readouterr().out.splitlines()
    start = next(i for i, line in enumerate(lines) if line.startswith("::stop-commands::"))
    token = lines[start].split("::")[2]
    end = lines.index(f"::{token}::")
    assert any(line.startswith("## ") for line in lines[start:end])
    assert any(line.startswith("::error ") for line in lines[end:])  # annotations after commands resume


# batch


def _write(folder, report=None, outcome="success", raw=None):
    folder.mkdir(parents=True)
    if raw is not None or report is not None:
        (folder / "report.json").write_text(json.dumps(raw if raw is not None else report.to_dict()))
    if outcome is not None:
        (folder / "outcome.txt").write_text(outcome + "\n")


def _plan(*codes):
    return json.dumps([{"code": c, "name": c.upper()} for c in codes])


@pytest.fixture
def synced(monkeypatch):
    seen = {}

    def sync(gh, state, *, failed, body, run_url, assignee=None):
        seen[state.code] = {"failed": failed, "body": body, "assignee": assignee}
        return "ok"

    monkeypatch.setattr(issue, "sync", sync)
    monkeypatch.setenv("GITHUB_REPOSITORY", "o/r")
    monkeypatch.setenv("GH_TOKEN", "tok")
    monkeypatch.setenv("GITHUB_REPOSITORY_OWNER", "someone")
    return seen


def test_batch_summarizes_and_syncs_every_planned_state(tmp_path, synced, capsys):
    good = Report("AZ", "Arizona", "t", served=True)
    bad = Report("CA", "California", "t", served=True, problems=["200: wrong"])
    _write(tmp_path / "az", good)
    _write(tmp_path / "ca", bad, outcome="failure")
    summary = tmp_path / "summary.md"
    code = cli.main(["batch", "--plan", _plan("az", "ca", "ct"), "--reports", str(tmp_path),
                     "--summary", str(summary), "--issues"])  # fmt: skip
    assert code == 0
    assert {k: v["failed"] for k, v in synced.items()} == {"az": False, "ca": True, "ct": True}
    assert synced["ct"]["assignee"] == "someone" and "the job left no report" in synced["ct"]["body"]
    text = summary.read_text()
    assert "1 of 3 states passed" in text
    assert "| AZ Arizona | ✅ |" in text and "| CA California | ❌ |" in text and "200: wrong" in text
    assert "::error title=State checks failed::2 of 3: CA, CT" in capsys.readouterr().out


def test_batch_without_issues_flag_touches_no_issue(tmp_path, synced):
    _write(tmp_path / "az", Report("AZ", "Arizona", "t", served=True))
    cli.main(["batch", "--plan", _plan("az"), "--reports", str(tmp_path)])
    assert synced == {}


@pytest.mark.parametrize(
    "raw",
    [
        {"cases": [None], "problems": [], "warnings": []},
        {"cases": [], "problems": "x", "warnings": []},
        [],
        {"cases": [], "problems": [], "warnings": [], "state": "AZ"},  # missing name, started_at
        {"cases": [{"eid": "1", "expect": "dead", "problem": "wrong"}], "problems": [], "warnings": [],
         "state": "AZ", "name": "Arizona", "started_at": "t"},
        {"cases": [], "problems": [], "warnings": [], "state": "AZ", "name": "Arizona", "started_at": "t",
         "round_trip": {"checked": True, "passed": False}},
    ],
)  # fmt: skip
def test_an_unusable_or_contradictory_report_counts_as_failed(tmp_path, raw):
    _write(tmp_path / "az", raw=raw)
    result = batch.load(states.get("az"), tmp_path / "az")
    assert result.failed and result.report is None


def test_a_stored_ok_never_overrides_the_problems(tmp_path):
    report = Report("AZ", "Arizona", "t", served=True, problems=["p"])
    _write(tmp_path / "az", raw={**report.to_dict(), "ok": True})
    assert batch.load(states.get("az"), tmp_path / "az").failed


@pytest.mark.parametrize("outcome", ["failure", None])
def test_a_check_step_that_did_not_succeed_fails_the_state(tmp_path, outcome):
    _write(tmp_path / "az", Report("AZ", "Arizona", "t", served=True), outcome=outcome)
    assert batch.load(states.get("az"), tmp_path / "az").failed


def test_plan_refuses_an_empty_selection():
    with pytest.raises(SystemExit):
        cli.main(["plan", "--states", ",,,"])


def test_one_state_issue_error_does_not_stop_the_others(tmp_path, monkeypatch, capsys):
    seen = []

    def sync(gh, state, *, failed, body, run_url, assignee=None):
        seen.append(state.code)
        if state.code == "az":
            raise issue.GitHubError("POST /issues/3/comments: HTTP 403: locked")
        return "ok"

    monkeypatch.setattr(issue, "sync", sync)
    monkeypatch.setenv("GITHUB_REPOSITORY", "o/r")
    monkeypatch.setenv("GH_TOKEN", "tok")
    code = cli.main(["batch", "--plan", _plan("az", "ca"), "--reports", str(tmp_path), "--issues"])
    assert seen == ["az", "ca"] and code == 1
    assert "::error title=Issue not updated::AZ: POST /issues/3/comments: HTTP 403: locked" in capsys.readouterr().out


def test_a_round_trip_that_is_not_an_object_counts_as_failed(tmp_path):
    report = Report("AZ", "Arizona", "t", served=True)
    _write(tmp_path / "az", raw={**report.to_dict(), "round_trip": "broken"})
    result = batch.load(states.get("az"), tmp_path / "az")
    assert result.failed and result.report is None


def test_a_failed_step_with_a_passing_report_says_why_in_body_and_table(tmp_path):
    _write(tmp_path / "az", Report("AZ", "Arizona", "t", served=True), outcome="failure")
    result = batch.load(states.get("az"), tmp_path / "az")
    assert result.failed and "the check step ended as failure" in result.body
    assert "the check step ended as failure" in batch.summary([result], "t")


@pytest.mark.parametrize(
    "trip",
    [
        {"checked": True, "passed": []},
        {"checked": "yes", "passed": True},
        {"checked": True, "passed": True, "tries": "x"},
        {"checked": True, "passed": True, "tries": [{"basis": "store"}]},
    ],
)
def test_a_malformed_round_trip_counts_as_failed_and_the_summary_still_renders(tmp_path, trip):
    report = Report("AZ", "Arizona", "t", served=True)
    _write(tmp_path / "az", raw={**report.to_dict(), "round_trip": trip})
    result = batch.load(states.get("az"), tmp_path / "az")
    assert result.failed and result.report is None
    assert "AZ Arizona" in batch.summary([result], "t")


def test_a_failed_step_with_problems_does_not_repeat_itself(tmp_path):
    _write(tmp_path / "az", Report("AZ", "Arizona", "t", served=True, problems=["200: wrong"]), outcome="failure")
    result = batch.load(states.get("az"), tmp_path / "az")
    assert result.failed and "check step ended" not in result.body
