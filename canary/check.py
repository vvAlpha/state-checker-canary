"""Check one state end to end through the deployed checker.

1. Read the checker's configuration: whether the state is served. Whether it
   *should* be served is this repository's call (states.py), so a checker that
   drops a state by mistake fails instead of excusing itself.
2. Look up this batch's entities live (`?live=true`: the checker scrapes the
   state's registry now). They are drawn at random from the state's pool
   (pools/<code>.txt: ids the checker verified before), a few per batch with a
   random pause between them, so no entity is looked up on a fixed rhythm and no
   registry sees a burst. Every answer must be well formed, say it was live,
   follow the checker's decision table, and be "verified".
3. Round trip through storage: right after a live answer, a normal lookup must be
   served from storage (R2) with an observation this run produced, which proves
   answers are saved and read back.

What fails the run (someone has to act):
- the checker unreachable, or its configuration unreadable;
- a state served or not served against what states.py says;
- an answer that is malformed, not live when asked live, or not verified (a
  pool entity the checker cannot find, or a registry not answering twice);
- storage not serving this run's answer back, or serving it back different;
- the run's time budget running out, or the check itself crashing.

What only warns (shown in the summary, the run stays green):
- a state the checker is meant not to serve;
- no answer in this run that storage would serve (inactive entities, and ones
  not open yet, are re-checked live by design).
"""

from __future__ import annotations

import random
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime

from .client import CheckerClient, Response
from .pools import Pool
from .states import State

# A registry that did not answer gets one more try after this pause, so a
# session being renewed or a proxy line resting does not page anyone.
RETRY_PAUSE_SECONDS = 60
# R2 writes land 3 to 8 seconds after the answer (measured 2026-11-03, 25 states);
# the round trip asks after the first pause, then after each later one.
ROUND_TRIP_PAUSES = (15, 20, 30)
# Entities per state per batch, and the random pause between their lookups.
COUNT = 5
SPACING_SECONDS = (5.0, 15.0)
# The whole check stops starting new lookups after this, leaving the job time to
# write the report and update the issue before its own timeout (30 minutes).
BUDGET_SECONDS = 20 * 60

VERDICTS = frozenset(
    {"eligible", "eligible_delinquent", "not_yet_open", "already_filed", "needs_reinstatement", "dead",
     "not_applicable", "unknown"}
)  # fmt: skip
OUTCOMES = frozenset({"verified", "not_found", "technical", "local", "unsupported"})
# The checker's decision table: each verdict has one action, and can_file is true
# exactly for the two "go ahead" actions. is_active is not checked against the
# verdict: it comes from the state's own status field, separately.
ACTION_FOR = {
    "eligible": "proceed", "eligible_delinquent": "proceed_with_warning", "unknown": "proceed",
    "not_yet_open": "hold", "already_filed": "block", "needs_reinstatement": "block", "dead": "block",
    "not_applicable": "block",
}  # fmt: skip
CAN_FILE_ACTIONS = ("proceed", "proceed_with_warning")
TRANSIENT_OUTCOMES = ("technical", "local")


class OutOfTime(Exception):
    pass


@dataclass
class CaseResult:
    eid: str
    expect: str
    outcome: str | None = None
    verdict: str | None = None
    reason: str | None = None
    is_active: bool | None = None
    seconds: float | None = None
    attempts: int = 0
    problem: str | None = None
    observation_id: str | None = None
    answer: dict | None = None  # the fields storage must serve back unchanged
    answered_at: float | None = None  # monotonic, when the live answer came back


@dataclass
class Report:
    state: str
    name: str
    started_at: str
    checker_status: str | None = None  # the checker's own view: working, limited, down, not_served
    checker_note: str | None = None
    served: bool | None = None
    cases: list[CaseResult] = field(default_factory=list)
    round_trip: dict | None = None
    problems: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    finished_at: str | None = None

    @property
    def ok(self) -> bool:
        return not self.problems

    def to_dict(self) -> dict:
        return {**asdict(self), "ok": self.ok}


def _error(r: Response) -> str:
    body = r.body or {}
    data = body.get("data") if isinstance(body.get("data"), dict) else {}
    detail = body.get("error") or data.get("message")
    return f"{r.error}" + (f" ({detail})" if detail else "")


