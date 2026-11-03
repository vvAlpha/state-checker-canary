"""The states the checker serves, in the console's order, and when each one runs.

Every state has its own workflow (.github/workflows/state-<code>.yml), generated
from this table by `python -m canary workflows --write`. CI fails when the files
and the table disagree, so adding a state is one line here plus a regeneration.
"""

from __future__ import annotations

from dataclasses import dataclass

# Every 3 hours. The states start STAGGER_MINUTES apart, so 25 checks spread over
# the 3-hour window instead of landing on the checker, its proxies and GitHub's
# scheduler at once (GitHub delays runs queued at the start of an hour).
INTERVAL_HOURS = 3
FIRST_MINUTE = 3
STAGGER_MINUTES = 7


@dataclass(frozen=True)
class State:
    code: str  # lower case, as in the checker's URLs
    name: str
    group: str
    # Whether the checker is meant to serve the state. This is kept here, not
    # read from the checker, so a deployment that stops serving a state by
    # mistake fails its check instead of passing as "not served".
    served: bool = True


STATES: tuple[State, ...] = (
    # First 10
    State("ca", "California", "First 10"),
    State("ct", "Connecticut", "First 10"),
    State("fl", "Florida", "First 10"),
    State("nc", "North Carolina", "First 10"),
    State("ny", "New York", "First 10"),
    State("pa", "Pennsylvania", "First 10"),
    State("va", "Virginia", "First 10"),
    State("wa", "Washington", "First 10"),
    State("wi", "Wisconsin", "First 10"),
    State("wy", "Wyoming", "First 10"),
    # Second 8
    State("ga", "Georgia", "Second 8"),
    # Not served since 2026-10-23: the state's firewall blocks the checker.
    State("in", "Indiana", "Second 8", served=False),
    State("mo", "Missouri", "Second 8"),
    State("ms", "Mississippi", "Second 8"),
    State("mt", "Montana", "Second 8"),
    State("nh", "New Hampshire", "Second 8"),
    State("nv", "Nevada", "Second 8"),
    State("vt", "Vermont", "Second 8"),
    # Third 7
    State("az", "Arizona", "Third 7"),
    State("ma", "Massachusetts", "Third 7"),
    State("mi", "Michigan", "Third 7"),
    State("oh", "Ohio", "Third 7"),
    State("or", "Oregon", "Third 7"),
    State("sd", "South Dakota", "Third 7"),
    State("tx", "Texas", "Third 7"),
)

BY_CODE = {s.code: s for s in STATES}


def get(code: str) -> State:
    try:
        return BY_CODE[code.strip().lower()]
    except KeyError:
        raise SystemExit(f"unknown state {code!r}; known: {', '.join(sorted(BY_CODE))}") from None


def cron(state: State) -> str:
    """The state's schedule in UTC: every INTERVAL_HOURS hours, at its own minute."""
    start = FIRST_MINUTE + STAGGER_MINUTES * STATES.index(state)
    hour, minute = divmod(start, 60)
    return f"{minute} {hour}-23/{INTERVAL_HOURS} * * *"
