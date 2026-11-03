"""GitHub Actions workflow commands, escaped so text from the checker cannot
end a command early, start a new one, or switch command processing off."""

from __future__ import annotations

import secrets
import sys
from contextlib import contextmanager


def _data(text: str) -> str:
    # `##[` starts a legacy command anywhere in a line; a zero-width joiner breaks it.
    return text.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A").replace("##[", "#\u2060#[")


def _prop(text: str) -> str:
    return _data(text).replace(":", "%3A").replace(",", "%2C")


def annotate(level: str, title: str, message: str, out=None) -> None:
    """level: error, warning or notice."""
    if level not in ("error", "warning", "notice"):
        raise ValueError(level)
    print(f"::{level} title={_prop(title)}::{_data(message)}", file=out or sys.stdout, flush=True)


def mask(value: str, out=None) -> None:
    if value:
        print(f"::add-mask::{_data(value)}", file=out or sys.stdout, flush=True)


@contextmanager
def commands_off(out=None):
    """Print untrusted text inside: the runner ignores workflow commands until the
    block ends, under a token the text cannot know."""
    out = out or sys.stdout
    token = secrets.token_hex(16)
    print(f"::stop-commands::{token}", file=out, flush=True)
    try:
        yield
    finally:
        print(f"::{token}::", file=out, flush=True)
