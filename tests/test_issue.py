from __future__ import annotations

import io
import json
import urllib.error

import pytest

from canary import states
from canary.issue import GitHub, GitHubError, sync

AZ = states.get("az")


class FakeGitHubAPI:
    """Records requests; answers from a small in-memory issue list."""

    def __init__(self, issues=None, fail=None):
        self.issues = issues or []
        self.requests: list[tuple[str, str, dict | None]] = []
        self.fail = fail

    def __call__(self, req, timeout):
        body = json.loads(req.data) if req.data else None
        path = req.full_url.split("/repos/o/r", 1)[1]
        self.requests.append((req.get_method(), path, body))
        assert req.headers["Authorization"] == "Bearer tok"
        if self.fail and self.fail in path:
            raise urllib.error.HTTPError(req.full_url, 403, "no", {}, io.BytesIO(b"forbidden"))
        if req.get_method() == "GET":
            return Resp(200, self.issues)
        if path == "/labels":
            return Resp(422, {"message": "exists"}) if body["name"] == "canary" else Resp(201, {})
        if path == "/issues":
            return Resp(201, {"number": 7})
        return Resp(200, {})


class Resp:
    def __init__(self, status, payload):
        self.status, self._raw = status, json.dumps(payload).encode()

    def read(self):
        return self._raw

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def gh(api):
    return GitHub("o/r", "tok", api="https://api.invalid", opener=api)


def test_failure_with_no_issue_opens_one_with_both_labels_assigned_to_the_owner():
    api = FakeGitHubAPI()
    assert sync(gh(api), AZ, failed=True, body="B", run_url="U", assignee="me") == "failing; opened #7"
    method, path, body = api.requests[-1]
    assert (method, path) == ("POST", "/issues")
    assert body["labels"] == ["canary", "canary-az"] and body["title"].startswith("AZ (Arizona)")
    assert body["assignees"] == ["me"]
    assert api.requests[0][1].startswith("/issues?") and "labels=canary-az" in api.requests[0][1]


def test_failure_with_an_open_issue_comments():
    api = FakeGitHubAPI(issues=[{"number": 12}, {"number": 3, "pull_request": {}}])
    assert sync(gh(api), AZ, failed=True, body="B", run_url="U") == "failing; commented on #12"
    assert api.requests[-1] == ("POST", "/issues/12/comments", {"body": "B"})


def test_pass_closes_the_open_issue():
    api = FakeGitHubAPI(issues=[{"number": 12}])
    assert sync(gh(api), AZ, failed=False, body="B", run_url="U") == "passing; closed #12"
    assert api.requests[-2] == ("POST", "/issues/12/comments", {"body": "Passing again: U"})
    assert api.requests[-1] == ("PATCH", "/issues/12", {"state": "closed", "state_reason": "completed"})


def test_pass_with_no_issue_does_nothing_else():
    api = FakeGitHubAPI()
    assert sync(gh(api), AZ, failed=False, body="B", run_url="U") == "passing; no issue open"
    assert len(api.requests) == 1


def test_github_errors_raise_so_the_step_fails():
    with pytest.raises(GitHubError, match="HTTP 403"):
        sync(gh(FakeGitHubAPI(fail="/issues")), AZ, failed=True, body="B", run_url="U")


def test_missing_token_or_repo_exits():
    with pytest.raises(SystemExit):
        GitHub("o/r", "")
    with pytest.raises(SystemExit):
        GitHub("", "tok")
