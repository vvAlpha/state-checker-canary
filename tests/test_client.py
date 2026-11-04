from __future__ import annotations

import io
import json
import urllib.error

import pytest

from canary.client import CheckerClient, base_url


class FakeResp:
    def __init__(self, status, body: bytes):
        self.status, self._body = status, body

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def opener_of(*items):
    calls = []

    def opener(req, timeout):
        calls.append(req.full_url)
        item = items[min(len(calls) - 1, len(items) - 1)]
        if isinstance(item, BaseException):
            raise item
        return item

    opener.calls = calls
    return opener


def http_error(code, body=b"{}"):
    return urllib.error.HTTPError("https://x.invalid", code, "err", {}, io.BytesIO(body))


def client(opener):
    return CheckerClient("https://checker.invalid", opener=opener, sleep=lambda s: None)


@pytest.mark.parametrize("raw", ["https://a.example", "https://a.example/", " https://a.example:8443 "])
def test_base_url_accepts_an_https_origin(raw):
    assert base_url(raw) == raw.strip().rstrip("/")


@pytest.mark.parametrize(
    "raw", [None, "", "http://a.example", "https://a.example/api", "https://a.example?x=1", "a.example", "https://"]
)
def test_base_url_rejects_anything_else(raw):
    with pytest.raises(SystemExit):
        base_url(raw)


def test_lookup_builds_an_encoded_url():
    op = opener_of(FakeResp(200, b"{}"))
    client(op).lookup("or", "123/45 6", live=True)
    assert op.calls == ["https://checker.invalid/api/v1/eligibility/or/123%2F45%206?live=true"]


def test_ok_json_object():
    r = client(opener_of(FakeResp(200, json.dumps({"a": 1}).encode()))).get("/x")
    assert r.ok and r.status == 200 and r.body == {"a": 1}


def test_retries_a_transport_failure_then_succeeds():
    op = opener_of(urllib.error.URLError("refused"), FakeResp(200, b"{}"))
    r = client(op).get("/x")
    assert r.ok and len(op.calls) == 2


def test_gives_up_after_two_attempts():
    op = opener_of(http_error(429), http_error(503))
    r = client(op).get("/x")
    assert not r.ok and r.status == 503 and len(op.calls) == 2


def test_a_json_404_is_an_answer_not_retried():
    op = opener_of(http_error(404, b'{"error": "nope"}'))
    r = client(op).get("/x")
    assert r.status == 404 and r.body == {"error": "nope"} and r.error == "HTTP 404" and len(op.calls) == 1


def test_non_json_and_non_object_bodies_are_errors():
    assert "not JSON" in client(opener_of(FakeResp(200, b"<html>"))).get("/x").error
    assert "not an object" in client(opener_of(FakeResp(200, b"[1]"))).get("/x").error


def test_timeout_is_an_error_without_status():
    r = client(opener_of(TimeoutError("slow"))).get("/x")
    assert r.status is None and r.error.startswith("no answer: TimeoutError")


def test_no_retry_starts_after_the_deadline():
    import time

    op = opener_of(urllib.error.URLError("refused"), FakeResp(200, b"{}"))
    c = client(op)
    c.deadline = time.monotonic() + 60
    c._sleep = lambda s: setattr(c, "deadline", time.monotonic() - 1)  # the backoff crosses the deadline
    r = c.get("/x")
    assert not r.ok and r.error.startswith("no answer") and len(op.calls) == 1


def test_no_first_attempt_after_the_deadline():
    import time

    op = opener_of(FakeResp(200, b"{}"))
    c = client(op)
    c.deadline = time.monotonic() - 1
    r = c.get("/x")
    assert op.calls == [] and r.error == "not sent: the check's time budget ran out"
