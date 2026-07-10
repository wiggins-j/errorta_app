"""Provider adapters for the Errorta model gateway."""
from __future__ import annotations

from .base import Provider, ProviderRequest, ProviderResult
from .ollama import OllamaProvider

__all__ = ["OllamaProvider", "Provider", "ProviderRequest", "ProviderResult"]
