from __future__ import annotations

import re
from pathlib import Path

from canary import states
from canary.gha import annotate, commands_off

ROOT = Path(__file__).resolve().parent.parent
WORKFLOWS = sorted((ROOT / ".github" / "workflows").glob("*.yml"))
CHECK = (ROOT / ".github" / "workflows" / "check.yml").read_text()
SHA_PIN = re.compile(r"uses:\s+([\w.-]+/[\w.-]+)@([0-9a-f]{40})\s+#\s+v\d")


def test_25_states_with_unique_codes():
    assert len(states.STATES) == 25
    assert len(states.BY_CODE) == 25


def test_the_batch_runs_every_interval_off_the_hour():
    minute, hours = re.search(r'cron: "(\d+) (\S+) \* \* \*"', CHECK).groups()
    assert hours == f"*/{states.INTERVAL_HOURS}" and minute != "0"


def test_one_state_never_stops_another():
    assert "fail-fast: false" in CHECK
    assert "state: ${{ fromJSON(needs.plan.outputs.states) }}" in CHECK


def test_only_the_summary_job_can_write_issues():
    jobs = CHECK.split("\njobs:\n", 1)[1]
    blocks = re.split(r"\n  (?=[a-z]+:\n)", jobs)
    writers = [b.split(":", 1)[0].strip() for b in blocks if "issues: write" in b]
    assert writers == ["summary"]


def test_every_third_party_action_is_pinned_to_a_full_sha():
    for f in WORKFLOWS:
        for line in f.read_text().splitlines():
            if "uses:" in line and not line.strip().startswith("uses: ./"):
                assert SHA_PIN.search(line), f"{f.name}: not pinned to a SHA with a version comment: {line.strip()}"


def test_workflows_default_to_no_permissions():
    for f in WORKFLOWS:
        assert "\npermissions: {}\n" in f.read_text(), f.name


def test_no_expression_is_expanded_inside_a_run_script():
    for f in WORKFLOWS:
        in_run = False
        for line in f.read_text().splitlines():
            stripped = line.strip()
            if stripped.startswith("run:"):
                in_run = True
                assert "${{" not in stripped, f"{f.name}: {stripped}"
                continue
            if in_run and stripped and not line.startswith(" " * 10):
                in_run = False
            if in_run:
                assert "${{" not in line, f"{f.name}: {stripped}"


def test_the_check_step_times_out_before_its_job():
    step = re.search(r"id: check\n(?:.*\n)*?\s+timeout-minutes: (\d+)", CHECK)
    job = re.search(r"\n  check:\n(?:.*\n)*?    timeout-minutes: (\d+)", CHECK)
    assert step and job and int(step.group(1)) < int(job.group(1))


def test_annotations_escape_newlines_and_separators(capsys):
    annotate("error", "a:b,c", "line1\nline2%")
    assert capsys.readouterr().out == "::error title=a%3Ab%2Cc::line1%0Aline2%25\n"


def test_annotations_break_legacy_commands(capsys):
    annotate("warning", "t", "x ##[stop-commands]tok")
    assert "##[" not in capsys.readouterr().out


def test_commands_off_wraps_output_in_a_fresh_token(capsys):
    with commands_off():
        print("##[error]not a command")
    first, body, last = capsys.readouterr().out.splitlines()
    token = first.removeprefix("::stop-commands::")
    assert len(token) == 32 and last == f"::{token}::" and body == "##[error]not a command"
    with commands_off():
        pass
    assert capsys.readouterr().out.splitlines()[0] != first
