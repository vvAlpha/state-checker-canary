"""python -m canary check | issue | workflows   (see --help on each)."""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.parse
from pathlib import Path

from . import gha, issue, states, workflows
from .check import CaseResult, Checker, Report, markdown
from .client import CheckerClient, base_url


def _run_url() -> str | None:
    server, repo, run = (os.environ.get(k) for k in ("GITHUB_SERVER_URL", "GITHUB_REPOSITORY", "GITHUB_RUN_ID"))
    return f"{server}/{repo}/actions/runs/{run}" if server and repo and run else None


def _bool(text: str) -> bool:
    if text.strip().lower() in ("true", "1", "yes"):
        return True
    if text.strip().lower() in ("false", "0", "no", ""):
        return False
    raise argparse.ArgumentTypeError(f"expected true or false, got {text!r}")


def redact(value, secrets_: list[str]):
    """`value` with every occurrence of the secrets replaced, through lists and dicts.
    The runner masks its log, but not the report artifact or the issue text."""
    if isinstance(value, str):
        for s in secrets_:
            value = value.replace(s, "<checker>")
        return value
    if isinstance(value, list):
        return [redact(v, secrets_) for v in value]
    if isinstance(value, dict):
        return {k: redact(v, secrets_) for k, v in value.items()}
    return value


def cmd_check(args) -> int:
    state = states.get(args.state)
    base = base_url(os.environ.get("CHECKER_BASE_URL"))
    host = urllib.parse.urlsplit(base).hostname or ""
    gha.mask(host)  # the host alone, as error messages print it
    hidden = sorted({base, urllib.parse.urlsplit(base).netloc, host} - {""}, key=len, reverse=True)
    report = _report_from(redact(Checker(CheckerClient(base)).run(state, all_cases=args.all_cases).to_dict(), hidden))
    if args.json:
        Path(args.json).write_text(json.dumps(report.to_dict(), indent=1) + "\n")
    text = markdown(report, _run_url())
    with gha.commands_off():
        print(text)
    if args.summary:
        with open(args.summary, "a", encoding="utf-8") as f:
            f.write(text)
    title = f"{report.state} check"
    for p in report.problems:
        gha.annotate("error", title, p)
    for w in report.warnings:
        gha.annotate("warning", title, w)
    return 0 if report.ok else 1


def cmd_issue(args) -> int:
    state = states.get(args.state)
    run_url = _run_url() or "(run link unavailable)"
    try:
        report = _report_from(json.loads(Path(args.report).read_text()))
        body = markdown(report, run_url)
        # From the report's problems, never its stored `ok`, and red if the step was.
        failed = not report.ok or args.check_outcome != "success"
    except Exception as e:  # noqa: BLE001  any unusable report is a failure to report, not a crash
        body = (
            f"## ❌ {state.code.upper()} ({state.name})\n\nThe check did not write a usable report "
            f"({type(e).__name__}); it failed before or while checking. [Run log]({run_url})\n"
        )
        failed = True
    gh = issue.GitHub(os.environ.get("GITHUB_REPOSITORY", ""), os.environ.get("GH_TOKEN", ""))
    owner = os.environ.get("GITHUB_REPOSITORY_OWNER") or None
    print(issue.sync(gh, state, failed=failed, body=body, run_url=run_url, assignee=owner))
    return 0


def _report_from(data) -> Report:
    """A Report from its to_dict() form; ValueError when the shape is wrong."""
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
    trip = report.round_trip if isinstance(report.round_trip, dict) else {}
    if not report.problems and (any(c.problem for c in cases) or trip.get("passed") is False):
        raise ValueError("a case or the round trip failed, but the report lists no problem")
    return report


def cmd_workflows(args) -> int:
    if args.write:
        changed = workflows.write()
        print("\n".join(f"wrote {n}" for n in changed) or "all workflows up to date")
        stale = [p for p in workflows.drift() if p.endswith("no such state in canary/states.py")]
        for p in stale:
            print(f"remove by hand: {p}")
        return 1 if stale else 0
    problems = workflows.drift()
    for p in problems:
        gha.annotate("error", "Workflows out of date", f"{p}; run `python -m canary workflows --write`")
    return 1 if problems else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m canary")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("check", help="check one state through the deployed checker (env CHECKER_BASE_URL)")
    p.add_argument("--state", required=True)
    p.add_argument("--all-cases", type=_bool, default=False, help="check every sample id (true/false)")
    p.add_argument("--json", help="write the report here")
    p.add_argument("--summary", help="append the markdown summary here (GITHUB_STEP_SUMMARY)")
    p.set_defaults(func=cmd_check)

    p = sub.add_parser("issue", help="open, update or close the state's issue (env GH_TOKEN, GITHUB_REPOSITORY)")
    p.add_argument("--state", required=True)
    p.add_argument("--report", required=True)
    p.add_argument("--check-outcome", required=True, help="the check step's outcome: success, failure, ...")
    p.set_defaults(func=cmd_issue)

    p = sub.add_parser("workflows", help="verify (default) or --write the per-state workflow files")
    p.add_argument("--write", action="store_true")
    p.set_defaults(func=cmd_workflows)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
