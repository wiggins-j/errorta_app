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


# --------------------------------------------------------------------------
# Slice 5c Task 7 — the model is told each verb's ARGUMENTS.
#
# The catalog advertised only "- `verb` [T]: summary". The envelope contract
# says '"args": {}' and nothing more, so the model had to guess every argument
# name. On a live run it guessed set_next_goal's and omitted `title`; the ledger
# refused with "focus title is required" and the goal was never set. This is not
# specific to one verb -- it blocks every verb with a required argument.
# --------------------------------------------------------------------------

_ARG_LINE = re.compile(r"^- `([a-zA-Z0-9_]+)` \[[A-Z?]\]:.*— args: (.+)$", re.MULTILINE)


def _rendered_args() -> dict[str, str]:
    prompt = concierge.build_system_prompt("canary-test-project", store=None)
    return dict(_ARG_LINE.findall(prompt))


def test_set_next_goal_advertises_its_required_title() -> None:
    """The exact live failure: the model omitted `title` because nothing ever
    told it the argument existed."""
    args = _rendered_args()

    assert "set_next_goal" in args
    assert "title" in args["set_next_goal"]
    assert "required" in args["set_next_goal"]


def test_every_verb_with_declared_args_renders_them() -> None:
    from errorta_slack import tools

    rendered = _rendered_args()
    for verb, spec in tools.TOOL_CATALOG.items():
        if spec.get("args"):
            assert verb in rendered, f"{verb} declares args but renders none"


def test_verbs_taking_no_args_render_no_args_clause() -> None:
    """A no-arg verb must not grow a confusing empty 'args:' tail."""
    rendered = _rendered_args()

    assert "project_status" not in rendered
    assert "start_run" not in rendered


def test_every_required_arg_is_actually_read_by_its_impl() -> None:
    """Anti-fiction: a declared argument the implementation never reads would
    be a documented lie the model then dutifully sends."""
    import inspect

    from errorta_slack import tools

    for verb, spec in tools.TOOL_CATALOG.items():
        impl = tools._VERB_IMPLS.get(verb)
        if not spec.get("args") or impl is None:
            continue
        src = inspect.getsource(impl)
        for name, _required, _desc in spec["args"]:
            if verb == "publish_pr":
                continue  # passes args straight through to deps.publish_fn
            assert f'"{name}"' in src, f"{verb} advertises {name!r} but never reads it"


# --------------------------------------------------------------------------
# Review fix: the not-done status vocabulary is ONE definition.
#
# It began as two literals -- concierge._FAILED_STATUSES and
# connection._NON_EXECUTION_STATUSES -- plus a third copy written out in the
# honesty-rule prompt text. When "empty"/"not_running" were added to the first,
# the second kept announcing "Autopilot approved & executed start_run" for a
# no-op, and the prompt still named a stale list. A comment saying "keep these
# in step" is precisely what failed.
# --------------------------------------------------------------------------


def test_not_done_statuses_have_a_single_definition() -> None:
    from errorta_slack import concierge, connection, tools

    assert concierge._FAILED_STATUSES is tools.NOT_DONE_STATUSES
    assert connection.SlackBridge._NON_EXECUTION_STATUSES is tools.NOT_DONE_STATUSES


def test_honesty_rule_names_every_not_done_status() -> None:
    """The prompt text is generated, not retyped, so it cannot fall behind."""
    from errorta_slack import concierge, tools

    for status in tools.NOT_DONE_STATUSES:
        assert f'"{status}"' in concierge._RECONCILE_RULE, (
            f"{status!r} is a not-done status but the honesty rule never names it"
        )
