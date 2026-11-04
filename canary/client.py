"""The checker's HTTP API, standard library only.

One call is one request plus transport retries: a refused connection, a
timeout, a 429 from the rate limit in front of the checker, a 502/503/504 from
the proxy, or a body that is not JSON. What the checker *says* (a registry that
did not answer, for example) is judged by check.py, not retried here.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass

USER_AGENT = "state-checker-canary/1 (+GitHub Actions)"
RETRY_STATUSES = {429, 502, 503, 504}


@dataclass
class Response:
    status: int | None  # None: no HTTP answer at all
    body: dict | None
    error: str | None  # set when the call failed
    seconds: float

    @property
    def ok(self) -> bool:
        return self.error is None


def base_url(raw: str | None) -> str:
    """CHECKER_BASE_URL: the checker's origin, https only."""
    url = (raw or "").strip().rstrip("/")
    parts = urllib.parse.urlsplit(url)
    if parts.scheme != "https" or not parts.netloc or parts.path or parts.query or parts.fragment:
        raise SystemExit(
            "CHECKER_BASE_URL must be the checker's https origin with no path, "
            "like https://checker.example.com (set it as a repository secret)"
        )
    return url


class CheckerClient:
    def __init__(
        self,
        base: str,
        *,
        # The checker answers within its 14 s deadline and nginx cuts at 25 s, so 30 s
        # per request is generous; two attempts keep five lookups inside the budget.
        timeout: float = 30.0,
        attempts: int = 2,
        backoff: tuple[float, ...] = (5.0,),
        opener: Callable = urllib.request.urlopen,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.base = base
        self.timeout = timeout
        self.attempts = attempts
        self.backoff = backoff
        self._open = opener
        self._sleep = sleep
        # A time.monotonic() deadline set by the check: no retry starts after it.
        self.deadline: float | None = None

    def lookup(self, state: str, eid: str, *, live: bool) -> Response:
        path = f"/api/v1/eligibility/{urllib.parse.quote(state, safe='')}/{urllib.parse.quote(eid, safe='')}"
        return self.get(path + ("?live=true" if live else ""))

    def config(self) -> Response:
        return self.get("/api/v1/eligibility/config")

    def get(self, path: str) -> Response:
        last: Response | None = None
        for attempt in range(self.attempts):
            if attempt:
                self._sleep(self.backoff[min(attempt - 1, len(self.backoff) - 1)])
            if self.deadline is not None and time.monotonic() >= self.deadline:
                # out of time: the failure so far, or "not started", is the answer
                return last or Response(None, None, "not sent: the check's time budget ran out", 0.0)
            last = self._once(path)
            if last.ok or (last.status is not None and last.status not in RETRY_STATUSES and last.body is not None):
                return last
        return last  # type: ignore[return-value]

    def _once(self, path: str) -> Response:
        req = urllib.request.Request(self.base + path, headers={"Accept": "application/json", "User-Agent": USER_AGENT})
        started = time.monotonic()
        status: int | None = None
        try:
            with self._open(req, timeout=self.timeout) as r:
                status, raw = r.status, r.read()
        except urllib.error.HTTPError as e:
            status, raw = e.code, e.read()
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            reason = getattr(e, "reason", e)
            return Response(None, None, f"no answer: {type(reason).__name__}: {reason}", _since(started))
        try:
            body = json.loads(raw)
        except ValueError:
            return Response(status, None, f"HTTP {status} with a body that is not JSON", _since(started))
        if not isinstance(body, dict):
            return Response(status, None, f"HTTP {status} with JSON that is not an object", _since(started))
        if status != 200:
            return Response(status, body, f"HTTP {status}", _since(started))
        return Response(status, body, None, _since(started))


def _since(started: float) -> float:
    return round(time.monotonic() - started, 2)
