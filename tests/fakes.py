"""A scripted checker: answers per (path) from queues, records every call."""

from __future__ import annotations

from canary.check import ACTION_FOR, CAN_FILE_ACTIONS
from canary.client import Response

SAMPLES = [
    {"eid": "100", "expect": "dead"},
    {"eid": "200", "expect": "eligible"},
    {"eid": "300", "expect": "already_filed"},
    {"eid": "999", "expect": "not_found"},
]


def config(state="az", served=True, status="working", note="", samples=None):
    return {
        "states": [
            {
                "state": state.upper(),
                "served": served,
                "status": status,
                "status_note": note,
                "samples": SAMPLES if samples is None else samples,
            }
        ]
    }


def answer(outcome="verified", verdict="eligible", *, active=True, basis="live", age=None, obs="obs-live", reason="OK"):
    return {
        "outcome": outcome,
        "verdict": verdict,
        "verified": outcome == "verified",
        "is_active": active,
        "basis": basis,
        "age_seconds": age,
        "observation_id": obs if outcome == "verified" else None,
        "action": ACTION_FOR.get(verdict),
        "can_file": ACTION_FOR.get(verdict) in CAN_FILE_ACTIONS,
        "reason": reason,
    }


class FakeClient:
    def __init__(self, cfg, answers):
        """answers: {(eid, live): [Response | dict, ...]}; the last one repeats."""
        self.cfg = cfg
        self.answers = answers
        self.calls: list[tuple[str, str, bool]] = []

    def config(self):
        if isinstance(self.cfg, Response):
            return self.cfg
        return Response(200, self.cfg, None, 0.1)

    def lookup(self, state, eid, *, live):
        self.calls.append((state, eid, live))
        queue = self.answers[(eid, live)]
        item = queue.pop(0) if len(queue) > 1 else queue[0]
        if isinstance(item, Response):
            return item
        # A real answer echoes what was asked; a test can override either.
        return Response(200, {"state": state.upper(), "entity_id": eid, **item}, None, 1.0)


class Clock:
    def __init__(self, wall=1_800_000_000.0):
        self.t = 0.0
        self.wall_t = wall
        self.slept: list[float] = []

    def sleep(self, s):
        self.slept.append(s)
        self.t += s

    def clock(self):
        return self.t

    def wall(self):
        return self.wall_t + self.t
