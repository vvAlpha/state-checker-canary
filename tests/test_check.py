from __future__ import annotations

import json

import pytest

from canary import states
from canary.check import RETRY_PAUSE_SECONDS, Checker, malformed, markdown, plan, slot
from canary.client import Response
from tests.fakes import SAMPLES, Clock, FakeClient, answer, config

AZ = states.get("az")
IN = states.get("in")


def run(client, all_cases=False, clock=None, state=AZ, budget=1200):
    clock = clock or Clock(wall=0)
    checker = Checker(client, sleep=clock.sleep, clock=clock.clock, wall=clock.wall, budget=budget)
    return checker.run(state, all_cases=all_cases), clock


def _healthy(rotating_eid="100", rotating=None):
    return {
        ("200", True): [answer(verdict="eligible", obs="live1")],
        ("200", False): [answer(verdict="eligible", basis="store", age=16, obs="live1")],
        (rotating_eid, True): [rotating or answer(verdict="dead", active=False, obs="live9")],
    }


# planning


def test_plan_anchors_on_the_first_active_sample_and_rotates_the_rest():
    picked = [plan(SAMPLES, s, False) for s in range(3)]
    assert all(p[0]["eid"] == "200" for p in picked)  # "eligible": the first active expectation
    assert [p[1]["eid"] for p in picked] == ["100", "300", "999"]
    assert [s["eid"] for s in plan(SAMPLES, 0, True)] == ["200", "100", "300", "999"]


def test_plan_falls_back_when_no_sample_is_expected_active():
    samples = [{"eid": "9", "expect": "not_found"}, {"eid": "1", "expect": "dead"}]
    assert plan(samples, 0, False)[0]["eid"] == "1"
    assert plan([{"eid": "9", "expect": "not_found"}], 5, False) == [{"eid": "9", "expect": "not_found"}]
    assert plan([], 0, False) == []


def test_slot_changes_every_three_hours():
    assert slot(0) == slot(3 * 3600 - 1) == 0
    assert slot(3 * 3600) == 1


# healthy runs and policy


def test_a_healthy_state_passes_with_a_round_trip():
    report, clock = run(FakeClient(config(), _healthy()))
    assert report.ok, report.problems
    assert report.round_trip["passed"] is True
    assert [c.eid for c in report.cases] == ["200", "100"]
    assert clock.slept == [15]  # the first round-trip pause, no retries


def test_verdict_drift_warns_but_passes():
    report, _ = run(FakeClient(config(), _healthy(rotating=answer(verdict="eligible", obs="x"))))
    assert report.ok
    assert any("dead -> eligible" in w for w in report.warnings)


def test_a_wrong_outcome_fails():
    report, _ = run(FakeClient(config(), _healthy(rotating=answer(outcome="not_found", verdict="unknown"))))
    assert any(p.startswith("100: expected a verified answer, got not_found") for p in report.problems)


def test_not_found_expected_and_given_passes():
    clock = Clock(wall=2 * 3 * 3600)  # slot 2: the rotating pick is "999"
    answers = _healthy(rotating_eid="999", rotating=answer(outcome="not_found", verdict="unknown"))
    report, _ = run(FakeClient(config(), answers), clock=clock)
    assert report.ok, report.problems
    assert report.cases[1].eid == "999"


def test_a_registry_that_misses_once_is_retried_and_passes():
    answers = _healthy()
    answers[("100", True)] = [
        answer(outcome="technical", verdict="unknown", reason="TECHNICAL_TIMEOUT"),
        answer(verdict="dead", active=False),
    ]
    report, clock = run(FakeClient(config(), answers))
    assert report.ok, report.problems
    assert report.cases[1].attempts == 2
    assert RETRY_PAUSE_SECONDS in clock.slept


def test_a_registry_that_misses_twice_fails():
    answers = _healthy()
    answers[("100", True)] = [answer(outcome="technical", verdict="unknown", reason="TECHNICAL_TIMEOUT")]
    report, _ = run(FakeClient(config(), answers))
    assert any("did not answer (TECHNICAL_TIMEOUT), twice" in p for p in report.problems)


