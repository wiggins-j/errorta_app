"""Layered verbosity: level gate + per-channel overrides compose (invariant 7)."""
from __future__ import annotations

from errorta_cli.verbosity import (
    CHANNELS,
    Level,
    Verbosity,
    channel_min_level,
    parse_level,
    resolve_level,
    should_emit,
)


def test_level_gate_is_monotonic_per_channel() -> None:
    for channel in CHANNELS:
        minimum = channel_min_level(channel)
        for level in range(0, 6):
            assert should_emit(channel, level) is (minimum <= level)


def test_level_N_shows_exactly_channels_leq_N() -> None:
    # The §6.1 table, asserted at each level boundary.
    assert {c for c in CHANNELS if should_emit(c, 0)} == set()
    assert {c for c in CHANNELS if should_emit(c, 1)} == {"team-log", "attention", "prs"}
    assert should_emit("decisions", 2) and should_emit("runtime", 2)
    assert not should_emit("turns", 2)
    assert should_emit("turns", 3) and should_emit("tokens", 3)
    assert not should_emit("tools", 3)
    assert should_emit("tools", 4)
    assert should_emit("poll", 5) and should_emit("http", 5)
    assert not should_emit("poll", 4)


def test_watch_forces_channel_on_below_its_level() -> None:
    v = Verbosity(level=Level.QUIET)
    assert not v.should_emit("turns")
    v.watch("turns")
    assert v.should_emit("turns")


def test_mute_forces_channel_off_above_its_level() -> None:
    v = Verbosity(level=Level.FIREHOSE)
    assert v.should_emit("team-log")
    v.mute("team-log")
    assert not v.should_emit("team-log")


def test_focus_solos_a_single_channel() -> None:
    v = Verbosity(level=Level.FIREHOSE)
    v.set_focus("prs")
    assert v.should_emit("prs")
    assert not v.should_emit("team-log")
    v.set_focus(None)
    assert v.should_emit("team-log")


def test_watch_and_mute_compose() -> None:
    v = Verbosity(level=Level.DEFAULT)
    v.watch("turns")  # normally L3
    v.mute("prs")  # normally L1
    assert v.should_emit("turns")
    assert not v.should_emit("prs")
    assert v.should_emit("team-log")  # still on by the level gate


def test_watch_then_mute_same_channel_last_wins() -> None:
    v = Verbosity(level=Level.QUIET)
    v.watch("turns")
    v.mute("turns")
    assert not v.should_emit("turns")
    v.watch("turns")
    assert v.should_emit("turns")


def test_parse_level_accepts_ints_names_and_clamps() -> None:
    assert parse_level(3) is Level.DEBUG
    assert parse_level("firehose") is Level.FIREHOSE
    assert parse_level("2") is Level.VERBOSE
    assert parse_level(99) is Level.FIREHOSE
    assert parse_level(-4) is Level.QUIET
    assert parse_level(None) is Level.DEFAULT
    assert parse_level("garbage") is Level.DEFAULT


def test_resolve_level_prefers_override_then_env(monkeypatch) -> None:
    monkeypatch.setenv("ERRORTA_CLI_VERBOSITY", "4")
    assert resolve_level(None) is Level.TRACE
    assert resolve_level("1") is Level.DEFAULT  # override wins
    monkeypatch.delenv("ERRORTA_CLI_VERBOSITY", raising=False)
    assert resolve_level(None) is Level.DEFAULT
