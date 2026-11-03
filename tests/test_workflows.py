from __future__ import annotations

import re
from pathlib import Path

from canary import states, workflows
from canary.gha import annotate, commands_off

ROOT = Path(__file__).resolve().parent.parent
SHA_PIN = re.compile(r"uses:\s+([\w.-]+/[\w.-]+)@([0-9a-f]{40})\s+#\s+v\d")


def test_every_state_has_an_up_to_date_workflow():
    assert workflows.drift() == []


def test_25_states_with_unique_codes():
    assert len(states.STATES) == 25
    assert len(states.BY_CODE) == 25


def test_schedules_are_distinct_off_the_hour_and_inside_the_interval():
    starts = []
    for s in states.STATES:
        minute, hours, *_ = states.cron(s).split()
        first_hour = int(hours.split("-")[0])
        assert hours.endswith(f"-23/{states.INTERVAL_HOURS}")
        assert first_hour < states.INTERVAL_HOURS and minute != "0"
        starts.append(first_hour * 60 + int(minute))
    assert len(set(starts)) == len(starts)
    assert max(starts) < states.INTERVAL_HOURS * 60


def test_every_third_party_action_is_pinned_to_a_full_sha():
    for f in sorted((ROOT / ".github" / "workflows").glob("*.yml")):
        for line in f.read_text().splitlines():
            if "uses:" in line and not line.strip().startswith("uses: ./"):
                assert SHA_PIN.search(line), f"{f.name}: not pinned to a SHA with a version comment: {line.strip()}"


def test_workflows_default_to_no_permissions():
    for f in sorted((ROOT / ".github" / "workflows").glob("*.yml")):
        assert "\npermissions: {}\n" in f.read_text(), f.name


def test_no_expression_is_expanded_inside_a_run_script():
    for f in sorted((ROOT / ".github" / "workflows").glob("*.yml")):
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


def test_concurrency_is_per_branch():
    for s in states.STATES:
        assert f"group: state-{s.code}-${{{{ github.ref }}}}" in workflows.path(s).read_text()


def test_the_check_step_times_out_before_the_job():
    text = (ROOT / ".github" / "workflows" / "check-state.yml").read_text()
    step = re.search(r"id: check\n(?:.*\n)*?\s+timeout-minutes: (\d+)", text)
    job = re.search(r"\n    timeout-minutes: (\d+)", text)
    assert step and job and int(step.group(1)) < int(job.group(1))