def malformed(body: dict, state: State, eid: str, *, basis: str) -> str | None:
    """Why an answer cannot be trusted, or None. `basis`: what the answer must say
    it came from ("live" when asked live)."""
    if body.get("state") != state.code.upper() or body.get("entity_id") != eid:
        return f"answered for {body.get('state')}/{body.get('entity_id')}, not {state.code.upper()}/{eid}"
    outcome, verdict = body.get("outcome"), body.get("verdict")
    if outcome not in OUTCOMES:
        return f"unknown outcome {outcome!r}"
    if verdict not in VERDICTS:
        return f"unknown verdict {verdict!r}"
    if not isinstance(body.get("verified"), bool) or not isinstance(body.get("is_active"), bool):
        return "verified and is_active must be true or false"
    if body.get("basis") != basis:
        return f"answered from {body.get('basis')!r}, expected {basis}"
    if outcome == "verified":
        if body["verified"] is not True:
            return "outcome verified but verified is false"
        if not (isinstance(body.get("observation_id"), str) and body["observation_id"]):
            return "a verified answer without an observation id"
    elif body["verified"] is True:
        return f"outcome {outcome} but verified is true"
    if outcome == "not_found" and verdict != "unknown":
        return f"not found, yet the verdict is {verdict}"
    action = body.get("action")
    if action != ACTION_FOR[verdict]:
        return f"verdict {verdict} with action {action!r}, expected {ACTION_FOR[verdict]}"
    if body.get("can_file") is not (action in CAN_FILE_ACTIONS):
        return f"action {action} with can_file {body.get('can_file')!r}"
    return None


def _answer(body: dict) -> dict:
    return {k: body.get(k) for k in ("verdict", "is_active", "can_file", "action")}


def _storable(body: dict) -> bool:
    """Whether the checker serves this answer from storage: verified, active, and not
    "not yet open" (which it re-checks live when the answer carries no date, and
    the API does not say whether it does)."""
    return body.get("outcome") == "verified" and body.get("is_active") is True and body.get("verdict") != "not_yet_open"


def judge(body: dict) -> str | None:
    """The problem with one well-formed answer for a pool entity, or None."""
    if body["outcome"] != "verified":
        return f"expected a verified answer, got {body['outcome']} ({body.get('reason')})"
    return None


