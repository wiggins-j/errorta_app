"""F151 — CLI run ergonomics: `stop` alias, `--autonomous` policy confirm, and the
`log --watch` tail (append-only, content-LCP diff)."""
from __future__ import annotations

from io import StringIO
from typing import Any

import pytest

from errorta_cli import registry, watch
from errorta_cli.commands import runctl

# --- Item 1: stop alias ------------------------------------------------------

def test_stop_resolves_to_cancel() -> None:
    assert registry.get("stop") is registry.get("cancel")
    assert registry.get("cancel").name == "cancel"


def test_alias_does_not_duplicate_canonical() -> None:
    names = [c.name for c in registry.all_commands()]
    assert names.count("cancel") == 1
    assert "stop" not in registry.names()
    assert registry.aliases().get("stop") == "cancel"


def test_unknown_alias_is_none() -> None:
    assert registry.get("definitely-not-a-command") is None


# --- Item 2: --autonomous ----------------------------------------------------

class _RecClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, Any]] = []

    def post_json(self, path: str, *, json: Any = None, **_: Any) -> Any:
        self.calls.append((path, json))
        return {}

    def get_json(self, *_a: Any, **_k: Any) -> Any:
        return {}


class _Ctx:
    project_id = "p"


def test_autonomous_posts_policy_only_confirm() -> None:
    c = _RecClient()
    runctl._apply_autonomy(c, _Ctx(), {"autonomous": True})
    assert c.calls == [("/coding/projects/p/run-setup/confirm", {"checkpoint_cadence": "off"})]


def test_checkpoint_cadence_passthrough() -> None:
    c = _RecClient()
    runctl._apply_autonomy(c, _Ctx(), {"checkpoint-cadence": "per_milestone"})
    assert c.calls[0][1] == {"checkpoint_cadence": "per_milestone"}


def test_no_flag_no_confirm() -> None:
    c = _RecClient()
    runctl._apply_autonomy(c, _Ctx(), {})
    assert c.calls == []


def test_autonomy_never_sends_a_team() -> None:
    c = _RecClient()
    runctl._apply_autonomy(c, _Ctx(), {"autonomous": True})
    body = c.calls[0][1]
    assert "members" not in body and "room_id" not in body


def test_run_and_continue_carry_autonomy_params() -> None:
    for name in ("run", "resume", "continue"):
        flags = {p.name for p in registry.get(name).params}
        assert "autonomous" in flags and "checkpoint-cadence" in flags


# --- Item 3: log --watch tail ------------------------------------------------

def _entry(at: str, msg: str, role: str = "pm", member: str = "", kind: str = "k") -> dict:
    return {"at": at, "role": role, "member": member, "kind": kind, "message": msg}


def _run_stream_over(ticks: list[dict], monkeypatch: pytest.MonkeyPatch,
                     raw_args: list[str] | None = None) -> list[str]:
    seq = iter(ticks)
    monkeypatch.setattr(registry, "dispatch",
                        lambda *a, **k: (next(seq), ""))
    out = StringIO()

    class C:
        poll_interval = None

    watch._run_stream("log", None, C(), raw_args or [], out, 0.0,
                      iterations=len(ticks), sleep=lambda _s: None)
    return [ln for ln in out.getvalue().splitlines() if ln.strip()]


def test_tail_appends_each_event_once(monkeypatch: pytest.MonkeyPatch) -> None:
    ticks = [
        {"entries": [_entry("1", "a")]},
        {"entries": [_entry("1", "a"), _entry("2", "b")]},
        {"entries": [_entry("1", "a"), _entry("2", "b"), _entry("3", "c")]},
    ]
    lines = _run_stream_over(ticks, monkeypatch)
    assert [ln.strip()[-1] for ln in lines] == ["a", "b", "c"]  # once each, in order


def test_tail_quiet_tick_prints_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    same = {"entries": [_entry("1", "a")]}
    lines = _run_stream_over([same, same, same], monkeypatch)
    assert len(lines) == 1  # printed once, then nothing new


def test_tail_reprints_on_divergence(monkeypatch: pytest.MonkeyPatch) -> None:
    # A mid-list insertion (earlier-timestamped event lands before "b").
    ticks = [
        {"entries": [_entry("2", "b"), _entry("3", "c")]},
        {"entries": [_entry("1", "a-inserted"), _entry("2", "b"), _entry("3", "c")]},
    ]
    lines = _run_stream_over(ticks, monkeypatch)
    # tick1: b, c. tick2: prefix diverges at index 0 -> reprint from there: a, b, c.
    assert any("a-inserted" in ln for ln in lines)
    assert sum("b" in ln for ln in lines) >= 2  # b reprinted, not dropped


def test_tail_filter_applies(monkeypatch: pytest.MonkeyPatch) -> None:
    ticks = [{"entries": [_entry("1", "keep", role="dev"),
                          _entry("2", "drop", role="reviewer")]}]
    # log passes _filters via payload; simulate by injecting into the payload.
    ticks[0]["_filters"] = {"role": "dev", "member": None, "grep": None}
    lines = _run_stream_over(ticks, monkeypatch)
    assert len(lines) == 1 and "keep" in lines[0]


def test_log_is_stream_mode() -> None:
    assert registry.get("log").watch_mode == "stream"
