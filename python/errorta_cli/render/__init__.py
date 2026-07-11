"""Rich rendering for the read views (F147 spec §9).

One renderer module per view. Every renderer takes a *route payload* and returns
a **string** (the registry's ``render`` contract) so the argv and slash front-ends
print it identically. ``--json`` never reaches a renderer — the registry
short-circuits to :func:`~errorta_cli.registry.render_json`.

Golden invariant #5 (no secret/raw-payload leak): renderers **select** the fields
they surface — they never dump the whole payload. An unknown/extra field on an
item (a token, a raw blob) is simply not rendered. The only way to see the raw
bytes is the explicit ``--json`` bypass. ``test_render_no_raw_leak`` locks this.

Rendering is deterministic + pipe-friendly by default: color is emitted only when
stdout is a real TTY, so captured/piped output (and the test suite) is plain text.
"""
from __future__ import annotations

import sys
from typing import Any, Iterable

from rich.console import Console, Group, RenderableType
from rich.text import Text
from rich.theme import Theme

# Role → accent, mirroring the app's Team Log color language.
_ROLE_STYLE = {
    "pm": "magenta",
    "dev": "cyan",
    "reviewer": "yellow",
    "tester": "green",
    "system": "dim",
    "user": "bold blue",
}

THEME = Theme(
    {
        "cli.role.pm": "magenta",
        "cli.role.dev": "cyan",
        "cli.role.reviewer": "yellow",
        "cli.role.tester": "green",
        "cli.role.system": "dim",
        "cli.role.user": "bold blue",
        "cli.head": "bold",
        "cli.muted": "dim",
        "cli.ok": "green",
        "cli.warn": "yellow",
        "cli.bad": "red",
        "cli.key": "cyan",
    }
)

# The friendly line for a command that needs a bound project but has none.
NO_PROJECT_MSG = (
    "no project bound to this directory — cd into a project, pass --home, "
    "or select one with /open <id>"
)


def _color_enabled() -> bool:
    try:
        return bool(sys.stdout.isatty())
    except (ValueError, AttributeError):  # pragma: no cover — closed stdout
        return False


def render(*renderables: RenderableType, color: bool | None = None) -> str:
    """Capture ``renderables`` to a string via a Rich console.

    Color is auto (TTY-only) unless ``color`` is forced. Trailing blank lines are
    stripped so the front-end's ``echo`` doesn't add a second newline.
    """
    if color is None:
        color = _color_enabled()
    console = Console(
        theme=THEME,
        force_terminal=True if color else False,
        color_system="auto" if color else None,
        highlight=False,
        soft_wrap=False,
    )
    with console.capture() as cap:
        for item in renderables:
            console.print(item)
    return cap.get().rstrip("\n")


def group(renderables: Iterable[RenderableType]) -> Group:
    return Group(*list(renderables))


def role_style(role: str | None) -> str:
    return _ROLE_STYLE.get(str(role or "").lower(), "white")


def role_text(role: str | None) -> Text:
    label = str(role or "?")
    style = f"cli.role.{label.lower()}" if label.lower() in _ROLE_STYLE else "white"
    return Text(label, style=style)


def ts(value: Any) -> str:
    """Compact a stored ISO-ish timestamp to ``HH:MM:SS`` when possible."""
    text = str(value or "")
    if not text:
        return ""
    # Stored form is typically ``2026-07-10T13:45:12.xxxxx`` — keep the clock.
    if "T" in text:
        clock = text.split("T", 1)[1]
        return clock.split(".", 1)[0][:8] or text
    return text


def truncate(value: Any, limit: int = 100) -> str:
    text = " ".join(str(value or "").split())
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)] + "…"


def heading(text: str) -> Text:
    return Text(text, style="cli.head")


def muted(text: str) -> Text:
    return Text(text, style="cli.muted")


def no_project() -> str:
    return render(muted(NO_PROJECT_MSG))


def is_no_project(payload: Any) -> bool:
    return isinstance(payload, dict) and bool(payload.get("_no_project"))


def usage_text(payload: Any) -> str | None:
    if isinstance(payload, dict) and payload.get("_usage"):
        return str(payload["_usage"])
    return None
