"""python -m canary plan | check | batch   (see --help on each)."""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.parse
from datetime import UTC, datetime
from pathlib import Path

from . import batch, gha, issue, pools, publish, states
from .check import COUNT, Checker, markdown
from .client import CheckerClient, base_url


def _run_url() -> str | None:
    server, repo, run = (os.environ.get(k) for k in ("GITHUB_SERVER_URL", "GITHUB_REPOSITORY", "GITHUB_RUN_ID"))
    return f"{server}/{repo}/actions/runs/{run}" if server and repo and run else None


def _count(text: str) -> int:
    """How many entities to look up per state: 1 to 20 (the pools hold 200)."""
    try:
        n = int(text.strip() or COUNT)
    except ValueError:
        raise argparse.ArgumentTypeError(f"expected a whole number, got {text!r}") from None
    if not 1 <= n <= 20:
        raise argparse.ArgumentTypeError(f"expected 1 to 20, got {n}")
    return n


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


def cmd_plan(args) -> int:
    """The batch's states as a JSON list for the job matrix: all, or a comma list."""
    wanted = args.states.strip().lower()
    if wanted in ("", "all"):
        chosen = list(states.STATES)
    else:
        chosen = list(dict.fromkeys(states.get(c) for c in wanted.split(",") if c.strip()))
    if not chosen:
        raise SystemExit(f"no states in {args.states!r}")
    print(json.dumps([{"code": s.code, "name": f"{s.code.upper()} {s.name}"} for s in chosen]))
    return 0


def cmd_check(args) -> int:
    state = states.get(args.state)
    base = base_url(os.environ.get("CHECKER_BASE_URL"))
    host = urllib.parse.urlsplit(base).hostname or ""
    gha.mask(host)  # the host alone, as error messages print it
    hidden = sorted({base, urllib.parse.urlsplit(base).netloc, host} - {""}, key=len, reverse=True)
    raw = Checker(CheckerClient(base)).run(state, pools.load(state), count=args.count).to_dict()
    report = batch.report_from(redact(raw, hidden))
    if args.json:
        Path(args.json).parent.mkdir(parents=True, exist_ok=True)
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


def cmd_batch(args) -> int:
    """Read every planned state's folder, write the batch summary, sync the issues."""
    planned = [states.get(s["code"]) for s in json.loads(args.plan)]
    results = [batch.load(s, Path(args.reports) / s.code) for s in planned]
    run_url = _run_url()
    when = datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC")
    text = batch.summary(results, f"State checks, {when}", run_url)
    with gha.commands_off():
        print(text)
    if args.summary:
        with open(args.summary, "a", encoding="utf-8") as f:
            f.write(text)
    errors: list[str] = []
    if args.issues:
        gh = issue.GitHub(os.environ.get("GITHUB_REPOSITORY", ""), os.environ.get("GH_TOKEN", ""))
        owner = os.environ.get("GITHUB_REPOSITORY_OWNER") or None
        link = run_url or "(run link unavailable)"
        for r in results:
            body = f"{r.body}\n[Batch run]({link})\n"
            try:
                did = issue.sync(gh, r.state, failed=r.failed, body=body, run_url=link, assignee=owner)
            except issue.GitHubError as e:  # one state's issue must not stop the others'
                errors.append(f"{r.state.code.upper()}: {e}")
                continue
            print(f"{r.state.code.upper()}: {did}")
    failed = [r.state.code.upper() for r in results if r.failed]
    if failed:
        gha.annotate("error", "State checks failed", f"{len(failed)} of {len(results)}: {', '.join(failed)}")
    for e in errors:
        gha.annotate("error", "Issue not updated", e)
    pushed = _publish(results, run_url) if args.publish else True
    # Each failing state's own job is red already; this job fails on its own errors.
    return 1 if errors or not pushed else 0


def _publish(results: list[batch.StateResult], run_url: str | None) -> bool:
    """Push the batch to the checker's console; False, and an error annotation, if it did not arrive."""
    try:
        base = base_url(os.environ.get("CHECKER_BASE_URL"))
        secret = os.environ.get("CANARY_PUSH_SECRET", "")
        if not secret:
            raise publish.PushError("CANARY_PUSH_SECRET is not set")
        if not run_url:
            raise publish.PushError("no run link to send")
        host = urllib.parse.urlsplit(base).hostname or ""
        gha.mask(host)
        hidden = sorted({base, urllib.parse.urlsplit(base).netloc, host} - {""}, key=len, reverse=True)
        data = publish.payload(
            results,
            at=publish.checked_at(results, datetime.now(UTC).isoformat(timespec="seconds")),
            run_url=run_url,
            event=os.environ.get("GITHUB_EVENT_NAME") or "unknown",
        )
        publish.push(base, secret, redact(data, hidden))
    except (publish.PushError, SystemExit) as e:  # base_url() exits on a bad address
        gha.annotate("error", "Batch not pushed to the console", str(e))
        return False
    print("pushed to the console")
    return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m canary")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("plan", help="print the batch's states as a JSON matrix")
    p.add_argument("--states", default="all", help='"all" or a comma list of codes, e.g. "oh,wa"')
    p.set_defaults(func=cmd_plan)

    p = sub.add_parser("check", help="check one state through the deployed checker (env CHECKER_BASE_URL)")
    p.add_argument("--state", required=True)
    p.add_argument("--count", type=_count, default=COUNT, help=f"entities to look up from the pool (default {COUNT})")
    p.add_argument("--json", help="write the report here")
    p.add_argument("--summary", help="append the markdown summary here (GITHUB_STEP_SUMMARY)")
    p.set_defaults(func=cmd_check)

    p = sub.add_parser("batch", help="summarize a batch and sync each state's issue (env GH_TOKEN, GITHUB_REPOSITORY)")
    p.add_argument("--plan", required=True, help="the JSON printed by `plan`")
    p.add_argument("--reports", required=True, help="folder with one sub-folder per state code")
    p.add_argument("--summary", help="append the batch summary here (GITHUB_STEP_SUMMARY)")
    p.add_argument("--issues", action="store_true", help="open, comment on or close each state's issue")
    p.add_argument(
        "--publish",
        action="store_true",
        help="push the results to the checker's console (env CHECKER_BASE_URL, CANARY_PUSH_SECRET)",
    )
    p.set_defaults(func=cmd_batch)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
