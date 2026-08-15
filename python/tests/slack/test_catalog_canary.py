"""Task 11 — anti-drift canary.

The concierge system prompt must advertise EXACTLY the verbs
``tools.dispatch`` actually accepts — no more, no less.

``tools.py`` already asserts ``set(_VERB_IMPLS) == set(TOOL_CATALOG)`` at
import time (see the module-level ``assert`` at the bottom of that file).
That catches drift *inside* ``tools.py`` between the catalog dict and the
impl-lookup dict. It does NOT catch drift on the surface a Slack user's
model turn actually sees: if ``concierge.build_system_prompt`` ever stopped
rendering straight from ``tools.TOOL_CATALOG`` (e.g. someone hand-rolls a
subset, or hardcodes a stale copy), the import-time assert would keep
passing while the prompt quietly lied about what the bot can do.

This test closes that gap by parsing the verb set out of the RENDERED
prompt text (not out of ``tools.TOOL_CATALOG`` itself) and asserting it
equals the set ``tools.dispatch`` will actually execute or stage rather
than reject with ``tool_not_allowed``. It must fail if a verb is added to
one side and not the other, in either direction.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from errorta_slack import concierge, tools


@pytest.fixture(autouse=True)
def _isolated_errorta_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("ERRORTA_HOME", str(tmp_path))
    return tmp_path


# Matches the exact line shape `concierge.build_system_prompt` renders per
# catalog entry: "- `verb` [R]: summary text". Anchored on the leading
# "- `" / trailing "` [" so it only matches genuine catalog lines, not any
# other backtick-quoted token that might appear elsewhere in the PM
# reference manual text baked into the same prompt.
_CATALOG_LINE = re.compile(r"^- `([a-zA-Z0-9_]+)` \[[A-Z?]\]:", re.MULTILINE)


def _verbs_advertised_in_system_prompt() -> set[str]:
    """Parse the verb set out of the fully-rendered concierge system
    prompt — the literal text a Slack conversation's model turn is shown as
    "what I can do". Deliberately regex-driven off the rendered string
    rather than reading ``tools.TOOL_CATALOG`` directly, so this test
    exercises the prompt surface itself and would still catch drift if the
    renderer ever stopped being a straight pass-through of the catalog.
    """
    prompt = concierge.build_system_prompt("canary-test-project", store=None)
    verbs = set(_CATALOG_LINE.findall(prompt))
    assert verbs, (
        "no `- `verb` [X]: ...` catalog lines matched in the rendered "
        "system prompt — the render format changed; update _CATALOG_LINE "
        "rather than silently passing an empty comparison"
    )
    return verbs


def _verbs_dispatch_accepts() -> set[str]:
    """The set of verbs ``tools.dispatch`` will actually execute (R) or
    stage a confirmation for (C) rather than reject fail-closed with
    ``tool_not_allowed``.

    ``dispatch``'s only "is this verb known" gate is a ``TOOL_CATALOG``
    lookup; once past that gate it calls into ``_VERB_IMPLS[verb]``. Both
    dicts are asserted equal at import time in ``tools.py``, so reading
    ``_VERB_IMPLS`` here reads the real dispatch-accepted set, not a second
    copy of the same source dict this test is trying to cross-check.
    """
    return set(tools._VERB_IMPLS)


def test_advertised_verbs_exactly_match_dispatch_accepted_verbs() -> None:
    advertised = _verbs_advertised_in_system_prompt()
    dispatchable = _verbs_dispatch_accepts()

    assert advertised == dispatchable, (
        "concierge system prompt drifted from tools.dispatch's accepted "
        f"verbs — advertised but not dispatchable: {advertised - dispatchable!r}; "
        f"dispatchable but not advertised: {dispatchable - advertised!r}"
    )


def test_dispatch_actually_rejects_a_verb_outside_the_catalog() -> None:
    """Belt-and-suspenders: confirm ``dispatch`` really fails closed on an
    unknown verb at runtime (not merely that the catalog dict looks right),
    so the equality check above is comparing against something ``dispatch``
    enforces, not just declares."""
    deps = tools.ToolDeps()

    with pytest.raises(tools.ToolError) as excinfo:
        tools.dispatch(
            "definitely_not_a_real_verb", {},
            channel_id="C1", thread_ts="1000.0", deps=deps,
        )

    assert excinfo.value.code == "tool_not_allowed"
