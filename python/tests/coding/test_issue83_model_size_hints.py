"""Issue #83 — anchored parameter-count parsing in the model catalog.

`_default_hints` matched bare substrings ("3b", "7b", "70b"), so every two-digit
size ending in one of them was mis-tagged as smallest-and-fastest: "27b" contains
"7b" and "13b" contains "3b". Because every local route ties on `cost_tier=0` and
(issue #82) on `capability_tier=mid`, selection falls through to `size_rank` — so
the bad hint actively selected the LARGEST model as the cheapest. On the 16 GB
reference card that is `gemma3:27b` (~17 GB), i.e. the one model that cannot fit,
which means CPU offload and an effectively hung run.

These cases are written against REAL model ids — the ones actually pulled on the
senditai reference box plus the families named in the issue — because the bug was
invisible to synthetic ids like "small"/"large".
"""
from __future__ import annotations

import pytest

from errorta_council.coding.model_catalog import (
    _LARGE_MIN_B,
    _SMALL_MAX_B,
    default_entry,
    param_billions,
)

# Exactly what `ollama list` reports on senditai (RX 9060 XT 16 GB), plus the
# collision families the issue calls out.
_REAL_IDS = [
    # (route_id, expected_billions)
    ("local.qwen2.5-coder:14b", 14.0),
    ("local.qwen2.5-coder:7b", 7.0),
    ("local.qwen2.5-coder:1.5b-base", 1.5),
    ("local.nomic-embed-text:latest", None),
    ("local.gemma3:27b", 27.0),
    ("local.mistral-small3.1:latest", None),   # "3.1" is a version, not a size
    ("local.qwen3.5:9b", 9.0),
    ("local.qwen2.5:3b", 3.0),
    ("local.qwen2.5:3b-instruct-q4_K_M", 3.0),
    ("local.llama3:70b", 70.0),
    ("local.deepseek-r1:13b", 13.0),
    ("local.deepseek-r1:1.5b", 1.5),
]


@pytest.mark.parametrize("route_id,expected", _REAL_IDS)
def test_param_billions_on_real_model_ids(route_id: str, expected) -> None:
    assert param_billions(route_id) == expected


@pytest.mark.parametrize("route_id,digits", [
    ("local.gemma3:27b", "7b"),
    ("local.deepseek-r1:13b", "3b"),
    ("local.foo:17b", "7b"),
    ("local.foo:23b", "3b"),
    ("local.foo:37b", "7b"),
    ("local.foo:47b", "7b"),
    ("local.foo:53b", "3b"),
])
def test_two_digit_sizes_are_not_mistaken_for_small(route_id: str,
                                                    digits: str) -> None:
    """THE regression. Each of these literally contains a small-size substring."""
    assert digits in route_id  # the collision the old code tripped on
    size, speed = default_entry(route_id).size_rank, default_entry(route_id).speed_rank
    assert size > 0, f"{route_id} was tagged smallest via the {digits!r} substring"
    assert speed > 0, f"{route_id} was tagged fastest via the {digits!r} substring"


def test_genuinely_small_models_still_rank_small() -> None:
    """The fix must not over-correct — a real 7b is still the cheap one."""
    for rid in ("local.qwen2.5-coder:7b", "local.qwen2.5:3b",
                "local.qwen2.5-coder:1.5b-base"):
        e = default_entry(rid)
        assert (e.size_rank, e.speed_rank) == (0, 0), rid


def test_27b_outranks_14b_so_the_selector_prefers_the_smaller() -> None:
    """The selection-relevant ordering, stated directly.

    With four local models pooled the issue measured `difficulty=light` and
    `difficulty=mid` BOTH resolving to `local.gemma3:27b`. Ordering is what fixes
    that, so assert the ordering rather than the bucket numbers.
    """
    big = default_entry("local.gemma3:27b")
    mid = default_entry("local.qwen2.5-coder:14b")
    small = default_entry("local.qwen2.5-coder:7b")
    assert small.size_rank < mid.size_rank <= big.size_rank
    assert small.size_rank < big.size_rank


def test_hosted_size_vocabulary_is_unchanged() -> None:
    """Non-local routes have no parameter count and must fall back as before."""
    assert default_entry("anthropic.claude-haiku-4-5").size_rank == 0
    assert default_entry("openai.gpt-5-nano").size_rank == 0
    assert default_entry("google.gemini-2.5-flash").size_rank == 0
    assert default_entry("anthropic.claude-opus-4-1").size_rank == 2


def test_no_param_count_falls_back_to_capability() -> None:
    """A model that declares no size is ranked by its capability tier, not guessed."""
    assert param_billions("local.mistral-small3.1:latest") is None
    e = default_entry("local.mistral-small3.1:latest")
    assert e.size_rank == e.speed_rank  # symmetric fallback, whatever the tier


def test_bucket_edges_are_inclusive_as_documented() -> None:
    assert default_entry(f"local.x:{int(_SMALL_MAX_B)}b").size_rank == 0
    assert default_entry(f"local.x:{int(_SMALL_MAX_B) + 1}b").size_rank == 1
    assert default_entry(f"local.x:{int(_LARGE_MIN_B)}b").size_rank == 2
    assert default_entry(f"local.x:{int(_LARGE_MIN_B) - 1}b").size_rank == 1


def test_version_numbers_are_not_read_as_sizes() -> None:
    """`llama3.1`, `qwen2.5`, `mistral-small3.1` must not yield a param count from
    their version digits — only a `:`/`-`/`_`-separated `<n>b` token counts."""
    assert param_billions("local.llama3.1:latest") is None
    assert param_billions("local.qwen2.5:latest") is None
    assert param_billions("local.gemma3:latest") is None
