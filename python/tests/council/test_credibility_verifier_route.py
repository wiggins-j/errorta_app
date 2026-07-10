"""Regression: the entailment verifier must NOT fall back to a CLI/remote
provider (it spawned a subprocess storm that starved the sidecar →
sidecar_unreachable on a concurrent save). No local route ⇒ {} ⇒ gate skips."""
from __future__ import annotations

from errorta_council.scheduler import TurnScheduler


class _Meta:
    id = "r1"
    def __init__(self, snap): self.room_snapshot = snap


def _m(mid, provider, route=None):
    return {"id": mid, "enabled": True, "provider": provider,
            "gateway_route_id": route or f"{provider}.default"}


def _sched(members, cred=None):
    s = TurnScheduler.__new__(TurnScheduler)
    s._meta = _Meta({"members": members, "credibility_policy": cred or {}})
    return s


def test_all_cli_members_no_verifier_route():
    s = _sched([_m("A", "codex_cli"), _m("B", "codex_cli")])
    assert s._credibility_verifier_route() == {}  # no storm


def test_prefers_local_member():
    s = _sched([_m("A", "codex_cli"), _m("B", "local", "local.ollama.qwen")])
    assert s._credibility_verifier_route().get("id") == "B"


def test_explicit_verifier_route_id_wins():
    s = _sched(
        [_m("A", "codex_cli"), _m("B", "local", "local.ollama.qwen")],
        cred={"verifier_route_id": "local.ollama.qwen"},
    )
    assert s._credibility_verifier_route().get("id") == "B"
