"""Pure Slack Block Kit renderers — coding-team state to dict lists.

Every function here is a pure dict builder: no I/O, no engine calls, no
network, no time/randomness. This module MUST NOT import ``slack_sdk`` (or
anything else optional) at module load — Block Kit is just JSON-shaped
dicts, so no SDK is needed to construct it. The actual Slack API call (via
``slack_sdk``) belongs to whichever module posts these blocks (a later
task), not to this one.

``decision_message``'s two button ``action_id``s (``slack_approve`` /
``slack_decline``) are a contract with the Task 8 interaction callback,
which matches on these exact strings — do not rename without updating that
callback.
"""
from __future__ import annotations

from typing import Any


def _blocker_title(blocker: Any) -> str:
    """Best-effort human label for a blocker item.

    Accepts a plain dict (``{"title": ...}`` or ``{"summary": ...}``), an
    object with a ``.title``/``.summary`` attribute (e.g.
    ``AttentionSignal``), or falls back to ``str(blocker)``.
    """
    if isinstance(blocker, dict):
        return str(blocker.get("title") or blocker.get("summary") or blocker)
    title = getattr(blocker, "title", None) or getattr(blocker, "summary", None)
    return str(title) if title else str(blocker)


def status_card(team_log: list, blockers: list) -> list[dict]:
    """A section summarizing task/log counts and any open blockers."""
    log_count = len(team_log)
    blocker_count = len(blockers)
    log_noun = "entry" if log_count == 1 else "entries"
    blocker_noun = "blocker" if blocker_count == 1 else "blockers"

    blocks: list[dict] = [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    f"*Status:* {log_count} log {log_noun}, "
                    f"{blocker_count} open {blocker_noun}"
                ),
            },
        }
    ]

    if blockers:
        lines = "\n".join(f"• {_blocker_title(b)}" for b in blockers)
        blocks.append(
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": f"*Open blockers:*\n{lines}"},
            }
        )

    return blocks


def escape_mrkdwn(text: str) -> str:
    """Escape the three characters Slack treats as mrkdwn control characters.

    Used on any text that reaches a message body from outside the operator —
    a model-proposed goal body, for instance, which may itself be grounded in
    repo files nobody on this team wrote. Unescaped, ``<!channel>`` in such a
    string pings the whole workspace and ``<https://evil|Approve here>``
    renders as a plausible link. Slack's own guidance is to replace ``&``,
    ``<`` and ``>`` (``&`` first, or the escapes get double-escaped)."""
    return (str(text)
            .replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;"))


def decision_message(title: str, detail: str, confirmation_id: str) -> list[dict]:
    """A 🔴 DECISION NEEDED header + detail section + Approve/Decline actions.

    The button ``action_id``s (``slack_approve`` / ``slack_decline``) and
    each button's ``value`` (== ``confirmation_id``) are the contract the
    Task 8 interaction callback matches against.
    """
    return [
        {
            "type": "header",
            "text": {
                "type": "plain_text",
                "text": "🔴 DECISION NEEDED",
                "emoji": True,
            },
        },
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": f"*{title}*\n{detail}"},
        },
        {
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "Approve", "emoji": True},
                    "style": "primary",
                    "action_id": "slack_approve",
                    "value": confirmation_id,
                },
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "Decline", "emoji": True},
                    "style": "danger",
                    "action_id": "slack_decline",
                    "value": confirmation_id,
                },
            ],
        },
    ]


def fyi_message(text: str) -> list[dict]:
    """A single plain section — no buttons, no ``actions`` block."""
    return [
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": text},
        }
    ]


# concierge.py emits emoji GLYPHS (e.g. "🤔", "✅") in a turn result's
# ``reactions`` list -- that matches concierge's own behavioral contract and
# tests. Slack's ``reactions.add`` API takes a SHORTCODE (e.g.
# "thinking_face"), not the glyph itself -- passing the glyph straight
# through errors ``invalid_name``. This module is the render/egress boundary
# where that translation belongs, so concierge never has to know about
# Slack's naming scheme.
_REACTION_SHORTCODES = {
    "🤔": "thinking_face",
    "✅": "white_check_mark",
    "👀": "eyes",
}


def reactions_for(turn_result: dict) -> list[str]:
    """The Slack reaction shortcodes a turn result asked to be added, if
    any -- translated from concierge's emoji glyphs via
    ``_REACTION_SHORTCODES``. A glyph with no known shortcode is skipped
    (never forwarded as-is) so an unmapped value never reaches Slack's API
    and triggers ``invalid_name``."""
    glyphs = turn_result.get("reactions", [])
    return [_REACTION_SHORTCODES[g] for g in glyphs if g in _REACTION_SHORTCODES]


__all__ = ["status_card", "decision_message", "fyi_message", "reactions_for"]
