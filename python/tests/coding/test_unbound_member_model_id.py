"""An unbound member must still carry a model id to the gateway.

`gateway_member_caller` (`coding/runner.py:7583`) builds its request with

    model=str(member.get("model") or member.get("model_display") or "")

but a PERSISTED coding member has neither key: `recipes.resolve_team`
(`coding/recipes.py:64-78`) writes only `id`/`role`/`enabled`/`metadata`/
`model_mode`/`gateway_route_id`, and real `run_config.json` files confirm
`model: None`.

The only thing that populates `model` is `bind_member_route`
(`model_assignment.py:51-66`, line 62), and it has exactly ONE call site —
`runner.py:6358`, the **Assign** (worker task) path. PM governance turns
(`runner.py:5508`) and governance review turns (`runner.py:5635`) call
`_member(...)` (`runner.py:5406-5421`), which returns the raw persisted dict.

So on an all-local team those turns sent `model=""`. For a `local.*` route
`LocalGateway.call` does NOT divert to `_registry_dispatch` — which would have
recovered the model from the route id (`gateway_local.py:285`) — because
`provider_class == "local"` is in `_ALLOWED_PROVIDERS`; it falls through to
`_ollama_dispatch`, which posts `body["model"] = ""`.

Measured against the reference box (senditai, Ollama):

    POST /api/chat {"model": "", ...}  ->  HTTP 400 {"error":"model is required"}

`_ollama_dispatch` maps that to `FatalError("gateway_4xx: 400")` — the message
does not contain "not found", so it is not even reported as `model_not_found`.

The fix derives the model id from the route suffix, the same derivation
`bind_member_route` uses, so an unbound member behaves like a bound one.
"""
from __future__ import annotations

from typing import Any

import pytest

from errorta_council.coding.recipes import resolve_team
from errorta_council.coding.runner import gateway_member_caller


class _CapturingGateway:
    """Records the request instead of dispatching it."""

    def __init__(self) -> None:
        self.requests: list[Any] = []

    async def call(self, request: Any) -> Any:
        self.requests.append(request)

        class _Result:
            content = "{}"
            provider_class = "local"
            model = request.model
            input_tokens = None
            output_tokens = None
            cache_read_input_tokens = None
            cache_write_input_tokens = None
            raw_usage_available = False
            num_turns = None
            duration_ms = 0
        return _Result()


def _persisted_member(route: str) -> dict[str, Any]:
    """Exactly the shape `recipes.resolve_team` persists — no model key."""
    return {
        "id": "pm-1", "role": "answerer", "enabled": True,
        "metadata": {"coding_role": "pm"},
        "model_mode": "single",
        "gateway_route_id": route,
    }


def test_persisted_member_really_has_no_model_key() -> None:
    """Guard the premise: if the team builder ever starts writing `model`,
    this test's whole basis is gone and it should be revisited."""
    team = resolve_team("private_offline", [
        {"route_id": "local.qwen2.5-coder:7b", "provider_class": "local"},
        {"route_id": "local.qwen3.5:9b", "provider_class": "local"},
    ])
    assert team, "resolve_team returned no members"
    for m in team:
        assert "model" not in m, f"{m['id']} now carries a model key: {m!r}"
        assert "model_display" not in m, f"{m['id']} now carries model_display"
        assert m.get("gateway_route_id"), f"{m['id']} has no route"


@pytest.mark.parametrize("route,expected", [
    ("local.qwen2.5-coder:7b", "qwen2.5-coder:7b"),
    ("local.qwen3.5:9b", "qwen3.5:9b"),
    ("local.gemma3:27b", "gemma3:27b"),
    ("local.mistral-small3.1:latest", "mistral-small3.1:latest"),
])
def test_unbound_local_member_sends_a_real_model_id(route: str,
                                                    expected: str) -> None:
    """THE REGRESSION. An unbound PM member on a local.* route must not send "".

    Ollama rejects an empty model with HTTP 400 "model is required", which
    `_ollama_dispatch` surfaces as `FatalError("gateway_4xx: 400")` — a hard turn
    failure on every PM-governance and governance-review turn of an all-local team.
    """
    gw = _CapturingGateway()
    caller = gateway_member_caller(gw)
    caller(_persisted_member(route), "hello")
    assert gw.requests, "the gateway was never called"
    assert gw.requests[0].model == expected, (
        f"unbound member on {route} sent model={gw.requests[0].model!r}")


def test_explicit_model_still_wins_over_the_route_suffix() -> None:
    """A bound member (or an operator-set model) must not be overridden.

    `bind_member_route` sets BOTH `model` and `gateway_route_id` to the same
    route, so this only bites if they ever disagree — in which case the explicit
    value is the intentional one.
    """
    gw = _CapturingGateway()
    member = _persisted_member("local.qwen2.5-coder:7b")
    member["model"] = "deliberately-different"
    gateway_member_caller(gw)(member, "hello")
    assert gw.requests[0].model == "deliberately-different"


def test_model_display_is_still_honoured() -> None:
    gw = _CapturingGateway()
    member = _persisted_member("local.qwen3.5:9b")
    member["model_display"] = "shown-name"
    gateway_member_caller(gw)(member, "hello")
    assert gw.requests[0].model == "shown-name"


def test_hosted_routes_are_unchanged() -> None:
    """Hosted routes were never broken — `_registry_dispatch` already derives the
    model from the route id (`gateway_local.py:285`). Deriving it here too is
    consistent, and must not change what those providers receive.
    """
    gw = _CapturingGateway()
    gateway_member_caller(gw)(_persisted_member("anthropic.claude-sonnet-4-6"),
                              "hello")
    assert gw.requests[0].model == "claude-sonnet-4-6"


def test_route_without_a_prefix_degrades_to_the_route_itself() -> None:
    """`bind_member_route` (`model_assignment.py:54-56`) treats a prefix-less route
    as the model id. Match that rather than inventing a third convention."""
    gw = _CapturingGateway()
    gateway_member_caller(gw)(_persisted_member("r1"), "hello")
    assert gw.requests[0].model == "r1"


def test_no_route_and_no_model_stays_empty() -> None:
    """With nothing to derive from, the old behaviour stands — this fix must not
    invent a model id out of nothing."""
    gw = _CapturingGateway()
    member = {"id": "pm-1", "role": "answerer", "metadata": {"coding_role": "pm"}}
    gateway_member_caller(gw)(member, "hello")
    assert gw.requests[0].model == ""
