"""Layered verbosity — the first-class control surface (F147 spec §6).

Two independent dials that compose:

1. A **global level** ``0..5`` (``quiet`` → ``firehose``). Each level unlocks a
   fixed set of channels (the §6.1 table). ``should_emit(channel, level)`` is the
   pure level-gate.
2. **Per-channel overrides** (``/watch``, ``/mute``, ``/focus``) held on a
   mutable :class:`Verbosity` state object, so a user can drill into one channel
   without cranking the whole level, or solo a single channel.

The two compose deterministically (plan §4 invariant 7): ``focus`` wins over
everything, then ``mute`` force-off, then ``watch`` force-on, else the level
gate.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from enum import IntEnum


class Level(IntEnum):
    """Global verbosity level. Higher = more streams in the live view."""

    QUIET = 0
    DEFAULT = 1
    VERBOSE = 2
    DEBUG = 3
    TRACE = 4
    FIREHOSE = 5


# The known live-view channels and the minimum global level at which each one
# appears by default. Mirrors the §6.1 table:
#   L1 default : team-log, attention, prs, pm (PM chat messages mid-run)
#   L2 verbose : + decisions, runtime (task transitions/test runs/launch)
#   L3 debug   : + turns (per-turn headers), tokens
#   L4 trace   : + tools (tool-events, prompt/response)
#   L5 firehose: + poll/http (raw poll diffs / HTTP call trace)
CHANNEL_MIN_LEVEL: dict[str, int] = {
    "team-log": Level.DEFAULT,
    "attention": Level.DEFAULT,
    "prs": Level.DEFAULT,
    "pm": Level.DEFAULT,  # F158: PM messages posted mid-run (a message to you)
    "decisions": Level.VERBOSE,
    "runtime": Level.VERBOSE,
    "turns": Level.DEBUG,
    "tokens": Level.DEBUG,
    "tools": Level.TRACE,
    "poll": Level.FIREHOSE,
    "http": Level.FIREHOSE,
}

CHANNELS: frozenset[str] = frozenset(CHANNEL_MIN_LEVEL)

# Accepted textual level names (in addition to the bare integers 0..5).
_LEVEL_NAMES = {
    "quiet": Level.QUIET,
    "default": Level.DEFAULT,
    "verbose": Level.VERBOSE,
    "debug": Level.DEBUG,
    "trace": Level.TRACE,
    "firehose": Level.FIREHOSE,
}


def channel_min_level(channel: str) -> int:
    """Minimum global level at which ``channel`` streams by default.

    An unknown channel is treated as ``FIREHOSE``-only (never shown until the
    user explicitly asks) so a typo never silently spams the default view.
    """
    return CHANNEL_MIN_LEVEL.get(channel, int(Level.FIREHOSE))


def should_emit(channel: str, level: int) -> bool:
    """Pure level-gate: does ``channel`` stream at global ``level``?

    This is the level dial only — per-channel ``watch``/``mute``/``focus``
    overrides live on :class:`Verbosity`.
    """
    return channel_min_level(channel) <= int(level)


def parse_level(raw: str | int | None) -> Level:
    """Coerce a CLI/env verbosity value into a :class:`Level`.

    Accepts an int (or int-like string) ``0..5`` or a name (``quiet``,
    ``firehose``, …). Out-of-range ints clamp to the valid band. ``None``/blank
    → :data:`Level.DEFAULT`.
    """
    if raw is None:
        return Level.DEFAULT
    if isinstance(raw, int):
        value = raw
    else:
        text = str(raw).strip().lower()
        if not text:
            return Level.DEFAULT
        if text in _LEVEL_NAMES:
            return _LEVEL_NAMES[text]
        try:
            value = int(text)
        except ValueError:
            return Level.DEFAULT
    value = max(int(Level.QUIET), min(int(Level.FIREHOSE), value))
    return Level(value)


def resolve_level(override: str | int | None = None) -> Level:
    """Resolve the effective global level.

    ``override`` (from ``--verbosity``) wins; else ``ERRORTA_CLI_VERBOSITY``;
    else :data:`Level.DEFAULT`.
    """
    if override is not None and str(override).strip() != "":
        return parse_level(override)
    return parse_level(os.environ.get("ERRORTA_CLI_VERBOSITY"))


@dataclass
class Verbosity:
    """Mutable per-session verbosity state: a level + channel overrides."""

    level: Level = Level.DEFAULT
    watched: set[str] = field(default_factory=set)
    muted: set[str] = field(default_factory=set)
    focus: str | None = None

    def should_emit(self, channel: str) -> bool:
        """Compose the level gate with the per-channel overrides.

        Precedence: ``focus`` (solo) > ``mute`` (force-off) > ``watch``
        (force-on) > the level gate.
        """
        if self.focus is not None:
            return channel == self.focus
        if channel in self.muted:
            return False
        if channel in self.watched:
            return True
        return should_emit(channel, self.level)

    def watch(self, channel: str) -> None:
        self.watched.add(channel)
        self.muted.discard(channel)

    def mute(self, channel: str) -> None:
        self.muted.add(channel)
        self.watched.discard(channel)

    def set_focus(self, channel: str | None) -> None:
        self.focus = channel

    def set_level(self, level: int) -> None:
        self.level = parse_level(level)
