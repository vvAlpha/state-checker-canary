from __future__ import annotations

import hashlib
import hmac
import http.server
import json
import threading
import urllib.error

import pytest

from canary import __main__ as cli
from canary import batch, publish, states
from canary.check import CaseResult, Report

SECRET = "s3cret-for-tests"
RUN = "https://github.com/acme/canary/actions/runs/1"
FL, NY = states.get("fl"), states.get("ny")


def case(problem=None):
    return CaseResult(
        eid="1",
        expect="verified",
        outcome="verified",
        verdict="eligible",
        reason="",
        is_active=True,
        seconds=1.0,
        attempts=1,
        problem=problem,
    )


def result(state=FL, *, problems=(), cases=None, trip=None, failed=None, why=""):
    rep = Report(state.code.upper(), state.name, "2026-01-01T00:00:00+00:00", served=True)
    rep.cases = cases if cases is not None else [case()]
    rep.problems = list(problems)
    rep.round_trip = trip
    return batch.StateResult(state, rep, bool(problems) if failed is None else failed, why)


class Opener:
    """Answers each attempt in turn: None delivers it, an exception is raised."""

    def __init__(self, *answers):
        self.answers, self.requests = list(answers), []

    def __call__(self, request, timeout):
        self.requests.append((request, timeout))
        answer = self.answers.pop(0)
        if answer is not None:
            raise answer

        class Done:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self, n=-1):
                return b'{"ok":true,"updated":1}'

        return Done()


def http_error(code):
    return urllib.error.HTTPError("https://checker.invalid", code, "x", {}, None)


def test_a_passing_state_reads_as_passed():
    row = publish.payload(
        [result(trip={"checked": True, "passed": True, "tries": []})], at="t", run_url=RUN, event="schedule"
    )["states"][0]
    assert row == {"state": "FL", "ok": True, "answers_ok": 1, "answers": 1, "storage": True, "note": ""}


def test_a_failing_state_carries_its_first_problem():
    row = publish.payload(
        [result(problems=["first", "second"], cases=[case(), case("x")])], at="t", run_url=RUN, event="e"
    )["states"][0]
    assert row["ok"] is False and row["note"] == "first" and (row["answers_ok"], row["answers"]) == (1, 2)


def test_a_state_with_no_report_is_failed_with_the_reason():
    r = batch.StateResult(NY, None, True, "the job left no report")
    assert publish.payload([r], at="t", run_url=RUN, event="e")["states"][0] == {
        "state": "NY",
        "ok": False,
        "answers_ok": 0,
        "answers": 0,
        "storage": None,
        "note": "the job left no report",
    }


@pytest.mark.parametrize(
    "trip, want",
    [
        (None, None),
        ({"checked": False, "passed": None, "tries": []}, None),
        ({"checked": True, "passed": None, "tries": []}, None),
        ({"checked": True, "passed": False, "tries": []}, False),
    ],
)
def test_storage_is_decided_or_none(trip, want):
    row = publish.payload([result(trip=trip)], at="t", run_url=RUN, event="e")["states"][0]
    assert row["storage"] is want


def test_a_long_note_is_cut():
    row = publish.payload([result(problems=["x" * 999])], at="t", run_url=RUN, event="e")["states"][0]
    assert len(row["note"]) == publish.NOTE_CHARS


def test_the_push_is_signed_over_timestamp_and_body():
    opener = Opener(None)
    publish.push("https://checker.invalid", SECRET, {"a": 1}, opener=opener, clock=lambda: 1000.0)
    request, _ = opener.requests[0]
    assert request.full_url == "https://checker.invalid/api/v1/canary/batch" and request.method == "POST"
    assert request.get_header("X-canary-timestamp") == "1000"
    want = hmac.new(SECRET.encode(), b"1000." + request.data, hashlib.sha256).hexdigest()
    assert request.get_header("X-canary-signature") == want
    assert json.loads(request.data) == {"a": 1}


