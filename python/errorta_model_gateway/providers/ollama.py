"""Local Ollama provider adapter for the gateway skeleton."""
from __future__ import annotations

import os
from ipaddress import ip_address
from typing import Any
from urllib.parse import urlparse

import httpx

from .base import ProviderRequest, ProviderResult


class OllamaProvider:
    name = "ollama"

    def __init__(self, *, host: str | None = None) -> None:
        self.host = (host or os.environ.get("OLLAMA_HOST") or "http://127.0.0.1:11434").rstrip(
            "/"
        )
        if _local_only_env_forced() and not is_loopback_ollama_host(self.host):
            raise RuntimeError("local-only mode requires OLLAMA_HOST to be loopback")

    def generate(self, request: ProviderRequest) -> ProviderResult:
        payload: dict[str, Any] = {
            "model": request.model,
            "messages": request.messages,
            "stream": False,
        }
        options: dict[str, Any] = {}
        if request.temperature is not None:
            options["temperature"] = request.temperature
        if request.max_tokens is not None:
            options["num_predict"] = request.max_tokens
        if options:
            payload["options"] = options

        with httpx.Client(timeout=httpx.Timeout(60.0)) as client:
            response = client.post(f"{self.host}/api/chat", json=payload)
        response.raise_for_status()
        data = response.json()
        message = data.get("message") if isinstance(data, dict) else None
        text = message.get("content", "") if isinstance(message, dict) else ""
        return ProviderResult(
            text=text,
            provider=self.name,
            model=request.model,
            input_tokens=int(data.get("prompt_eval_count") or 0),
            output_tokens=int(data.get("eval_count") or 0),
            raw=data if isinstance(data, dict) else None,
        )


def _local_only_env_forced() -> bool:
    return (
        os.environ.get("AIAR_LOCAL_ONLY") == "1"
        or os.environ.get("ERRORTA_MODEL_GATEWAY_LOCAL_ONLY") == "1"
    )


def is_loopback_ollama_host(host: str | None) -> bool:
    """Return True only for explicit local Ollama endpoints.

    Missing host values are treated as the default local Ollama daemon. Hostnames
    other than localhost/IP loopback are considered remote because they can
    egress over LAN/cloud even when the provider is named "local".
    """
    if not host:
        return True
    raw = host.strip()
    if not raw:
        return True
    parsed = urlparse(raw if "://" in raw else f"http://{raw}")
    hostname = (parsed.hostname or "").strip().lower()
    if hostname == "localhost":
        return True
    if not hostname:
        return False
    try:
        return ip_address(hostname).is_loopback
    except ValueError:
        return False
