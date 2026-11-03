"""One batch: every state's report, read back after all the state jobs ran.

Builds the batch summary (one table, then each state's detail) and keeps each
state's issue in step. A state whose job left no usable report, or whose check
step did not succeed, counts as failed.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from .check import CaseResult, Report, markdown
from .states import State


@dataclass
class StateResult:
    state: State
    report: Report | None  # None: no usable report
    failed: bool
    why: str = ""  # why it failed beyond the report's own problems: no report, or a failed step

    @property
    def body(self) -> str:
        """The state's detail, as its issue and the batch summary show it."""
        if self.report is None:
            return f"## ❌ {self.state.code.upper()} ({self.state.name})\n\nNo usable report: {self.why}.\n"
        text = markdown(self.report)
        return f"{text}\n**Failed:** {self.why}.\n" if self.why else text


def report_from(data) -> Report:
    """A Report from its to_dict() form; ValueError when the shape is wrong or its
    parts contradict its problem list."""
    if not isinstance(data, dict) or not isinstance(data.get("cases"), list):
        raise ValueError("not a report")
    if not all(isinstance(c, dict) for c in data["cases"]):
        raise ValueError("a case is not an object")
    for key in ("problems", "warnings"):
        if not isinstance(data.get(key), list) or not all(isinstance(i, str) for i in data[key]):
            raise ValueError(f"{key} is not a list of text")
    cases = [CaseResult(**{k: v for k, v in c.items() if k in CaseResult.__dataclass_fields__}) for c in data["cases"]]
    fields = {k: v for k, v in data.items() if k in Report.__dataclass_fields__ and k != "cases"}
    report = Report(cases=cases, **fields)
    trip = report.round_trip
    if trip is not None:
        tries = trip.get("tries", []) if isinstance(trip, dict) else None
        if (
            not isinstance(trip, dict)
            or not isinstance(trip.get("checked"), bool)
            or trip.get("passed") not in (True, False, None)
            or not isinstance(tries, list)
            or not all(isinstance(t, dict) and {"basis", "error", "age_seconds"} <= t.keys() for t in tries)
        ):
            raise ValueError("round_trip is malformed")
    trip = trip or {}
    if not report.problems and (any(c.problem for c in cases) or trip.get("passed") is False):
        raise ValueError("a case or the round trip failed, but the report lists no problem")
    return report


def load(state: State, folder: Path) -> StateResult:
    """The state's result from its job's folder: report.json and outcome.txt (the
    check step's outcome: success, failure, ...)."""
    try:
        report = report_from(json.loads((folder / "report.json").read_text()))
    except Exception as e:  # noqa: BLE001  any unusable report is a failure to show, not a crash
        reason = "the job left no report" if isinstance(e, FileNotFoundError) else f"{type(e).__name__}: {e}"
        return StateResult(state, None, True, f"{reason} (it failed before or while checking)")
    try:
        outcome = (folder / "outcome.txt").read_text().strip()
    except OSError:
        outcome = "unknown"
    # From the report's problems, never a stored `ok`, and red if the step was.
    # Named only when the report looks clean, as the report's problems say the rest.
    why = (
        ""
        if outcome == "success" or not report.ok
        else f"the check step ended as {outcome} (the report may be partial)"
    )
    return StateResult(state, report, not report.ok or outcome != "success", why)


def _cell(text: str) -> str:
    return text.replace("|", "\\|").replace("\n", " ")


def summary(results: list[StateResult], title: str, run_url: str | None = None) -> str:
    failed = [r for r in results if r.failed]
    head = f"## {'❌' if failed else '✅'} {title}: {len(results) - len(failed)} of {len(results)} states passed"
    lines = [head, "", "| State | Result | Live answers | Storage round trip | First problem or warning |",
             "|---|---|---|---|---|"]  # fmt: skip
    for r in results:
        rep = r.report
        if rep is None:
            lines.append(f"| {r.state.code.upper()} {r.state.name} | ❌ | | | {_cell(r.why)} |")
            continue
        good = sum(1 for c in rep.cases if not c.problem)
        trip = rep.round_trip or {}
        storage = "not checked"
        if trip.get("checked"):
            storage = {True: "✅", False: "❌"}.get(trip.get("passed"), "⚠️ inconclusive")
        note = (rep.problems or ([r.why] if r.why else []) or rep.warnings or [""])[0]
        mark = "❌" if r.failed else ("⚠️" if rep.warnings else "✅")
        answers = f"{good} of {len(rep.cases)}" if rep.cases else "not checked"
        lines.append(f"| {r.state.code.upper()} {r.state.name} | {mark} | {answers} | {storage} | {_cell(note)} |")
    if run_url:
        lines += ["", f"[This batch's run]({run_url})"]
    lines += ["", "### Each state", ""]
    for r in results:
        lines += ["<details>", f"<summary>{r.state.code.upper()} {r.state.name}: {'failed' if r.failed else 'passed'}"
                  "</summary>", "", r.body, "</details>", ""]  # fmt: skip
    return "\n".join(lines) + "\n"
