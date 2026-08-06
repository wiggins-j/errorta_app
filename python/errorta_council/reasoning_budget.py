"""SPEC-42 — the per-turn output budget rule, in one stdlib-only leaf.

A reasoning ("thinking") model spends its output budget on a hidden reasoning trace
*before* the visible answer, so a budget below that trace's length yields a
thinking-burn with no answer. The scheduler has known this since F127 — but the
mitigation lived as class attributes on ``CouncilScheduler`` and the **coding council
never dispatches through it**: every coding turn builds its own request in
``coding/runner.py``'s ``gateway_member_caller``, which hardcoded 2048.

Measured on the RX 9060 XT reference box (`qwen3.5:9b`, `/api/chat`, six cache-busted
trials/arm — see `docs/coding/LOCAL_MODEL_SELECTION_RX9060XT.md` §4.1a): mean
``eval_count`` with thinking on is **2197**, against a 2048 budget. See SPEC-42.

This module is deliberately **stdlib-only and dependency-free** so both the scheduler
and the coding runner can import it without either importing the other. Importing
``scheduler`` from ``coding/runner`` would work (no cycle exists today) but drags
callouts, steward, dialect and topologies into every coding process for two integers
and a substring test.
"""
from __future__ import annotations

from typing import Any

# Model-name substrings that identify reasoning ("thinking") models. Moved verbatim
# from the scheduler so there is exactly one list. Matched case-insensitively against
# the model id.
#
# KNOWN COVERAGE GAP (SPEC-42 Risks, both directions):
#   * false negatives — reasoning models this MISSES, leaving the defect live for them:
#     gpt-oss, magistral, openthinker, smallthinker, exaone-deep, glm-z1,
#     deepseek-v3.1, cogito, deepcoder, granite3.2, seed-oss, minimax-m1, and hosted
#     `o4-mini` / `gpt-5` (only the literal "gpt-5-thinking" is listed).
#   * false positives — non-thinking models that MATCH and would get a wasted 8192:
#     qwen3-coder (instruct-only), qwen3-embedding, qwen3-reranker.
# A capability flag on the route/catalog is the real fix and is out of scope here;
# the substring list is retained so this spec changes budget resolution only.
REASONING_MODEL_MARKERS: tuple[str, ...] = (
    "qwen3", "qwq", "deepseek-r1", "deepseek-reasoner", "r1-", "-r1",
    "thinking", "reasoning", "o1", "o3", "gpt-5-thinking",
)

# Per-turn output budget when a member sets no explicit limit.
DEFAULT_MAX_OUTPUT_TOKENS = 2048
REASONING_MAX_OUTPUT_TOKENS = 8192
# Reasoning models need more wall-clock than a normal turn, or the bigger budget just
# trades a thinking-burn for a timeout.
REASONING_TIMEOUT_FLOOR_SECONDS = 300
# SPEC-42 Move 4: the interactive floor. Deliberately a CEILING against a full 8192
# burn (~370 s at the ~22 tok/s measured on the reference box), not headroom — an
# interactive chat that hangs for six minutes is a worse product than one that reports
# a timeout. The loop path keeps 600 s, which does cover the worst case.
INTERACTIVE_REASONING_TIMEOUT_SECONDS = 240


def is_reasoning_model(model: str) -> bool:
    """Whether a model id looks like a reasoning model. See the coverage gap above."""
    name = str(model or "").lower()
    return any(marker in name for marker in REASONING_MODEL_MARKERS)


def resolve_turn_budget(
    model_id: str,
    *,
    explicit: dict[str, Any] | None = None,
    default_max_output_tokens: int = DEFAULT_MAX_OUTPUT_TOKENS,
    default_timeout_seconds: int = 600,
    enabled: bool = True,
) -> tuple[int, int]:
    """Resolve ``(max_output_tokens, timeout_seconds)`` for one turn.

    ``model_id`` is the ALREADY-RESOLVED provider-side model id. This function does not
    derive one: the coding seam computes it via ``runner._member_model_id`` (which falls
    back to the ``gateway_route_id`` suffix, because a persisted member carries no
    ``model`` key on the very turns the benchmark measured), and a second derivation
    here could silently disagree with the id actually sent.

    **An explicit ``turn_limits`` value always wins** — the operator typed it. The
    member editor's help text tells operators to raise it for reasoning models, so
    treating a persisted value as "probably un-customised" would override a human to
    fix a defect we can fix in code.

    ``enabled=False`` is SPEC-42's escape hatch: skip the model-derived default and
    fall through to the plain ``explicit``-or-default resolution, i.e. exactly today's
    behaviour. It **suppresses** a default; it never imposes one, because imposing the
    legacy literals would demote the real teams that persist 8192/6144.
    """
    limits = explicit or {}

    raw_tokens = limits.get("max_output_tokens")
    if raw_tokens:
        max_output_tokens = int(raw_tokens)
    elif enabled and is_reasoning_model(model_id):
        max_output_tokens = REASONING_MAX_OUTPUT_TOKENS
    else:
        max_output_tokens = int(default_max_output_tokens)

    raw_timeout = limits.get("timeout_seconds")
    if raw_timeout:
        timeout_seconds = int(raw_timeout)
    else:
        timeout_seconds = int(default_timeout_seconds)
        if enabled and is_reasoning_model(model_id):
            timeout_seconds = max(timeout_seconds, REASONING_TIMEOUT_FLOOR_SECONDS)

    return max_output_tokens, timeout_seconds


__all__ = [
    "REASONING_MODEL_MARKERS",
    "DEFAULT_MAX_OUTPUT_TOKENS",
    "REASONING_MAX_OUTPUT_TOKENS",
    "REASONING_TIMEOUT_FLOOR_SECONDS",
    "INTERACTIVE_REASONING_TIMEOUT_SECONDS",
    "is_reasoning_model",
    "resolve_turn_budget",
]