class Checker:
    def __init__(
        self,
        client: CheckerClient,
        *,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
        wall: Callable[[], float] = time.time,
        budget: float = BUDGET_SECONDS,
        rng: random.Random | None = None,
    ):
        self.client = client
        self.rng = rng or random.SystemRandom()
        self.sleep = sleep
        self.clock = clock
        self.wall = wall
        self.budget = budget
        self._deadline = 0.0

    def run(self, state: State, pool: Pool, *, count: int = COUNT) -> Report:
        """Never raises: whatever goes wrong becomes a problem in the report."""
        report = Report(state.code.upper(), state.name, _iso(self.wall()))
        self._deadline = self.clock() + self.budget
        if isinstance(self.client, CheckerClient):
            self.client.deadline = time.monotonic() + self.budget
        try:
            self._run(state, report, pool, count)
        except OutOfTime as e:
            report.problems.append(f"stopped: the check's {round(self.budget / 60)}-minute budget ran out before {e}")
        except Exception as e:  # noqa: BLE001  reported as a failure, never lost
            report.problems.append(f"the check itself failed: {type(e).__name__}: {e}")
        report.finished_at = _iso(self.wall())
        return report

    def _time(self, before: str) -> None:
        if self.clock() >= self._deadline:
            raise OutOfTime(before)

    def _run(self, state: State, report: Report, pool: Pool, count: int) -> None:
        cfg = self.client.config()
        if not cfg.ok:
            report.problems.append(f"the checker's configuration could not be read: {_error(cfg)}")
            return
        listed = cfg.body.get("states")
        entries = [s for s in listed if isinstance(s, dict)] if isinstance(listed, list) else []
        entry = next((s for s in entries if str(s.get("state", "")).lower() == state.code), None)
        if entry is None:
            report.problems.append(f"the checker's configuration does not list {report.state}")
            return
        served = entry.get("served")
        report.served = served if isinstance(served, bool) else None
        report.checker_status = str(entry.get("status") or "") or None
        report.checker_note = str(entry.get("status_note") or "") or None
        if report.served is None:
            report.problems.append(f"the configuration's served flag is {served!r}, not true or false")
            return
        if report.served != state.served:
            want = "served" if state.served else "not served"
            report.problems.append(
                f"the checker says {report.state} is {'served' if report.served else 'not served'}, "
                f"but it should be {want} (canary/states.py)"
            )
            return
        if len(pool.ids) < count or not pool.active:
            report.problems.append(
                f"the pool for {report.state} has {len(pool.ids)} ids ({len(pool.active)} active); "
                f"a batch needs {count}, one of them active"
            )
            return
        # One that was active, so storage should serve it (the round trip), then the
        # rest from the whole pool; all at random.
        first = self.rng.choice(pool.active)
        picked = [first, *self.rng.sample([i for i in pool.ids if i != first], count - 1)]
        if not state.served:
            self._not_served(state, report, picked[0])
            return
        for i, eid in enumerate(picked):
            if i:
                self.sleep(self.rng.uniform(*SPACING_SECONDS))
            self._time(f"checking {eid}")
            report.cases.append(self._case(state, eid))
        report.problems += [f"{c.eid}: {c.problem}" for c in report.cases if c.problem]
        self._round_trip(state, report)

    def _not_served(self, state: State, report: Report, eid: str) -> None:
        self._time(f"checking {eid}")
        r = self.client.lookup(state.code, eid, live=False)
        result = CaseResult(eid, "unsupported", seconds=r.seconds, attempts=1)
        report.cases.append(result)
        if not r.ok:
            report.problems.append(f"{eid}: no answer from the checker: {_error(r)}")
            return
        result.outcome, result.verdict = r.body.get("outcome"), r.body.get("verdict")
        if result.outcome != "unsupported":
            report.problems.append(f"{eid}: {report.state} should not be served, but it answered as {result.outcome}")
            return
        note = f": {report.checker_note}" if report.checker_note else ""
        report.warnings.append(f"{report.state} is not served by the checker{note}")

    def _case(self, state: State, eid: str) -> CaseResult:
        result = CaseResult(eid, "verified")
        for attempt in (1, 2):
            if attempt == 2:
                self.sleep(RETRY_PAUSE_SECONDS)
                self._time(f"retrying {eid}")
            result.attempts = attempt
            r = self.client.lookup(state.code, eid, live=True)
            result.seconds = r.seconds
            result.outcome = result.verdict = result.reason = result.observation_id = result.is_active = None
            result.answer = result.answered_at = None
            if not r.ok:
                result.problem = f"no answer from the checker: {_error(r)}"
                continue
            body = r.body
            bad = malformed(body, state, eid, basis="live")
            if bad:
                result.problem = f"a malformed answer: {bad}"
                return result
            result.outcome, result.verdict, result.reason = body["outcome"], body["verdict"], body.get("reason")
            result.is_active, result.observation_id = body["is_active"], body.get("observation_id")
            result.answer, result.answered_at = _answer(body), self.clock()
            if result.outcome in TRANSIENT_OUTCOMES:
                result.problem = f"the registry did not answer ({result.reason}), twice"
                continue
            result.problem = judge(body)
            return result
        return result

    def _round_trip(self, state: State, report: Report) -> None:
        live = next((c for c in report.cases if not c.problem and c.answer and _storable(
            {"outcome": c.outcome, **c.answer})), None)  # fmt: skip
        if live is None:
            report.round_trip = {"checked": False}
            report.warnings.append(
                "storage round trip not checked: no answer in this run was one the checker serves from storage "
                "(it re-checks inactive entities live by design; not-yet-open ones are skipped because the "
                "answer does not say whether storage may serve them)"
            )
            return
        # Observations this run produced for the entity, with their verdicts: the
        # live answer, and any probe below that missed storage and went live
        # (which saves a newer one). Serving any of them back proves the round trip.
        ours = {live.observation_id: live.answer}
        # When this run's newest request started: its observation was fetched at or
        # after this, so a foreign one younger than this may be newer than ours.
        asked = live.answered_at - (live.seconds or 0)
        pauses = list(ROUND_TRIP_PAUSES)
        extra_reads = 2  # a refresh on the last probe still gets read back, at most twice
        trip: dict = {"checked": True, "eid": live.eid, "observation_id": live.observation_id, "tries": []}
        report.round_trip = trip

        def take_live(r: Response, started: float, what: str, *, read_back: bool) -> bool:
            """Judge a live answer met during the round trip (a probe that missed storage,
            or a refresh). False ends the round trip: failed, or inconclusive when the
            entity changed into an answer storage never serves. A verified answer joins
            this run's observations; a transient failure is just a missed try."""
            nonlocal asked, extra_reads
            if not r.ok:
                return True
            body = r.body
            bad = malformed(body, state, live.eid, basis="live")
            if bad or body["outcome"] not in ("verified", *TRANSIENT_OUTCOMES):
                why = f"a malformed answer: {bad}" if bad else f"the answer {body['outcome']}"
                report.problems.append(f"{live.eid}: a {what} got {why}")
                trip["passed"] = False
                return False
            if body["outcome"] != "verified":
                return True
            if not _storable(body):
                trip["passed"] = None
                report.warnings.append(
                    f"storage round trip inconclusive: {live.eid} now answers {body['verdict']} "
                    f"(active {body['is_active']}), which the checker re-checks live"
                )
                return False
            ours[body["observation_id"]] = _answer(body)
            asked = started
            if read_back and not pauses and extra_reads:
                extra_reads -= 1
                pauses.append(ROUND_TRIP_PAUSES[-1])
            return True

        while pauses:
            self.sleep(pauses.pop(0))
            self._time(f"the storage round trip for {live.eid}")
            started = self.clock()
            r = self.client.lookup(state.code, live.eid, live=False)
            body = r.body if r.ok else {}
            tried = {"basis": body.get("basis"), "observation_id": body.get("observation_id"),
                     "age_seconds": body.get("age_seconds"), "verdict": body.get("verdict"),
                     "error": None if r.ok else _error(r)}  # fmt: skip
            trip["tries"].append(tried)
            if not r.ok:
                continue
            if body.get("basis") == "live":
                # Missed storage and asked the registry, which saves a newer one. No
                # extra read for it: probes that keep missing storage mean R2 is failing.
                if not take_live(r, started, "storage probe", read_back=False):
                    return
                continue
            bad = malformed(body, state, live.eid, basis="store")
            if bad:
                report.problems.append(f"{live.eid}: storage gave a malformed answer: {bad}")
                trip["passed"] = False
                return
            if body["outcome"] != "verified":
                report.problems.append(f"{live.eid}: storage served an unverified answer ({body['outcome']})")
                trip["passed"] = False
                return
            stored_id, age = body["observation_id"], body.get("age_seconds")
            if not (isinstance(age, int) and not isinstance(age, bool) and age >= 0):
                report.problems.append(f"{live.eid}: storage gave an age of {age!r}")
                trip["passed"] = False
                return
            if stored_id not in ours:
                # Possibly fetched after ours: another run (a branch test, a user) saved
                # over it. Ask live once more, so storage gets one of ours again. It may
                # even be inactive: the checker serves a stored inactive answer when its
                # live re-check fails, by design, so that alone proves nothing wrong.
                if age < self.clock() - asked:
                    self._time(f"the storage round trip for {live.eid}")
                    started = self.clock()
                    again = self.client.lookup(state.code, live.eid, live=True)
                    if not take_live(again, started, "live refresh", read_back=True):
                        return
                continue  # otherwise an older one: this run's has not landed yet
            stored, saved = _answer(body), ours[stored_id]
            changed = [f"{k} {saved[k]} -> {stored[k]}" for k in saved if stored[k] != saved[k]]
            trip["passed"] = not changed
            if changed:
                report.problems.append(
                    f"{live.eid}: storage served observation {stored_id} changed: {', '.join(changed)}"
                )
            return
        trip["passed"] = False
        report.problems.append(
            f"{live.eid}: storage did not serve this run's answer back after {len(trip['tries'])} tries "
            "(saving to or reading from R2 is failing or too slow)"
        )


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, UTC).isoformat(timespec="seconds")


