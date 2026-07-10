from errorta_council.coding.model_catalog import load_catalog
from errorta_council.coding.model_selector import NoCapableModel, select


def test_selector_chooses_lowest_cost_after_capability_filter() -> None:
    pool = ["anthropic.haiku", "openai.gpt-5", "claude_cli.opus"]
    result = select(pool, set(pool), load_catalog(pool), "mid")
    assert not isinstance(result, NoCapableModel)
    assert result.route_id == "claude_cli.opus"


def test_escalation_requires_strictly_stronger_capability() -> None:
    pool = ["claude_cli.sonnet", "anthropic.haiku", "openai.gpt-5-high"]
    result = select(
        pool, set(pool), load_catalog(pool), "mid", minimum_rank_exclusive=1,
    )
    assert not isinstance(result, NoCapableModel)
    assert result.route_id == "openai.gpt-5-high"


def test_negative_corpus_can_demote_but_not_promote() -> None:
    pool = ["openai.gpt-5"]
    digest = {"openai.gpt-5": {"implementation:mid": {"attempts": 5, "accepted_rate": 0.2}}}
    result = select(pool, set(pool), load_catalog(pool), "mid", corpus_digest=digest)
    assert isinstance(result, NoCapableModel)
