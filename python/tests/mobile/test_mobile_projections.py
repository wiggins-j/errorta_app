"""Mobile projection humanization — digest_v1 → prose for the simple transcript."""
from __future__ import annotations

import json

from errorta_mobile.projections import humanize_credibility, humanize_digest


def test_plain_text_passes_through() -> None:
    assert humanize_digest("Africa has over 2,000 languages.") == "Africa has over 2,000 languages."


def test_non_digest_json_passes_through() -> None:
    # Not a digest_v1 envelope — leave it alone (don't guess).
    s = json.dumps({"foo": "bar"})
    assert humanize_digest(s) == s


def test_digest_v1_envelope_becomes_prose() -> None:
    env = json.dumps({
        "v": "digest_v1",
        "answer_fragment": "There is no single main language.",
        "claims": [{"text": "Africa is linguistically diverse."},
                   {"text": "Swahili and Arabic are widely spoken."}],
        "delta": "no_changed_views",
    })
    out = humanize_digest(env)
    assert "There is no single main language." in out
    assert "• Africa is linguistically diverse." in out
    assert "• Swahili and Arabic are widely spoken." in out
    assert "digest_v1" not in out
    assert "{" not in out  # no raw JSON


def test_fenced_digest_is_unwrapped() -> None:
    env = "```json\n" + json.dumps({"v": "digest_v1", "position": "Use minimal memory."}) + "\n```"
    assert humanize_digest(env).strip() == "Use minimal memory."


def test_digest_with_preamble_extracted() -> None:
    env = "Here is my structured reply:\n" + json.dumps({"v": "digest_v1", "answer_fragment": "Evict lowest scores."})
    assert "Evict lowest scores." in humanize_digest(env)
    assert "digest_v1" not in humanize_digest(env)


def test_credibility_claim_packet_becomes_prose_with_citation() -> None:
    env = json.dumps({
        "answer_fragment": "Justin Gaethje",
        "claims": [{
            "claim_id": "c1",
            "text": "Justin Gaethje won the lightweight title.",
            "source_ids": ["https://www.encyclopediaofalabama.org/x"],
        }],
    })
    out = humanize_credibility(env)
    assert "Justin Gaethje won the lightweight title. (encyclopediaofalabama.org)" in out
    assert '"answer_fragment"' not in out


def test_credibility_discussion_prefers_the_members_comment() -> None:
    env = "```json\n" + json.dumps({
        "comment": "Claude makes the stronger materialist case, but I'd push back on c2.",
        "reviews": [{"claim_id": "Claude:c1", "status": "verified"}],
    }) + "\n```"
    out = humanize_credibility(env)
    assert "Claude makes the stronger materialist case" in out
    assert "Claude:c1" not in out  # the structured tally stays under the hood


def test_credibility_review_summary_is_plain_english_without_comment() -> None:
    env = json.dumps({"reviews": [
        {"claim_id": "GPT:c1", "status": "verified"},
        {"claim_id": "GPT:c2", "status": "contradicted"},
    ]})
    out = humanize_credibility(env)
    assert "I agree with GPT:c1" in out
    assert "I disagree with GPT:c2" in out
    assert "verified — GPT:c1" not in out


def test_credibility_skips_minted_source_id_citation() -> None:
    env = json.dumps({"claims": [
        {"claim_id": "c1", "text": "A fact.", "source_ids": ["src_0001"]}
    ]})
    # A minted id is not a website — no parenthetical host.
    assert humanize_credibility(env).strip() == "• A fact."


def test_credibility_non_credibility_text_passes_through() -> None:
    assert humanize_credibility("Just a normal answer.") == "Just a normal answer."


# --- F080 neutral judge ---------------------------------------------------

def _judge_event(verdict, reason="because"):
    from errorta_council.schema import CouncilEvent, EventStatus, EventType
    return CouncilEvent(
        format_version=1, id="e1", run_id="r1", sequence=1,
        type=EventType.JUDGE_VERDICT, status=EventStatus.COMPLETED,
        created_at="2026-06-15T00:00:00Z", member_id="m-judge", round=1,
        payload={"verdict": verdict, "reason": reason},
    )


def test_judge_decisive_verdict_projects_a_body():
    from errorta_mobile.projections import event_projection
    out = event_projection(_judge_event("reached", "converged on LRU"))
    assert out["mobile_visibility"] == "visible"
    assert "Judge" in out["body"]["text"]
    assert "members reached a verdict" in out["body"]["text"]
    assert "converged on LRU" in out["body"]["text"]


def test_judge_continue_verdict_is_hidden():
    from errorta_mobile.projections import event_projection
    out = event_projection(_judge_event("continue"))
    assert out["body"] is None
    assert out["mobile_visibility"] == "metadata"
