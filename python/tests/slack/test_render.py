from __future__ import annotations

import sys

from errorta_slack import render


# --- module load safety ----------------------------------------------------


def test_render_module_does_not_import_slack_sdk() -> None:
    """render.py builds plain dicts — it must not pull in slack_sdk at
    module load (that SDK isn't needed to construct Block Kit JSON)."""
    assert "slack_sdk" not in sys.modules or "errorta_slack.render" in sys.modules
    # Stronger check: the module's own globals never bind slack_sdk.
    assert "slack_sdk" not in vars(render)


# --- decision_message --------------------------------------------------


def test_decision_message_has_header_with_decision_needed() -> None:
    blocks = render.decision_message("Ship it?", "PR #42 is ready.", "conf-1")

    headers = [b for b in blocks if b["type"] == "header"]
    assert len(headers) == 1
    header_text = headers[0]["text"]["text"]
    assert "🔴" in header_text
    assert "DECISION NEEDED" in header_text


def test_decision_message_has_two_distinct_buttons_carrying_confirmation_id() -> None:
    confirmation_id = "conf-abc-123"
    blocks = render.decision_message("Ship it?", "PR #42 is ready.", confirmation_id)

    actions_blocks = [b for b in blocks if b["type"] == "actions"]
    assert len(actions_blocks) == 1
    buttons = actions_blocks[0]["elements"]
    assert len(buttons) == 2
    assert all(btn["type"] == "button" for btn in buttons)

    action_ids = {btn["action_id"] for btn in buttons}
    assert action_ids == {"slack_approve", "slack_decline"}

    for btn in buttons:
        assert btn["value"] == confirmation_id


def test_decision_message_section_carries_title_and_detail() -> None:
    blocks = render.decision_message("Ship it?", "PR #42 is ready.", "conf-1")

    sections = [b for b in blocks if b["type"] == "section"]
    assert len(sections) == 1
    text = sections[0]["text"]["text"]
    assert "Ship it?" in text
    assert "PR #42 is ready." in text


# --- fyi_message -------------------------------------------------------


def test_fyi_message_is_a_plain_section_with_no_actions_block() -> None:
    blocks = render.fyi_message("The build finished cleanly.")

    assert all(b["type"] != "actions" for b in blocks)
    sections = [b for b in blocks if b["type"] == "section"]
    assert len(sections) == 1
    assert sections[0]["text"]["text"] == "The build finished cleanly."


def test_fyi_message_has_no_button_elements_anywhere() -> None:
    blocks = render.fyi_message("FYI: nothing to see here.")

    for block in blocks:
        for element in block.get("elements", []):
            assert element.get("type") != "button"


# --- status_card ---------------------------------------------------------


def test_status_card_renders_counts_from_sample_team_log_and_blockers() -> None:
    team_log = [
        {"at": "2026-08-14T00:00:00Z", "role": "dev", "member": "m-1",
         "kind": "task", "message": "Implemented render.py"},
        {"at": "2026-08-14T00:05:00Z", "role": "rev", "member": "m-2",
         "kind": "review", "message": "Approved render.py"},
    ]
    blockers = [{"title": "Waiting on Slack app credentials"}]

    blocks = render.status_card(team_log, blockers)

    assert len(blocks) >= 1
    assert blocks[0]["type"] == "section"
    summary_text = blocks[0]["text"]["text"]
    assert "2" in summary_text
    assert "1" in summary_text

    full_text = " ".join(
        b["text"]["text"] for b in blocks if b.get("type") == "section"
    )
    assert "Waiting on Slack app credentials" in full_text


def test_status_card_with_no_blockers_has_no_blocker_detail_block() -> None:
    team_log = [{"at": "t", "role": "pm", "member": "", "kind": "note",
                 "message": "kicked off"}]

    blocks = render.status_card(team_log, [])

    assert len(blocks) == 1
    assert "0" in blocks[0]["text"]["text"]


def test_status_card_accepts_object_style_blockers_with_title_attr() -> None:
    class _Blocker:
        def __init__(self, title: str) -> None:
            self.title = title

    blocks = render.status_card([], [_Blocker("Needs PM decision")])

    full_text = " ".join(
        b["text"]["text"] for b in blocks if b.get("type") == "section"
    )
    assert "Needs PM decision" in full_text


# --- reactions_for ---------------------------------------------------------


def test_reactions_for_returns_reactions_list_from_turn_result() -> None:
    assert render.reactions_for({"reactions": ["white_check_mark", "eyes"]}) == [
        "white_check_mark",
        "eyes",
    ]


def test_reactions_for_defaults_to_empty_list_when_missing() -> None:
    assert render.reactions_for({}) == []
