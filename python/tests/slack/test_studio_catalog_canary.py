"""Task 8 — studio anti-drift canary.

Mirrors ``tests/slack/test_catalog_canary.py`` (the per-project bridge's
Task 11 canary) one level up: the studio-concierge system prompt must
advertise EXACTLY the verbs ``studio_tools.dispatch`` actually accepts — no
more, no less.

``studio_tools.py`` already asserts ``set(_VERB_IMPLS) == set(TOOL_CATALOG)``
at import time (see the module-level ``assert`` near the bottom of that
file). That catches drift *inside* ``studio_tools.py`` between the catalog
dict and the impl-lookup dict. It does NOT catch drift on the surface a
Slack user's studio-manager turn actually sees: if
``studio_concierge.build_system_prompt`` ever stopped rendering straight
from ``studio_tools.TOOL_CATALOG`` (e.g. someone hand-rolls a subset, or
hardcodes a stale copy), the import-time assert would keep passing while the
prompt quietly lied about what the studio manager can do.

This test closes that gap by parsing the verb set out of the RENDERED
prompt text (not out of ``studio_tools.TOOL_CATALOG`` itself) and asserting
it equals the set ``studio_tools.dispatch`` will actually execute or stage
rather than reject with ``tool_not_allowed``. The two sides being compared
come from independently-maintained surfaces — the rendered prompt STRING vs
the dispatch table (``_VERB_IMPLS``) — exactly like the per-project canary,
so this is not a tautology: it must fail if a verb is added to one side and
not the other, in either direction.
"""
from __future__ import annotations

import re

import pytest

from errorta_slack import studio_concierge, studio_tools

# Matches the exact line shape `studio_concierge.build_system_prompt` renders
# per catalog entry: "- `verb` [R]: summary text". Anchored on the leading
# "- `" / trailing "` [" so it only matches genuine catalog lines, not any
# other backtick-quoted token that might appear elsewhere in the charter-
# intake or etiquette contract text baked into the same prompt.
_CATALOG_LINE = re.compile(r"^- `([a-zA-Z0-9_]+)` \[[A-Z?]\]:", re.MULTILINE)


def _verbs_advertised_in_system_prompt() -> set[str]:
    """Parse the verb set out of the fully-rendered studio-concierge system
    prompt — the literal text a Slack studio-manager turn is shown as "what
    I can do". Deliberately regex-driven off the rendered string rather than
    reading ``studio_tools.TOOL_CATALOG`` directly, so this test exercises
    the prompt surface itself and would still catch drift if the renderer
    ever stopped being a straight pass-through of the catalog.
    """
    prompt = studio_concierge.build_system_prompt()
    verbs = set(_CATALOG_LINE.findall(prompt))
    assert verbs, (
        "no `- `verb` [X]: ...` catalog lines matched in the rendered "
        "studio system prompt — the render format changed; update "
        "_CATALOG_LINE rather than silently passing an empty comparison"
    )
    return verbs


def _verbs_dispatch_accepts() -> set[str]:
    """The set of verbs ``studio_tools.dispatch`` will actually execute (R)
    or stage a confirmation for (C) rather than reject fail-closed with
    ``tool_not_allowed``.

    ``dispatch``'s only "is this verb known" gate is a ``TOOL_CATALOG``
    lookup; once past that gate it calls into ``_VERB_IMPLS[verb]``. Both
    dicts are asserted equal at import time in ``studio_tools.py``, so
    reading ``_VERB_IMPLS`` here reads the real dispatch-accepted set, not a
    second copy of the same source dict this test is trying to cross-check.
    """
    return set(studio_tools._VERB_IMPLS)


def test_advertised_verbs_exactly_match_dispatch_accepted_verbs() -> None:
    advertised = _verbs_advertised_in_system_prompt()
    dispatchable = _verbs_dispatch_accepts()

    assert advertised == dispatchable, (
        "studio_concierge system prompt drifted from studio_tools.dispatch's "
        f"accepted verbs — advertised but not dispatchable: "
        f"{advertised - dispatchable!r}; dispatchable but not advertised: "
        f"{dispatchable - advertised!r}"
    )


def test_canary_catches_a_verb_dropped_from_the_rendered_prompt() -> None:
    """Prove the equality check above is load-bearing, not a tautology.

    Render the prompt from a catalog that is MISSING a verb the real
    ``studio_tools.dispatch`` still accepts (dispatch's accepted set is read
    from the live ``_VERB_IMPLS``, untouched by this perturbation) — the
    same shape of drift the per-project canary guards against: catalog/impl
    stay in sync (the import-time assert in ``studio_tools.py`` would still
    pass) while the PROMPT surface quietly lies about what the bot can do.
    This must fail before the fix and pass after, proving the two sides
    really are independent surfaces rather than the same dict compared to
    itself.
    """
    perturbed_catalog = {
        verb: spec
        for verb, spec in studio_tools.TOOL_CATALOG.items()
        if verb != "create_project"
    }
    assert "create_project" in studio_tools.TOOL_CATALOG, (
        "test fixture assumption broken: 'create_project' must be a real "
        "catalog verb for this perturbation to be meaningful"
    )

    prompt = studio_concierge.build_system_prompt(catalog=perturbed_catalog)
    advertised = set(_CATALOG_LINE.findall(prompt))
    dispatchable = _verbs_dispatch_accepts()

    assert advertised != dispatchable, (
        "expected the perturbed (verb-dropped) prompt to disagree with "
        "dispatch's real accepted set — if they match, the canary above "
        "cannot be trusted to catch this exact class of drift"
    )
    assert "create_project" in dispatchable
    assert "create_project" not in advertised


def test_dispatch_actually_rejects_a_verb_outside_the_catalog() -> None:
    """Belt-and-suspenders: confirm ``dispatch`` really fails closed on an
    unknown verb at runtime (not merely that the catalog dict looks right),
    so the equality check above is comparing against something ``dispatch``
    enforces, not just declares."""
    deps = studio_tools.StudioDeps()

    with pytest.raises(studio_tools.ToolError) as excinfo:
        studio_tools.dispatch(
            "definitely_not_a_real_verb", {},
            channel_id="C1", thread_ts="1000.0", deps=deps,
        )

    assert excinfo.value.code == "tool_not_allowed"