def test_no_http_answer_fails_after_the_retry():
    answers = _healthy()
    answers[("100", True)] = [Response(None, None, "no answer: timeout", 45.0)]
    report, _ = run(FakeClient(config(), answers))
    assert any("no answer from the checker" in p for p in report.problems)
    assert report.cases[1].outcome is None


# answers that must not be trusted


@pytest.mark.parametrize(
    "bad, why",
    [
        (answer(basis="store", age=5), "answered from 'store', expected live"),
        ({**answer(), "verdict": None}, "unknown verdict None"),
        ({**answer(), "outcome": "great"}, "unknown outcome 'great'"),
        ({**answer(), "is_active": None}, "must be true or false"),
        ({**answer(), "verified": False}, "outcome verified but verified is false"),
        ({**answer(), "observation_id": ""}, "without an observation id"),
        (answer(outcome="not_found", verdict="eligible"), "not found, yet the verdict is eligible"),
        ({**answer(outcome="not_found", verdict="unknown"), "verified": True}, "outcome not_found but verified"),
        ({**answer(), "entity_id": "other"}, "answered for AZ/other, not AZ/100"),
        ({**answer(), "state": "CA"}, "answered for CA/100"),
    ],
)
def test_malformed_answers_fail(bad, why):
    report, _ = run(FakeClient(config(), _healthy(rotating=bad)))
    assert not report.ok
    assert any(why in p for p in report.problems), report.problems


def test_malformed_accepts_a_well_formed_answer():
    body = {"state": "AZ", "entity_id": "1", **answer()}
    assert malformed(body, AZ, "1", basis="live") is None


# the storage round trip


def test_round_trip_fails_when_storage_never_serves_the_answer():
    answers = _healthy()
    answers[("200", False)] = [answer(verdict="eligible", basis="live", obs="live1")]
    report, _ = run(FakeClient(config(), answers))
    assert report.round_trip["passed"] is False
    assert len(report.round_trip["tries"]) == 3
    assert any("did not serve this run's answer back" in p for p in report.problems)


def test_round_trip_ignores_a_stored_answer_from_an_earlier_run():
    answers = _healthy()
    answers[("200", False)] = [answer(verdict="eligible", basis="store", age=12, obs="from-3-hours-ago")]
    report, _ = run(FakeClient(config(), answers))
    assert report.round_trip["passed"] is False


def test_round_trip_accepts_the_replacement_a_missed_probe_saved():
    answers = _healthy()
    answers[("200", False)] = [answer(basis="live", obs="live2"), answer(basis="store", age=20, obs="live2")]
    report, _ = run(FakeClient(config(), answers))
    assert report.ok, report.problems
    assert [t["basis"] for t in report.round_trip["tries"]] == ["live", "store"]


def test_round_trip_fails_when_storage_serves_a_different_verdict():
    answers = _healthy()
    answers[("200", False)] = [answer(verdict="already_filed", basis="store", age=16, obs="live1")]
    report, _ = run(FakeClient(config(), answers))
    assert report.round_trip["passed"] is False
    assert any(
        "observation live1 changed: verdict eligible -> already_filed, can_file True -> False, action proceed -> block"
        in p
        for p in report.problems
    )


def test_round_trip_fails_on_a_malformed_stored_answer():
    answers = _healthy()
    answers[("200", False)] = [{**answer(basis="store", age=16, obs="live1"), "verified": False}]
    report, _ = run(FakeClient(config(), answers))
    assert any("storage gave a malformed answer" in p for p in report.problems)


@pytest.mark.parametrize("age", [True, -1, 1.5, None])
def test_round_trip_does_not_accept_a_bad_age(age):
    answers = _healthy()
    answers[("200", False)] = [answer(basis="store", age=age, obs="live1")]
    report, _ = run(FakeClient(config(), answers))
    assert report.round_trip["passed"] is False


def test_no_storable_answer_skips_the_round_trip_with_a_warning():
    answers = _healthy()
    answers[("200", True)] = [answer(verdict="eligible", active=False)]
    client = FakeClient(config(), answers)
    report, _ = run(client)
    assert report.round_trip == {"checked": False}
    assert any("round trip not checked" in w for w in report.warnings)
    assert all(live for _, _, live in client.calls)  # no store-first lookup


