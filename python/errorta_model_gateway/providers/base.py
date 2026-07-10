"""Provider protocol used behind the Errorta model gateway."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass(frozen=True)
class ProviderRequest:
    model: str
    messages: list[dict[str, str]]
    max_tokens: int | None = None
    temperature: float | None = None
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ProviderResult:
    text: str
    provider: str
    model: str
    input_tokens: int = 0
    output_tokens: int = 0
    raw: dict[str, Any] | None = None


class Provider(Protocol):
    name: str

    def generate(self, request: ProviderRequest) -> ProviderResult: ...
