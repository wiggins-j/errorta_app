"""F127 Workstream C — model-tier awareness (pure, table-driven, no network).

Errorta's coding teams are heterogeneous on purpose: a STRONG model on the PM
(context delivery, requirements, review synthesis) and slightly weaker/cheaper
models on the workers. When a worker keeps producing unusable turns, the
escalation ladder reassigns its task UP — to the highest-tier idle member of the
role. This module gives the scheduler the coarse notion of "strength" it needs.

Tiers (high -> low): ``strong > mid > light``. The mapping is deliberately coarse
and substring-based off the ``gateway_route_id`` family, with a safe default of
``mid`` (never assume a worker is light or strong without evidence). Members may
override via ``metadata.model_tier``. Provider churns its model names, so this is
guidance, not a contract — see F127 D4."""
from __future__ import annotations

import os
import re
from typing import Any

STRONG = "strong"
MID = "mid"
LIGHT = "light"

_TIER_RANK = {LIGHT: 0, MID: 1, STRONG: 2}

# Order matters: the FIRST family whose token is a substring of the lowercased
# model id wins. `light`/`strong` markers are checked before the broad `mid`
# fallbacks so e.g. `gpt-5.3-codex-low` -> light and `...-high` -> strong.
_LIGHT_MARKERS = ("haiku", "-mini", "-nano", "-low", "-flash", "lite")
_STRONG_MARKERS = ("opus", "-high", "-xhigh", "-thinking", "-max")
_MID_MARKERS = ("sonnet", "gpt-5", "gpt-4", "gemini", "-pro", "grok", "composer", "codex")


# --- issue #83 parameter parsing (moved DOWN from model_catalog by SPEC-44) --- #
# `model_catalog` imports THIS module, so the parser had to move down rather than
# the tier logic moving up. `model_catalog` re-exports all four names unchanged, and
# `tests/coding/test_issue83_model_size_hints.py` still imports them from there.
#
# Parameter count in a model id, ANCHORED. The previous implementation tested bare
# substrings ("3b", "7b", "70b"), which is wrong for every two-digit size that ends
# in one of them: "27b" contains "7b" and "13b" contains "3b", so `gemma3:27b` and
# `deepseek-r1:13b` were both tagged smallest-and-fastest. Since every local route
# ties on cost_tier=0 and capability_tier=mid, selection falls through to size_rank —
# so the mis-hint actively chose the LARGEST model as the cheapest, i.e. the one that
# does not fit in 16 GB VRAM. Requiring a separator before the digits and a word
# boundary after the "b" makes the match positional rather than incidental.
_PARAM_BILLIONS_RE = re.compile(r"[:\-_/](\d+(?:\.\d+)?)b\b")

# Bucket edges in billions of parameters. These decide relative ORDERING only —
# they are NOT a VRAM fit check (nothing here knows the card). 24B is the large
# edge rather than 32B so that a 27B sorts ABOVE a 14B: on the 16 GB reference card
# a 27B (~17 GB) is the model that does not fit, and ranking it as merely "medium"
# is what let the selector reach for it.
#
# SPEC-44 reuses `_SMALL_MAX_B` — and ONLY `_SMALL_MAX_B` — as the local capability
# edge. `_LARGE_MIN_B` is deliberately NOT a `strong` edge: on the 16 GB reference
# card the only local models a `>=24B -> strong` rule would promote are the ones that
# do not fit (gemma3:27b is ~17 GB against 15.9 GiB usable), and because the escalate
# rung admits only strictly-stronger routes, a count-derived `strong` would aim the
# ladder's designed target at an OOM. Parameter count is an ORDERING signal; VRAM fit
# is a DEPLOYMENT fact nothing in the catalog knows. `strong` for a local route comes
# only from an explicit `model-catalog-overrides.json` entry.
_SMALL_MAX_B = 8.0
_LARGE_MIN_B = 24.0


def param_billions(route_id: str) -> float | None:
    """Parameter count parsed from a model id, or ``None`` when it declares none.

    ``local.qwen2.5-coder:7b`` -> 7.0; ``local.gemma3:27b`` -> 27.0;
    ``local.mistral-small3.1:latest`` -> None (the "3.1" is a version, not a size).
    """
    m = _PARAM_BILLIONS_RE.search(str(route_id or "").lower())
    if not m:
        return None
    try:
        return float(m.group(1))
    except ValueError:  # pragma: no cover — the regex only matches numerics
        return None


# SPEC-44 Move 1: derive a LOCAL route's capability tier from its declared parameter
# count. Off by default — the disable value restores `tier_for_route` returning MID
# for every `local.*` route before any name inspection, byte for byte.
_SIZE_TIERS_ENV = "ERRORTA_LOCAL_SIZE_TIERS"
_OFF = {"", "0", "false", "no", "off"}


def _size_tiers_enabled() -> bool:
    """Read on every call, deliberately uncached: a module-level constant makes the
    knob untestable via monkeypatch and unswitchable without a process restart."""
    return os.environ.get(_SIZE_TIERS_ENV, "").strip().lower() not in _OFF


def _model_id(route_id: str) -> str:
    rid = (route_id or "").strip().lower()
    return rid.split(".", 1)[1] if "." in rid else rid


def tier_for_route(route_id: str) -> str:
    """Coarse tier for a gateway route id. Default ``mid`` (never assume)."""
    rid = (route_id or "").strip().lower()
    if not rid:
        return MID
    if rid.startswith("fake."):
        # The test seam. NEVER derived, with the knob on or off — the suite relies
        # on fake routes tying at mid.
        return MID
    if rid.startswith("local."):
        if not _size_tiers_enabled():
            return MID
        billions = param_billions(rid)
        if billions is None:
            return MID  # never assume
        return LIGHT if billions <= _SMALL_MAX_B else MID
    model = _model_id(route_id)
    # Strong markers win over the broad mid fallbacks (a "-high" gpt is strong),
    # but an explicit light marker (e.g. a "-low" codex) wins over everything.
    if any(m in model for m in _LIGHT_MARKERS):
        return LIGHT
    if any(m in model for m in _STRONG_MARKERS):
        return STRONG
    if any(m in model for m in _MID_MARKERS):
        return MID
    return MID


def tier_rank(tier: str) -> int:
    """Numeric rank for ordering (higher = stronger). Unknown -> mid's rank."""
    return _TIER_RANK.get(tier, _TIER_RANK[MID])


def member_tier(member: dict[str, Any]) -> str:
    """A member's tier: explicit ``metadata.model_tier`` override, else derived
    from ``gateway_route_id``."""
    md = member.get("metadata") if isinstance(member, dict) else None
    if isinstance(md, dict):
        override = str(md.get("model_tier", "") or "").strip().lower()
        if override in _TIER_RANK:
            return override
    return tier_for_route(str((member or {}).get("gateway_route_id", "") or ""))


def member_rank(member: dict[str, Any]) -> int:
    return tier_rank(member_tier(member))


__all__ = [
    "STRONG", "MID", "LIGHT",
    "param_billions",
    "tier_for_route", "tier_rank", "member_tier", "member_rank",
]