# configuration and served contract


def test_an_unreachable_checker_fails():
    report, _ = run(FakeClient(Response(None, None, "no answer: refused", 1.0), {}))
    assert "configuration could not be read" in report.problems[0]


@pytest.mark.parametrize("states_value", [None, "x", [None, 3]])
def test_a_malformed_state_list_fails_without_crashing(states_value):
    report, _ = run(FakeClient({"states": states_value}, {}))
    assert report.problems == ["the checker's configuration does not list AZ"]


def test_a_served_state_reported_not_served_fails():
    client = FakeClient(config(served=False), {("200", False): [answer(outcome="unsupported", verdict="unknown")]})
    report, _ = run(client)
    assert any("says AZ is not served, but it should be served" in p for p in report.problems)
    assert client.calls == []


def test_a_served_flag_that_is_not_boolean_fails():
    report, _ = run(FakeClient({"states": [{**config()["states"][0], "served": "yes"}]}, {}))
    assert "served flag is 'yes'" in report.problems[0]


@pytest.mark.parametrize(
    "samples, why",
    [
        ([], "needs a made-up sample id and one expected active"),
        ([{"eid": "1", "expect": "eligible"}], "needs a made-up sample id and one expected active"),
        ([{"eid": "1", "expect": "dead"}, {"eid": "9", "expect": "not_found"}], "one expected active (or unknown)"),
        ([{"eid": "1", "expect": "dead"}, {"eid": "2", "expect": "maybe"}], "expects 'maybe'"),
        ([{"eid": "", "expect": "dead"}, {"eid": "9", "expect": "not_found"}], "no usable id"),
        ("nope", "missing or not a list"),
    ],
)
def test_unusable_samples_fail(samples, why):
    report, _ = run(FakeClient(config(samples=samples), {}))
    assert any(why in p for p in report.problems), report.problems


def test_a_state_meant_not_to_be_served_passes_with_a_warning():
    client = FakeClient(
        config(state="in", served=False, status="not_served", note="Blocked."),
        {("200", False): [answer(outcome="unsupported", verdict="unknown")]},
    )
    report, _ = run(client, state=IN)
    assert report.ok, report.problems
    assert report.warnings == ["IN is not served by the checker: Blocked."]
    assert client.calls == [("in", "200", False)]  # never live


def test_a_state_meant_not_to_be_served_that_is_served_fails():
    report, _ = run(FakeClient(config(state="in", served=True), {}), state=IN)
    assert any("says IN is served, but it should be not served" in p for p in report.problems)


def test_a_state_meant_not_to_be_served_that_answers_fails():
    client = FakeClient(config(state="in", served=False), {("200", False): [answer()]})
    report, _ = run(client, state=IN)
    assert any("should not be served, but it answered as verified" in p for p in report.problems)


# budget and crashes


def test_the_budget_stops_new_lookups_and_fails():
    report, _ = run(FakeClient(config(), _healthy()), budget=0)
    assert report.cases == []
    assert any("budget ran out before checking 200" in p for p in report.problems)


def test_a_crash_inside_the_check_becomes_a_problem():
    class Exploding(FakeClient):
        def lookup(self, *a, **k):
            raise RuntimeError("boom")

    report, _ = run(Exploding(config(), {}))
    assert report.problems == ["the check itself failed: RuntimeError: boom"]
    assert report.finished_at


# output


def test_markdown_escapes_table_cells_and_lists_problems():
    report, _ = run(FakeClient(config(), _healthy(rotating=answer(outcome="technical", reason="a|b\nc"))))
    text = markdown(report, "https://example.invalid/run/1")
    assert "a\\|b c" in text
    assert "**Problems**" in text and "[Run log and report](https://example.invalid/run/1)" in text
    assert text.startswith("## ❌ AZ (Arizona)")


def test_report_dict_is_json_ready():
    report, _ = run(FakeClient(config(), _healthy()))
    data = report.to_dict()
    assert data["ok"] is True
    json.dumps(data)


