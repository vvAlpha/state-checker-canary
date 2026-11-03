"""Check one state end to end through the deployed checker.

1. Read the checker's configuration: the state's sample ids from its regression
   pack, each with the answer it is expected to give, and whether it is served.
   Whether it *should* be served is this repository's call (states.py), so a
   checker that drops a state by mistake fails instead of excusing itself.
2. Look up this run's samples live (`?live=true`: the checker scrapes the state's
   registry now). Every answer must be well formed, say it was live, and give
   the expected outcome: "verified" with a verdict, or "not_found" for an id that
   does not exist.
3. Round trip through storage: right after a live answer, a normal lookup must be
   served from storage (R2) with an observation this run produced, which proves
   answers are saved and read back.

What fails the run (someone has to act):
- the checker unreachable, or its configuration unreadable or short of samples;
- a state served or not served against what states.py says;
- an answer that is malformed, not live when asked live, or has the wrong outcome;
- the registry not answering, twice in a row;
- storage not serving this run's answer back, or serving it back different;
- the run's time budget running out, or the check itself crashing.

What only warns (shown in the summary, the run stays green):
- a verdict that moved (an entity filed or was dissolved since the pack was
  recorded: the pack wants updating, the checker is working; the same rule as
  the checker's own regression tool);
- a state the checker is meant not to serve;
- no answer in this run that storage would serve (inactive entities, and ones
  not open yet, are re-checked live by design).
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime

from .client import CheckerClient, Response
from .states import INTERVAL_HOURS, State

# A registry that did not answer gets one more try after this pause, so a
# session being renewed or a proxy line resting does not page anyone.
RETRY_PAUSE_SECONDS = 60
# R2 writes land 3 to 8 seconds after the answer (measured 2026-11-03, 25 states);
# the round trip asks after the first pause, then after each later one.
ROUND_TRIP_PAUSES = (15, 20, 30)
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
# Expected verdicts of active entities, which the checker serves from storage.
ACTIVE_VERDICTS = ("eligible", "eligible_delinquent", "already_filed", "not_applicable")


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
    drift: str | None = None
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


def slot(now: float) -> int:
    """Which INTERVAL_HOURS-hour slot `now` falls in; picks the rotating sample."""
    return int(now // (INTERVAL_HOURS * 3600))


def plan(samples: list[dict], run_slot: int, all_cases: bool) -> list[dict]:
    """This run's samples. The first is the round-trip candidate: the first sample
    expected to be active (fixed, so every run tries a storage round trip). The
    second rotates through the rest, one per run, so a day of runs covers all of
    them. `all_cases` checks every sample."""
    if not samples:
        return []
    anchor = next((s for s in samples if s["expect"] in ACTIVE_VERDICTS), None)
    anchor = anchor or next((s for s in samples if s["expect"] == "unknown"), None)
    anchor = anchor or next((s for s in samples if s["expect"] != "not_found"), samples[0])
    rest = [s for s in samples if s is not anchor]
    if all_cases:
        return [anchor, *rest]
    return [anchor, rest[run_slot % len(rest)]] if rest else [anchor]


def samples_of(entry: dict) -> tuple[list[dict], str | None]:
    """The entry's samples, or a problem: each needs a text id and a known expected
    answer, and a served state needs at least one existing and one made-up id."""
    raw = entry.get("samples")
    if not isinstance(raw, list):
        return [], "its sample list is missing or not a list"
    samples = []
    for s in raw:
        if not (isinstance(s, dict) and isinstance(s.get("eid"), str) and s["eid"].strip()):
            return [], f"a sample has no usable id: {str(s)[:80]}"
        if s.get("expect") not in VERDICTS | {"not_found"}:
            return [], f"sample {s['eid']} expects {s.get('expect')!r}, not a known answer"
        samples.append({"eid": s["eid"], "expect": s["expect"]})
    return samples, None


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


def judge(expect: str, body: dict) -> tuple[str | None, str | None]:
    """(problem, drift) for one well-formed answer."""
    outcome, verdict = body["outcome"], body["verdict"]
    if expect == "not_found":
        return (None if outcome == "not_found" else f"expected not found, got {outcome} ({verdict})"), None
    if outcome != "verified":
        return f"expected a verified answer, got {outcome} ({body.get('reason')})", None
    return None, (f"verdict {expect} -> {verdict}" if verdict != expect else None)


class Checker:
    def __init__(
        self,
        client: CheckerClient,
        *,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
        wall: Callable[[], float] = time.time,
        budget: float = BUDGET_SECONDS,
    ):
        self.client = client
        self.sleep = sleep
        self.clock = clock
        self.wall = wall
        self.budget = budget
        self._deadline = 0.0

    def run(self, state: State, *, all_cases: bool = False) -> Report:
        """Never raises: whatever goes wrong becomes a problem in the report."""
        report = Report(state.code.upper(), state.name, _iso(self.wall()))
        self._deadline = self.clock() + self.budget
        try:
            self._run(state, report, all_cases)
        except OutOfTime as e:
            report.problems.append(f"stopped: the check's {round(self.budget / 60)}-minute budget ran out before {e}")
        except Exception as e:  # noqa: BLE001  reported as a failure, never lost
            report.problems.append(f"the check itself failed: {type(e).__name__}: {e}")
        report.finished_at = _iso(self.wall())
        return report

    def _time(self, before: str) -> None:
        if self.clock() >= self._deadline:
            raise OutOfTime(before)

    def _run(self, state: State, report: Report, all_cases: bool) -> None:
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
        samples, bad = samples_of(entry)
        if bad:
            report.problems.append(f"the configuration's samples for {report.state} are unusable: {bad}")
            return
        made_up = any(s["expect"] == "not_found" for s in samples)
        storable = any(s["expect"] in (*ACTIVE_VERDICTS, "unknown") for s in samples)
        if state.served and not (made_up and storable):
            report.problems.append(
                f"the configuration needs a made-up sample id and one expected active (or unknown) "
                f"for {report.state}, to check not-found answers and storage"
            )
            return
        if not samples:
            report.problems.append(f"the checker's configuration has no sample ids for {report.state}")
            return
        cases = plan(samples, slot(self.wall()), all_cases)
        if not state.served:
            self._not_served(state, report, cases[0])
            return
        for case in cases:
            self._time(f"checking {case['eid']}")
            report.cases.append(self._case(state, case))
        for c in report.cases:
            if c.problem:
                report.problems.append(f"{c.eid}: {c.problem}")
            if c.drift:
                report.warnings.append(f"{c.eid}: {c.drift} (the regression pack wants updating)")
        self._round_trip(state, report)

    def _not_served(self, state: State, report: Report, case: dict) -> None:
        r = self.client.lookup(state.code, case["eid"], live=False)
        result = CaseResult(case["eid"], "unsupported", seconds=r.seconds, attempts=1)
        report.cases.append(result)
        if not r.ok:
            report.problems.append(f"{case['eid']}: no answer from the checker: {_error(r)}")
            return
        result.outcome, result.verdict = r.body.get("outcome"), r.body.get("verdict")
        if result.outcome != "unsupported":
            report.problems.append(
                f"{case['eid']}: {report.state} should not be served, but it answered as {result.outcome}"
            )
            return
        note = f": {report.checker_note}" if report.checker_note else ""
        report.warnings.append(f"{report.state} is not served by the checker{note}")

    def _case(self, state: State, case: dict) -> CaseResult:
        result = CaseResult(case["eid"], case["expect"])
        for attempt in (1, 2):
            if attempt == 2:
                self._time(f"retrying {case['eid']}")
                self.sleep(RETRY_PAUSE_SECONDS)
            result.attempts = attempt
            r = self.client.lookup(state.code, case["eid"], live=True)
            result.seconds = r.seconds
            result.outcome = result.verdict = result.reason = result.observation_id = result.is_active = None
            result.answer = result.answered_at = None
            if not r.ok:
                result.problem = f"no answer from the checker: {_error(r)}"
                continue
            body = r.body
            bad = malformed(body, state, case["eid"], basis="live")
            if bad:
                result.problem = f"a malformed answer: {bad}"
                return result
            result.outcome, result.verdict, result.reason = body["outcome"], body["verdict"], body.get("reason")
            result.is_active, result.observation_id = body["is_active"], body.get("observation_id")
            result.answer, result.answered_at = _answer(body), self.clock()
            if result.outcome in TRANSIENT_OUTCOMES:
                result.problem = f"the registry did not answer ({result.reason}), twice"
                continue
            result.problem, result.drift = judge(case["expect"], body)
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

        def adopt(r: Response, started: float, *, read_back: bool) -> None:
            nonlocal asked, extra_reads
            if r.ok and not malformed(r.body, state, live.eid, basis="live") and r.body["outcome"] == "verified":
                ours[r.body["observation_id"]] = _answer(r.body)
                asked = started
                if read_back and not pauses and extra_reads:
                    extra_reads -= 1
                    pauses.append(ROUND_TRIP_PAUSES[-1])

        while pauses:
            self._time(f"the storage round trip for {live.eid}")
            self.sleep(pauses.pop(0))
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
                if not malformed(body, state, live.eid, basis="live") and not _storable(body):
                    # The entity changed (dissolved, say) into an answer storage never serves.
                    trip["passed"] = None
                    report.warnings.append(
                        f"storage round trip inconclusive: {live.eid} now answers {body['verdict']} "
                        f"(active {body['is_active']}), which the checker re-checks live"
                    )
                    return
                # Missed storage and asked the registry, which saves a newer one. No
                # extra read for it: probes that keep missing storage mean R2 is failing.
                adopt(r, started, read_back=False)
                continue
            bad = malformed(body, state, live.eid, basis="store")
            if bad:
                report.problems.append(f"{live.eid}: storage gave a malformed answer: {bad}")
                trip["passed"] = False
                return
            stored_id, age = body["observation_id"], body.get("age_seconds")
            if not (isinstance(age, int) and not isinstance(age, bool) and age >= 0):
                report.problems.append(f"{live.eid}: storage gave an age of {age!r}")
                trip["passed"] = False
                return
            if stored_id not in ours:
                # Possibly fetched after ours: another run (a branch test, a user) saved
                # over it. Ask live once more, so storage gets one of ours again.
                if age < self.clock() - asked:
                    self._time(f"the storage round trip for {live.eid}")
                    started = self.clock()
                    adopt(self.client.lookup(state.code, live.eid, live=True), started, read_back=True)
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
        result = f"❌ {c.problem}" if c.problem else (f"⚠️ {c.drift}" if c.drift else "✅")
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
