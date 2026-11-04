"""Each state's pool of entity ids (pools/<code>.txt): one id per line, optionally
followed by "active" when the entity was active at collection, so storage would
serve it; `#` starts a comment. The ids come from the checker's own benchmark
runs, where each was verified."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from .states import State

POOLS = Path(__file__).resolve().parent.parent / "pools"
LINE = re.compile(r"([A-Za-z0-9][A-Za-z0-9-]{0,39})(?:\s+(active))?")


@dataclass(frozen=True)
class Pool:
    ids: tuple[str, ...]
    active: tuple[str, ...]  # a subset of ids


def load(state: State, folder: Path = POOLS) -> Pool:
    """The state's pool; SystemExit when the file is missing or malformed, an id
    appears twice, or no id is marked active."""
    path = folder / f"{state.code}.txt"
    try:
        lines = path.read_text().splitlines()
    except OSError as e:
        raise SystemExit(f"no pool for {state.code.upper()}: {e}") from None
    ids, active = [], []
    for line in (raw.split("#", 1)[0].strip() for raw in lines):  # ids never contain "#"
        if not line:
            continue
        m = LINE.fullmatch(line)
        if not m:
            raise SystemExit(f"{path.name}: not an entity id line: {line!r}")
        ids.append(m.group(1))
        if m.group(2):
            active.append(m.group(1))
    if len(set(ids)) != len(ids):
        raise SystemExit(f"{path.name}: an id appears twice")
    if not active:
        raise SystemExit(f"{path.name}: no id is marked active, so no batch could test storage")
    return Pool(tuple(ids), tuple(active))
