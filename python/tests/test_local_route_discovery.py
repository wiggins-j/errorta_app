"""Local routes are DISCOVERED from the Ollama host, not just a curated list.

The curated `_DEFAULT_ROUTES` were functioning as an ALLOWLIST: the CLI team
builder's `_resolve_value` validates a route against `/gateway/routes`, so a model
the operator had actually pulled — `qwen3.5:9b` on the reference box — was rejected
with "unknown route or provider" and could not be seated at all. Meanwhile
`LocalHandler.validate_route` accepts ANY `local.*` id, so the list was only ever
meant to be discovery.

Found by trying to build a real mixed team: hosted PM/reviewer/tester, three local
devs. The devs could not be added.
"""
from __future__ import annotations

import pytest

from errorta_model_gateway.providers import async_local as AL


class _Resp:
    def __init__(self, status_code: int, payload: object) -> None:
        self.status_code = status_code
        self._payload = payload

    def json(self) -> object:
        return self._payload


class _Client:
    def __init__(self, resp: object | Exception) -> None:
        self._resp = resp

    def __enter__(self) -> "_Client":
        return self

    def __exit__(self, *a: object) -> bool:
        return False

    def get(self, _url: str) -> object:
        if isinstance(self._resp, Exception):
            raise self._resp
        return self._resp


def _patch_host(monkeypatch: pytest.MonkeyPatch, resp: object | Exception) -> None:
    import httpx

    monkeypatch.setattr(httpx, "Client", lambda **_kw: _Client(resp))


def test_a_pulled_model_becomes_a_seatable_route(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """The regression: qwen3.5:9b is on the host but not in the curated list."""
    _patch_host(monkeypatch, _Resp(200, {"models": [
        {"name": "qwen3.5:9b"}, {"name": "deepseek-r1:8b"}]}))

    ids = {r.route_id for r in AL.LocalHandler().list_routes(configured=False)}
    assert "local.ollama.qwen3.5:9b" in ids, (
        "a model the operator has pulled must be seatable; this is the id the "
        "team builder rejected as 'unknown route or provider'")
    assert "local.ollama.deepseek-r1:8b" in ids


def test_curated_defaults_are_still_offered(
        monkeypatch: pytest.MonkeyPatch) -> None:
    # Discovery UNIONS with the defaults; it does not replace them. A curated
    # entry the host has not pulled yet still shows (availability is a separate
    # probe, and it is what reports `local_model_missing`).
    _patch_host(monkeypatch, _Resp(200, {"models": [{"name": "qwen3.5:9b"}]}))

    ids = {r.route_id for r in AL.LocalHandler().list_routes(configured=False)}
    assert "local.ollama.llama3.2:3b" in ids
    assert "fake.local.deterministic" in ids


def test_no_duplicate_when_the_host_serves_a_curated_model(
        monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_host(monkeypatch, _Resp(200, {"models": [{"name": "gemma2:9b"}]}))

    ids = [r.route_id for r in AL.LocalHandler().list_routes(configured=False)]
    assert ids.count("local.ollama.gemma2:9b") == 1


@pytest.mark.parametrize("resp", [
    RuntimeError("host down"),
    _Resp(500, {}),
    _Resp(200, {"models": None}),
    _Resp(200, "not a dict"),
])
def test_discovery_fails_open(monkeypatch: pytest.MonkeyPatch,
                              resp: object) -> None:
    """`errorta models` and the team builder must not break because Ollama is off.

    Every failure path degrades to exactly the curated list — the pre-change
    behaviour — rather than an empty list or an exception.
    """
    _patch_host(monkeypatch, resp)

    ids = {r.route_id for r in AL.LocalHandler().list_routes(configured=False)}
    assert ids == {r.route_id for r in AL._DEFAULT_ROUTES}


def test_blank_model_names_are_skipped(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_host(monkeypatch, _Resp(200, {"models": [
        {"name": ""}, {"name": "  "}, {}, {"name": "qwen3.5:9b"}]}))

    ids = {r.route_id for r in AL.LocalHandler().list_routes(configured=False)}
    assert "local.ollama.qwen3.5:9b" in ids
    assert not any(r.endswith("ollama.") for r in ids)


def test_validate_route_already_accepted_what_the_list_rejected() -> None:
    """Pins the inconsistency that caused this: the handler was permissive while
    the catalog was treated as a gate."""
    h = AL.LocalHandler()
    assert h.validate_route("local.ollama.qwen3.5:9b").ok