@pytest.mark.parametrize(
    "bad, why",
    [
        ({**answer(), "action": "block"}, "verdict eligible with action 'block', expected proceed"),
        ({**answer(verdict="dead"), "can_file": True}, "action block with can_file True"),
        ({**answer(), "can_file": None}, "action proceed with can_file None"),
    ],
)
def test_answers_against_the_decision_table_fail(bad, why):
    report, _ = run(FakeClient(config(), _healthy(rotating=bad)))
    assert any(why in p for p in report.problems), report.problems


def test_an_eligible_but_inactive_answer_is_accepted():
    # is_active comes from the state's status field, apart from the decision
    report, _ = run(FakeClient(config(), _healthy(rotating=answer(verdict="eligible", active=False, obs="x"))))
    assert report.ok, report.problems
    assert not any("malformed" in w for w in report.warnings)


def test_round_trip_recovers_when_another_run_saved_a_newer_observation():
    answers = _healthy()
    answers[("200", True)] = [answer(obs="live1"), answer(obs="live3")]
    # 15 s after our answer, storage holds one fetched 2 s ago: someone else's, newer
    answers[("200", False)] = [answer(basis="store", age=2, obs="theirs"), answer(basis="store", age=5, obs="live3")]
    client = FakeClient(config(), answers)
    report, _ = run(client)
    assert report.ok, report.problems
    assert client.calls.count(("az", "200", True)) == 2  # the one extra live lookup


def test_round_trip_waits_out_an_older_foreign_observation_without_asking_live():
    answers = _healthy()
    answers[("200", False)] = [answer(basis="store", age=900, obs="older"), answer(basis="store", age=40, obs="live1")]
    client = FakeClient(config(), answers)
    report, _ = run(client)
    assert report.ok, report.problems
    assert client.calls.count(("az", "200", True)) == 1


def test_round_trip_refreshes_for_a_foreign_save_made_while_our_request_was_in_flight():
    # Our live call took 10 s; a foreign observation fetched 5 s into it overwrote ours.
    clock = Clock(wall=0)
    answers = _healthy()
    answers[("200", True)] = [answer(obs="live1"), answer(obs="live3")]
    answers[("200", False)] = [answer(basis="store", age=20, obs="theirs"), answer(basis="store", age=5, obs="live3")]

    class SlowLive(FakeClient):
        def lookup(self, state, eid, *, live):
            r = super().lookup(state, eid, live=live)
            if live and eid == "200":
                clock.t += 10
                r.seconds = 10.0
            return r

    client = SlowLive(config(), answers)
    report, _ = run(client, clock=clock)
    assert report.ok, report.problems
    assert client.calls.count(("az", "200", True)) == 2


def test_round_trip_reads_back_a_refresh_made_on_the_last_probe():
    answers = _healthy()
    answers[("200", True)] = [answer(obs="live1"), answer(obs="live3")]
    answers[("200", False)] = [
        answer(basis="store", age=900, obs="older"),
        answer(basis="store", age=900, obs="older"),
        answer(basis="store", age=1, obs="theirs"),
        answer(basis="store", age=4, obs="live3"),
    ]
    report, _ = run(FakeClient(config(), answers))
    assert report.ok, report.problems
    assert len(report.round_trip["tries"]) == 4


def test_round_trip_is_inconclusive_when_the_entity_turns_inactive():
    answers = _healthy()
    answers[("200", False)] = [answer(verdict="needs_reinstatement", active=False, obs="live7")]
    report, _ = run(FakeClient(config(), answers))
    assert report.ok, report.problems
    assert report.round_trip["passed"] is None
    assert any("inconclusive: 200 now answers needs_reinstatement" in w for w in report.warnings)


def test_a_probe_that_gets_no_registry_answer_is_not_inconclusive():
    # found on GitHub 2026-11-03: a technical probe was taken for "the entity changed"
    answers = _healthy()
    answers[("200", False)] = [answer(outcome="technical", verdict="unknown", reason="TECHNICAL_TIMEOUT")]
    report, _ = run(FakeClient(config(), answers))
    assert not report.ok
    assert report.round_trip["passed"] is False
    assert not any("inconclusive" in w for w in report.warnings)