@pytest.mark.parametrize("first", [http_error(503), http_error(429), OSError("reset"), TimeoutError()])
def test_a_transient_failure_is_tried_again_with_a_new_signature_time(first):
    opener, slept, ticks = Opener(first, None), [], iter([1000.0, 1010.0])
    publish.push("https://c.invalid", SECRET, {}, opener=opener, sleep=slept.append, clock=lambda: next(ticks))
    assert len(opener.requests) == 2 and slept == [publish.RETRY_PAUSE_SECONDS]
    assert opener.requests[1][0].get_header("X-canary-timestamp") == "1010"


@pytest.mark.parametrize("code", [401, 404, 413, 422])
def test_a_refusal_is_not_tried_again(code):
    opener = Opener(http_error(code))
    with pytest.raises(publish.PushError, match=f"HTTP {code}"):
        publish.push("https://c.invalid", SECRET, {}, opener=opener, sleep=lambda s: None)
    assert len(opener.requests) == 1


def test_it_gives_up_after_its_attempts_without_saying_the_secret():
    opener = Opener(*[OSError(SECRET)] * publish.ATTEMPTS)
    with pytest.raises(publish.PushError) as e:
        publish.push("https://c.invalid", SECRET, {}, opener=opener, sleep=lambda s: None)
    assert len(opener.requests) == publish.ATTEMPTS and SECRET not in str(e.value)


# the command


def _reports(tmp_path, code="fl", outcome="success"):
    folder = tmp_path / code
    folder.mkdir()
    rep = Report(code.upper(), states.get(code).name, "2026-01-01T00:00:00+00:00", served=True, cases=[case()])
    (folder / "report.json").write_text(json.dumps(rep.to_dict()))
    (folder / "outcome.txt").write_text(outcome)
    return json.dumps([{"code": code, "name": code.upper()}])


def _env(monkeypatch, secret=SECRET):
    monkeypatch.setenv("CHECKER_BASE_URL", "https://checker.invalid")
    monkeypatch.setenv("GITHUB_SERVER_URL", "https://github.com")
    monkeypatch.setenv("GITHUB_REPOSITORY", "acme/canary")
    monkeypatch.setenv("GITHUB_RUN_ID", "1")
    monkeypatch.setenv("GITHUB_EVENT_NAME", "schedule")
    if secret:
        monkeypatch.setenv("CANARY_PUSH_SECRET", secret)
    else:
        monkeypatch.delenv("CANARY_PUSH_SECRET", raising=False)


def test_batch_publish_sends_the_results(tmp_path, monkeypatch):
    _env(monkeypatch)
    plan, sent = _reports(tmp_path), []
    monkeypatch.setattr(publish, "push", lambda base, secret, data: sent.append((base, data)))
    assert cli.main(["batch", "--plan", plan, "--reports", str(tmp_path), "--publish"]) == 0
    base, data = sent[0]
    assert base == "https://checker.invalid" and data["event"] == "schedule" and data["run_url"] == RUN
    assert data["states"][0]["state"] == "FL" and data["states"][0]["ok"] is True


def test_a_host_in_a_note_is_redacted_before_the_push(tmp_path, monkeypatch):
    _env(monkeypatch)
    folder = tmp_path / "fl"
    folder.mkdir()
    rep = Report(
        "FL", "Florida", "2026-01-01T00:00:00+00:00", served=True, problems=["no answer from https://checker.invalid/x"]
    )
    (folder / "report.json").write_text(json.dumps(rep.to_dict()))
    (folder / "outcome.txt").write_text("failure")
    sent = []
    monkeypatch.setattr(publish, "push", lambda base, secret, data: sent.append(data))
    plan = json.dumps([{"code": "fl", "name": "FL"}])
    cli.main(["batch", "--plan", plan, "--reports", str(tmp_path), "--publish"])
    assert "checker.invalid" not in json.dumps(sent[0])