def markdown(report: Report, run_url: str | None = None) -> str:
    icon = "✅" if report.ok else "❌"
    lines = [f"## {icon} {report.state} ({_cell(report.name)})", ""]
    note = f", {_cell(report.checker_note)}" if report.checker_note else ""
    lines.append(f"Checker status: **{_cell(report.checker_status or 'unknown')}**{note}")
    if report.cases:
        lines += [
            "",
            "| Entity ID | Expected | Outcome | Verdict | Reason | Seconds | Tries | Result |",
            "|---|---|---|---|---|---|---|---|",
        ]
    for c in report.cases:
        result = f"❌ {c.problem}" if c.problem else "✅"
        seconds = c.seconds if c.seconds is not None else ""
        lines.append(
            f"| `{_cell(c.eid)}` | {_cell(c.expect)} | {_cell(c.outcome)} | {_cell(c.verdict)} | "
            f"{_cell(c.reason)} | {seconds} | {c.attempts} | {_cell(result)} |"
        )
    trip = report.round_trip or {}
    if trip.get("checked"):
        tries = ", ".join(
            f"{t['basis'] or t['error']}" + (f" ({t['age_seconds']}s old)" if t["basis"] == "store" else "")
            for t in trip["tries"]
        )
        mark = {True: "✅", False: "❌"}.get(trip.get("passed"), "⚠️ inconclusive")
        lines += ["", f"Storage round trip (`{_cell(trip['eid'])}`): {mark} {_cell(tries) or 'not reached'}"]
    for title, items in (("Problems", report.problems), ("Warnings", report.warnings)):
        if items:
            lines += ["", f"**{title}**", *[f"- {_cell(i)}" for i in items]]
    if run_url:
        lines += ["", f"[Run log and report]({run_url})"]
    return "\n".join(lines) + "\n"


def _cell(value) -> str:
    return "" if value is None else str(value).replace("|", "\\|").replace("\r", " ").replace("\n", " ")
