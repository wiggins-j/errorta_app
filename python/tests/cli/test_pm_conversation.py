"""F158 — CLI PM conversation: extended pm-ask timeout (P0), the interactive
chat loop (Item 1), the `pm` live channel (Item 2a), and sub-aware `pm chat
--watch` tailing (Item 3). Pure/injected IO — no sidecar, no terminal."""
from __future__ import annotations

import pytest

from errorta_cli import registry, watch
from errorta_cli.commands import pm
from errorta_cli.errors import CliError
from errorta_cli.poller import DEFAULT_SOURCES
from errorta_cli.render.runctl import render_stream_event

from .conftest import RouteClient

# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #

def _scripted(lines):
    """A read_line that yields each line, then raises EOFError (Ctrl-D)."""
    it = iter(lines)

    def read_line(_prompt: str) -> str:
        try:
            return next(it)
        except StopIteration:
            raise EOFError
    return read_line


class _Writer:
    def __init__(self):
        self.blocks: list[str] = []

    def __call__(self, text: str) -> None:
        self.blocks.append(text)

    @property
    def text(self) -> str:
        return "\n".join(self.blocks)


class _Ev:
    """Minimal stand-in for poller.Event (render_stream_event uses getattr)."""

    def __init__(self, channel, item):
        self.channel = channel
        self.item = item


# --------------------------------------------------------------------------- #
# P0 — pm-ask gets a timeout longer than the 30s client default
# --------------------------------------------------------------------------- #

def test_pm_ask_uses_extended_timeout():
    seen = {}

    class _Spy:
        def post_json(self, path, *, json=None, params=None, timeout=None):
            seen["path"], seen["timeout"] = path, timeout
            return {"reply": {"message": "ok"}, "answered": True}

    pm._ask_turn(_Spy(), "p", "why?")
    assert seen["path"].endswith("/pm-ask")
    assert seen["timeout"] == pm._PM_ASK_TIMEOUT
    assert seen["timeout"] >= 120  # covers the server's 120s cap


# --------------------------------------------------------------------------- #
# Item 1 — line classification + the interactive loop
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("line,kind,payload", [
    ("why did you drop caching?", "question", "why did you drop caching?"),
    ("!focus on the parser", "directive", "focus on the parser"),
    ("\\!important, why?", "question", "!important, why?"),   # escaped bang
    ("/exit", "meta", "exit"),
    ("/quit", "meta", "quit"),
    ("/changes", "meta", "changes"),
    ("/etc/hosts — what's in it?", "question", "/etc/hosts — what's in it?"),  # not meta
])
def test_classify_line(line, kind, payload):
    assert pm._classify_line(line) == (kind, payload)


def test_interactive_question_then_exit(make_ctx):
    client = RouteClient(responses={
        "/pm-chat": {"thread": [{"role": "user", "message": "hi"}]},
        "/pm-ask": {"reply": {"role": "pm", "kind": "chat",
                              "message": "because it wasn't on the critical path"},
                    "answered": True},
    })
    w = _Writer()
    pm.run_pm_chat(client, make_ctx(project_id="p"),
                   read_line=_scripted(["why drop caching?", "/exit"]), write=w)
    assert ("POST", "/coding/projects/p/pm-ask") in client.calls
    assert "PM: because it wasn't on the critical path" in w.text
    assert "leaving PM chat" in w.text


def test_interactive_directive_uses_interject(make_ctx):
    client = RouteClient(responses={
        "/pm-chat": {"thread": []},
        "/interject": {"ok": True, "applied": [], "refusals": []},
    })
    w = _Writer()
    pm.run_pm_chat(client, make_ctx(project_id="p"),
                   read_line=_scripted(["!prioritize the parser", "/exit"]), write=w)
    assert ("POST", "/coding/projects/p/interject") in client.calls
    assert ("POST", "/coding/projects/p/pm-ask") not in client.calls
    assert "directive delivered" in w.text


def test_render_ask_reply_handles_both_non_answer_branches():
    # pm_unreachable: has an `error` key.
    unreachable = pm._render_ask_reply({
        "reply": {"kind": "error", "message": "busy — try again"},
        "answered": False, "error": "pm_unreachable"})
    assert "busy — try again" in unreachable
    # unconfigured: NO `error` key — must not KeyError, and points at team apply.
    unconfigured = pm._render_ask_reply({
        "reply": {"kind": "unconfigured", "message": "team isn't set up"},
        "answered": False})
    assert "team isn't set up" in unconfigured
    assert "team apply" in unconfigured


def test_render_ask_reply_surfaces_applied_change_hint():
    out = pm._render_ask_reply({
        "reply": {"kind": "chat", "message": "done"}, "answered": True,
        "applied": [{"change_id": "c1", "summary": "assigned dev"}]})
    assert "review with `pm changes`" in out


def test_interactive_refuses_under_json(make_ctx):
    ctx = make_ctx(project_id="p")
    ctx.json_mode = True
    with pytest.raises(CliError) as ei:
        pm._call(RouteClient(), ctx, {"sub": "chat", "interactive": True})
    assert ei.value.code == "interactive_requires_tty"


# --------------------------------------------------------------------------- #
# Item 2a — the `pm` live channel
# --------------------------------------------------------------------------- #

def test_pm_chat_source_registered():
    src = [s for s in DEFAULT_SOURCES if s.name == "pm-chat"]
    assert src and src[0].channel == "pm" and src[0].mode == "append"
    assert src[0].key == "thread"


def test_stream_event_renders_pm_turn_but_not_user():
    pm_line = render_stream_event(_Ev("pm", {
        "role": "pm", "message": "I re-planned the backlog", "at": "2026-07-13T10:00:00"}))
    assert pm_line is not None and "PM" in pm_line and "re-planned" in pm_line
    # the user's own turn is not echoed back into the live view
    assert render_stream_event(_Ev("pm", {"role": "user", "message": "hi"})) is None


# --------------------------------------------------------------------------- #
# Item 3 — sub-aware watch_mode + tailing
# --------------------------------------------------------------------------- #

def test_pm_watch_mode_is_sub_aware():
    cmd = registry.get("pm")
    assert cmd.watch_mode_for({"sub": "chat"}) == "stream"
    assert cmd.watch_mode_for({"sub": "changes"}) == "snapshot"
    assert cmd.watch_mode_for({}) == "stream"  # sub defaults to chat


def test_pm_chat_watch_tails_transcript(make_ctx):
    client = RouteClient(responses={"/pm-chat": {"thread": [
        {"role": "user", "message": "why?"},
        {"role": "pm", "message": "here's why"}]}})
    import io
    out = io.StringIO()
    watch.run_watch("pm", client, make_ctx(project_id="p"), ["chat", "--watch"],
                    iterations=1, sleep=lambda _s: None, out=out, clear=False)
    text = out.getvalue()
    assert "here's why" in text
    assert "\x1b[2J" not in text  # tail path never clears the screen


def test_pm_ask_watch_is_rejected(make_ctx):
    client = RouteClient()
    with pytest.raises(CliError) as ei:
        watch.run_watch("pm", client, make_ctx(project_id="p"),
                        ["ask", "q", "--watch"], iterations=1, sleep=lambda _s: None)
    assert ei.value.code == "watch_on_mutation"