def test_a_failed_push_fails_the_job_loudly(tmp_path, monkeypatch, capsys):
    _env(monkeypatch)
    plan = _reports(tmp_path)

    def refuse(base, secret, data):
        raise publish.PushError("the checker did not take the batch (HTTP 401)")

    monkeypatch.setattr(publish, "push", refuse)
    assert cli.main(["batch", "--plan", plan, "--reports", str(tmp_path), "--publish"]) == 1
    assert "::error" in capsys.readouterr().out


def test_a_missing_secret_fails_the_job(tmp_path, monkeypatch, capsys):
    _env(monkeypatch, secret=None)
    plan = _reports(tmp_path)
    assert cli.main(["batch", "--plan", plan, "--reports", str(tmp_path), "--publish"]) == 1
    assert "CANARY_PUSH_SECRET" in capsys.readouterr().out


def test_without_the_flag_nothing_is_pushed(tmp_path, monkeypatch):
    _env(monkeypatch)
    plan = _reports(tmp_path)
    monkeypatch.setattr(publish, "push", lambda *a: pytest.fail("no push expected"))
    assert cli.main(["batch", "--plan", plan, "--reports", str(tmp_path)]) == 0


def test_the_batch_carries_when_its_checks_ended_not_when_it_was_pushed(tmp_path, monkeypatch):
    """Re-running only the summary job hours later must not make old results read as fresh."""
    _env(monkeypatch)
    folder = tmp_path / "fl"
    folder.mkdir()
    rep = Report("FL", "Florida", "2026-01-01T00:00:00+00:00", served=True, finished_at="2026-01-01T00:03:00+00:00")
    (folder / "report.json").write_text(json.dumps(rep.to_dict()))
    (folder / "outcome.txt").write_text("success")
    sent = []
    monkeypatch.setattr(publish, "push", lambda base, secret, data: sent.append(data))
    cli.main(["batch", "--plan", json.dumps([{"code": "fl", "name": "FL"}]), "--reports", str(tmp_path), "--publish"])
    assert sent[0]["at"] == "2026-01-01T00:03:00+00:00"


def test_checked_at_is_the_latest_report_or_the_default():
    a = result()
    a.report.finished_at = "2026-01-01T00:03:00+00:00"
    b = result(NY)
    b.report.finished_at = "2026-01-01T00:09:00+00:00"
    assert publish.checked_at([a, b], "now") == "2026-01-01T00:09:00+00:00"
    assert publish.checked_at([batch.StateResult(NY, None, True, "no report")], "now") == "now"
    c = result()
    c.report.started_at, c.report.finished_at = "not a time", None
    assert publish.checked_at([c], "now") == "now"


class _Server:
    """A real local HTTP server: what urllib does with an answer is the point of these tests."""

    def __init__(self, status, body=b"", location=None):
        seen = self.seen = []

        class Handler(http.server.BaseHTTPRequestHandler):
            def _answer(self):
                seen.append((self.command, self.path))
                self.send_response(status)
                if location:
                    self.send_header("Location", location)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            do_GET = do_POST = _answer

            def log_message(self, *a):
                pass

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()


@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
def test_a_redirect_is_a_failure_and_is_not_followed(status):
    s = _Server(status, location="/login")
    with pytest.raises(publish.PushError, match=f"HTTP {status}"):
        publish.push(s.base, SECRET, {}, sleep=lambda x: None)
    s.server.shutdown()
    assert s.seen == [("POST", publish.PATH)]  # one request, never the redirect's target


@pytest.mark.parametrize("body", [b"<html>a landing page</html>", b"[]", b'{"ok":false}', b'{"ok":"true"}', b""])
def test_a_200_that_is_not_the_acknowledgment_is_a_failure(body):
    s = _Server(200, body)
    with pytest.raises(publish.PushError, match="acknowledgment"):
        publish.push(s.base, SECRET, {}, sleep=lambda x: None)
    s.server.shutdown()
    assert len(s.seen) == 1


def test_the_checker_s_acknowledgment_is_delivered():
    s = _Server(200, b'{"ok":true,"updated":25}')
    publish.push(s.base, SECRET, {})
    s.server.shutdown()
