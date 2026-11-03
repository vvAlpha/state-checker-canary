"""One GitHub issue per state, kept in step with its checks.

A failing check opens the state's issue, or comments on the one already open;
the first passing check closes it. The issue carries the labels `canary` and
`canary-<code>`, which is how it is found again.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable

from .states import State

API = "https://api.github.com"


class GitHubError(RuntimeError):
    pass


class GitHub:
    def __init__(self, repo: str, token: str, *, api: str = API, opener: Callable = urllib.request.urlopen):
        if not repo or "/" not in repo:
            raise SystemExit("GITHUB_REPOSITORY must be owner/name")
        if not token:
            raise SystemExit("GH_TOKEN is not set: the issue step needs the workflow's token")
        self.repo, self.token, self.api, self._open = repo, token, api, opener

    def call(self, method: str, path: str, payload: dict | None = None, ok: tuple[int, ...] = (200, 201)):
        data = json.dumps(payload).encode() if payload is not None else None
        req = urllib.request.Request(
            f"{self.api}/repos/{self.repo}{path}",
            data=data,
            method=method,
            headers={
                "Authorization": f"Bearer {self.token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
                "Content-Type": "application/json",
                "User-Agent": "state-checker-canary",
            },
        )
        try:
            with self._open(req, timeout=30) as r:
                status, raw = r.status, r.read()
        except urllib.error.HTTPError as e:
            status, raw = e.code, e.read()
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            raise GitHubError(f"{method} {path}: no answer from GitHub ({e})") from e
        if status not in ok:
            raise GitHubError(f"{method} {path}: HTTP {status}: {raw[:300].decode('utf-8', 'replace')}")
        return status, (json.loads(raw) if raw else None)

    def ensure_label(self, name: str, color: str, description: str) -> None:
        # 422: the label exists already
        self.call("POST", "/labels", {"name": name, "color": color, "description": description}, ok=(201, 422))

    def open_issue(self, label: str) -> int | None:
        query = urllib.parse.urlencode({"state": "open", "labels": label, "per_page": 20})
        _, issues = self.call("GET", f"/issues?{query}")
        numbers = sorted(i["number"] for i in issues if "pull_request" not in i)
        return numbers[0] if numbers else None


def labels(state: State) -> tuple[str, str]:
    return "canary", f"canary-{state.code}"


def sync(gh: GitHub, state: State, *, failed: bool, body: str, run_url: str, assignee: str | None = None) -> str:
    """Open, update or close the state's issue. Returns what it did. A new issue is
    assigned to `assignee` (the repository owner), so GitHub notifies them."""
    group, own = labels(state)
    number = gh.open_issue(own)
    if not failed:
        if number is None:
            return "passing; no issue open"
        gh.call("POST", f"/issues/{number}/comments", {"body": f"Passing again: {run_url}"})
        gh.call("PATCH", f"/issues/{number}", {"state": "closed", "state_reason": "completed"})
        return f"passing; closed #{number}"
    gh.ensure_label(group, "B60205", "Opened and closed by the scheduled state checks")
    gh.ensure_label(own, "D93F0B", f"{state.name} checker check")
    if number is not None:
        gh.call("POST", f"/issues/{number}/comments", {"body": body})
        return f"failing; commented on #{number}"
    new = {"title": f"{state.code.upper()} ({state.name}) checker check failing", "body": body, "labels": [group, own]}
    if assignee:
        new["assignees"] = [assignee]
    _, issue = gh.call("POST", "/issues", new)
    return f"failing; opened #{issue['number']}"
