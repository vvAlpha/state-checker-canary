"""Push a finished batch's results to the checker, which shows them on its console.

The checker's POST /api/v1/canary/batch takes one signed JSON body per batch, so nothing
polls GitHub and no browser talks to it. A push that does not arrive fails the batch's
summary job: a console quietly showing an old batch is what this exists to avoid (the
console also says when its newest batch is old).
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
import urllib.error
import urllib.request
from datetime import datetime

from .batch import StateResult

PATH = "/api/v1/canary/batch"
TIMEOUT_SECONDS = 30.0
ATTEMPTS = 3
RETRY_PAUSE_SECONDS = 10.0
NOTE_CHARS = 300


class PushError(RuntimeError):
    pass


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """A redirect turns the POST into a GET of somewhere else, whose 200 would read as
    delivered, and carries the signature with it. It raises as an HTTP error instead."""

    def redirect_request(self, *args, **kwargs):
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)


def _row(r: StateResult) -> dict:
    rep = r.report
    cases = rep.cases if rep is not None else []
    trip = (rep.round_trip or {}) if rep is not None else {}
    note = ""
    if r.failed:
        problems = rep.problems if rep is not None else []
        note = (problems[0] if problems else r.why)[:NOTE_CHARS]
    return {
        "state": r.state.code.upper(),
        "ok": not r.failed,
        "answers_ok": sum(1 for c in cases if not c.problem),
        "answers": len(cases),
        # True or False when it was checked and decided; None when not checked or inconclusive
        "storage": trip.get("passed") if trip.get("checked") else None,
        "note": note,
    }


def payload(results: list[StateResult], *, at: str, run_url: str, event: str) -> dict:
    return {"schema": 1, "at": at, "run_url": run_url, "event": event, "states": [_row(r) for r in results]}


def checked_at(results: list[StateResult], default: str) -> str:
    """When the batch's checks ended, from their reports: re-running only the summary job
    later must not make old results read as fresh. `default` when no report says."""
    times = []
    for r in results:
        rep = r.report
        for text in (rep.finished_at, rep.started_at) if rep is not None else ():
            try:
                when = datetime.fromisoformat(text) if isinstance(text, str) else None
            except ValueError:
                when = None
            if when is not None and when.tzinfo is not None:
                times.append(when)
                break
    return max(times).isoformat(timespec="seconds") if times else default


def signature(secret: str, timestamp: str, body: bytes) -> str:
    return hmac.new(secret.encode(), timestamp.encode() + b"." + body, hashlib.sha256).hexdigest()


def _acknowledged(response) -> bool:
    try:
        return json.loads(response.read(4096)).get("ok") is True
    except (ValueError, AttributeError):  # not JSON, or JSON that is not an object
        return False


def push(base: str, secret: str, data: dict, *, opener=_OPENER.open, sleep=time.sleep, clock=time.time) -> None:
    """Deliver the batch, trying again on no answer and on a 429 or 5xx. Delivered means the
    checker said {"ok": true}: anything else that answers 200 is not it. Raises PushError with
    a reason that never carries the secret. Signed per attempt: a signature ages."""
    body = json.dumps(data, separators=(",", ":")).encode()
    last = "not tried"
    for attempt in range(ATTEMPTS):
        if attempt:
            sleep(RETRY_PAUSE_SECONDS)
        timestamp = str(int(clock()))
        request = urllib.request.Request(
            base + PATH,
            data=body,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "X-Canary-Timestamp": timestamp,
                "X-Canary-Signature": signature(secret, timestamp, body),
            },
        )
        try:
            with opener(request, timeout=TIMEOUT_SECONDS) as response:
                if _acknowledged(response):
                    return
            last = "an answer that is not the checker's acknowledgment"
            break  # something else answered: the same request gets the same answer
        except urllib.error.HTTPError as e:
            last = f"HTTP {e.code}"
            if e.code != 429 and e.code < 500:
                break  # refused: the same request gets the same answer
        except (OSError, ValueError) as e:
            last = type(e).__name__
    raise PushError(f"the checker did not take the batch ({last})")
