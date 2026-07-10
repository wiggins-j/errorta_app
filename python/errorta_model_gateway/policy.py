"""Model gateway policy and egress compatibility rules."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any

GlobalMode = str
Provider = str
Role = str
CorpusEgressPolicy = str

VALID_GLOBAL_MODES = {"local_only", "you_pick", "user_selected"}
VALID_PROVIDERS = {
    "off",
    "local",
    "anthropic",
    "openai",
    "google",
    "custom",
    "claude_cli",
    "codex_cli",
    "cursor_cli",
}
VALID_ROLES = {
    "answerer",
    "judge",
    "verifier",
    "hyde",
    "fallback",
    "benchmark",
    "brief_planner",
}
VALID_EGRESS_POLICIES = {
    "local_only",
    "prompt_only",
    "redacted_support",
    "retrieved_snippets",
    "answer_context",
}
REMOTE_PROVIDERS = {
    "anthropic",
    "openai",
    "google",
    "custom",
    "claude_cli",
    "codex_cli",
    "cursor_cli",
}

DEFAULT_ROLE_ROUTES: dict[str, str] = {
    "answerer": "local",
    "judge": "local",
    "verifier": "off",
    "hyde": "local",
    "fallback": "off",
    "benchmark": "local",
    "brief_planner": "local",
}

POLICY_ALLOWED_ROLES: dict[str, set[str]] = {
    "local_only": set(),
    "prompt_only": {"brief_planner"},
    "redacted_support": {"judge", "verifier"},
    "retrieved_snippets": {"judge", "verifier", "fallback"},
    "answer_context": {"answerer", "judge", "verifier", "fallback"},
}

POLICY_ALLOWED_FIELDS: dict[str, set[str]] = {
    "local_only": set(),
    "prompt_only": {"prompt"},
    "redacted_support": {
        "prompt",
        "answer",
        "verdict_schema",
        "citation_ids",
        "redacted_snippets",
    },
    "retrieved_snippets": {
        "prompt",
        "answer",
        "verdict_schema",
        "citation_ids",
        "redacted_snippets",
        "retrieved_snippets",
        "source_metadata",
    },
    "answer_context": {
        "prompt",
        "answer",
        "verdict_schema",
        "citation_ids",
        "redacted_snippets",
        "retrieved_snippets",
        "source_metadata",
        "retrieved_chunks",
        "answer_context",
    },
}


def _now_iso_z() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z"


def _as_int_or_none(value: Any) -> int | None:
    if value is None:
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return max(0, parsed)


def _as_float_or_none(value: Any) -> float | None:
    if value is None:
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return max(0.0, parsed)


@dataclass(frozen=True)
class RoutePolicy:
    provider: Provider = "local"
    model: str | None = None
    enabled: bool = True

    @classmethod
    def from_dict(cls, value: Any, *, fallback_provider: str = "local") -> "RoutePolicy":
        if isinstance(value, str):
            value = {"provider": value}
        if not isinstance(value, dict):
            value = {}
        provider = str(value.get("provider") or fallback_provider).strip().lower()
        if provider not in VALID_PROVIDERS:
            provider = fallback_provider
        model = value.get("model")
        return cls(
            provider=provider,
            model=str(model).strip() or None if model is not None else None,
            enabled=bool(value.get("enabled", True)),
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class BudgetPolicy:
    max_tokens_per_call: int | None = 4096
    max_remote_tokens_per_day: int | None = None
    max_remote_calls_per_day: int | None = 0
    max_remote_calls_per_session: int | None = 0
    max_usd_per_month: float | None = 0.0
    hard_stop: bool = True

    @classmethod
    def from_dict(cls, value: Any) -> "BudgetPolicy":
        if not isinstance(value, dict):
            value = {}
        if "max_tokens_per_call" in value:
            max_tokens = value.get("max_tokens_per_call")
        else:
            max_tokens = value.get("max_input_tokens_per_call", 4096)

        if "max_remote_tokens_per_day" in value:
            max_remote_tokens = value.get("max_remote_tokens_per_day")
        else:
            max_remote_tokens = value.get("daily_token_cap")

        if "max_usd_per_month" in value:
            max_usd = value.get("max_usd_per_month")
        else:
            max_usd = value.get("monthly_estimated_usd_cap", 0.0)
        return cls(
            max_tokens_per_call=_as_int_or_none(max_tokens),
            max_remote_tokens_per_day=_as_int_or_none(max_remote_tokens),
            max_remote_calls_per_day=_as_int_or_none(
                value.get("max_remote_calls_per_day", 0)
            ),
            max_remote_calls_per_session=_as_int_or_none(
                value.get("max_remote_calls_per_session", 0)
            ),
            max_usd_per_month=_as_float_or_none(max_usd),
            hard_stop=bool(value.get("hard_stop", True)),
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class GatewayPolicy:
    global_mode: GlobalMode = "local_only"
    role_routes: dict[str, RoutePolicy] = field(default_factory=dict)
    corpus_policies: dict[str, CorpusEgressPolicy] = field(default_factory=dict)
    budget: BudgetPolicy = field(default_factory=BudgetPolicy)
    updated_at: str | None = None

    @classmethod
    def default(cls) -> "GatewayPolicy":
        routes = {
            role: RoutePolicy(provider=provider)
            for role, provider in DEFAULT_ROLE_ROUTES.items()
        }
        return cls(
            global_mode="local_only",
            role_routes=routes,
            corpus_policies={},
            budget=BudgetPolicy(),
            updated_at=None,
        )

    @classmethod
    def from_dict(cls, value: Any) -> "GatewayPolicy":
        if not isinstance(value, dict):
            return cls.default()

        global_mode = str(value.get("global_mode") or "local_only").strip().lower()
        if global_mode not in VALID_GLOBAL_MODES:
            global_mode = "local_only"
        if global_mode == "user_selected":
            global_mode = "you_pick"

        incoming_routes = value.get("role_routes") or {}
        if not isinstance(incoming_routes, dict):
            incoming_routes = {}
        routes: dict[str, RoutePolicy] = {}
        for role in VALID_ROLES:
            fallback = DEFAULT_ROLE_ROUTES.get(role, "local")
            routes[role] = RoutePolicy.from_dict(
                incoming_routes.get(role),
                fallback_provider=fallback,
            )

        incoming_corpus = value.get("corpus_policies") or {}
        corpus_policies: dict[str, CorpusEgressPolicy] = {}
        if isinstance(incoming_corpus, dict):
            for name, policy in incoming_corpus.items():
                corpus_name = str(name).strip()
                policy_name = str(policy or "local_only").strip().lower()
                if corpus_name and policy_name in VALID_EGRESS_POLICIES:
                    corpus_policies[corpus_name] = policy_name

        updated_at = value.get("updated_at")
        return cls(
            global_mode=global_mode,
            role_routes=routes,
            corpus_policies=corpus_policies,
            budget=BudgetPolicy.from_dict(value.get("budget")),
            updated_at=str(updated_at) if updated_at else None,
        )

    def with_timestamp(self) -> "GatewayPolicy":
        return GatewayPolicy(
            global_mode=self.global_mode,
            role_routes=self.role_routes,
            corpus_policies=self.corpus_policies,
            budget=self.budget,
            updated_at=_now_iso_z(),
        )

    def route_for(self, role: str) -> RoutePolicy:
        normalized = (role or "").strip().lower()
        if normalized not in VALID_ROLES:
            normalized = "answerer"
        return self.role_routes.get(
            normalized,
            RoutePolicy(provider=DEFAULT_ROLE_ROUTES.get(normalized, "local")),
        )

    def egress_policy_for(self, corpus: str | None) -> CorpusEgressPolicy:
        if not corpus:
            return "local_only"
        return self.corpus_policies.get(corpus, "local_only")

    def to_dict(self) -> dict[str, Any]:
        return {
            "global_mode": self.global_mode,
            "role_routes": {
                role: route.to_dict() for role, route in sorted(self.role_routes.items())
            },
            "corpus_policies": dict(sorted(self.corpus_policies.items())),
            "budget": self.budget.to_dict(),
            "updated_at": self.updated_at,
        }


def egress_class(policy_name: str, payload_fields: set[str]) -> str:
    if policy_name == "local_only":
        return "none"
    if not payload_fields:
        return "metadata_only"
    return "_plus_".join(sorted(payload_fields))


def provider_is_known(provider: str | None) -> bool:
    normalized = (provider or "").strip().lower()
    return normalized in VALID_PROVIDERS


def provider_is_remote(provider: str | None) -> bool:
    normalized = (provider or "").strip().lower()
    return normalized in REMOTE_PROVIDERS


def role_allows_policy(role: str, policy_name: str) -> bool:
    return role in POLICY_ALLOWED_ROLES.get(policy_name, set())


def fields_allowed(policy_name: str, fields: set[str]) -> bool:
    allowed = POLICY_ALLOWED_FIELDS.get(policy_name, set())
    return fields.issubset(allowed)
