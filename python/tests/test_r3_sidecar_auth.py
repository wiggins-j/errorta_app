"""R3 — real auth on the mutation surface (per-sidecar bearer token).

Every mutation guard validates a per-sidecar bearer token IN ADDITION to its own
origin policy, via the shared origin-agnostic helper
``errorta_app.origin.validate_sidecar_token``. There are TWO guard families and
this coverage is NOT limited to coding/gateway:

  * the shared ``require_ui_or_cli_origin`` (``coding.py`` / ``gateway.py``;
    accepts ``tauri-ui`` OR ``cli``), and
  * the per-route ``_require_tauri_origin`` guards in ``settings.py`` /
    ``council.py`` / ``alpha.py`` / ``aiar_connection.py`` / ``auth.py``
    (``tauri-ui`` ONLY — stricter; R3 adds the token check WITHOUT loosening the
    origin policy).

The token decision runs in one of two modes: GRACE (default — a missing bearer
is tolerated for old-CLI compat) and ENFORCE
(``ERRORTA_SIDECAR_TOKEN_ENFORCE`` truthy — a missing OR invalid bearer is
rejected). These tests pin the truth table at the guard-function level and
end-to-end through a real mutating route in EACH guarded file, confirm the
desktop ``tauri-ui`` (origin-only, grace) path still passes, and confirm the
tauri-ui-only routes still reject ``cli`` (origin policy preserved).
"""
from __future__ import annotations

import inspect

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from errorta_app import origin
from errorta_app.server import app

_TOKEN = "s3cr3t-token-value"
# A real mutating route: DELETE clears a CLI-binary override; it calls the shared
# guard at the top of the handler, so a 403 there proves the chokepoint fired.
_MUTATING_ROUTE = "/provider-keys/claude_cli/cli-binary"


class _Req:
    """Minimal stand-in for a Starlette request (headers only)."""

    def __init__(self, headers: dict[str, str] | None = None) -> None:
        self.headers = headers or {}


@pytest.fixture(autouse=True)
def _isolate_home(tmp_path, monkeypatch):
    monkeypatch.setenv("ERRORTA_HOME", str(tmp_path))
    yield


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


def _detail(fn, req) -> str | None:
    """Return the 403 detail if ``fn`` rejects ``req``, else ``None`` (allowed)."""
    try:
        fn(req)
        return None
    except HTTPException as exc:
        assert exc.status_code == 403
        return exc.detail


# --------------------------------------------------------------------------- #
# Guard-function truth table (unit).
# --------------------------------------------------------------------------- #

def test_no_configured_token_allows_trusted_origin_only(monkeypatch) -> None:
    """A sidecar with NO token (desktop-spawned / pre-R3) runs origin-only."""
    monkeypatch.delenv(origin.SIDECAR_TOKEN_ENV, raising=False)
    assert _detail(origin.require_ui_or_cli_origin, _Req({"x-errorta-origin": "cli"})) is None
    assert (
        _detail(origin.require_ui_or_cli_origin, _Req({"x-errorta-origin": "tauri-ui"}))
        is None
    )


def test_valid_token_and_trusted_origin_allowed(monkeypatch) -> None:
    monkeypatch.setenv(origin.SIDECAR_TOKEN_ENV, _TOKEN)
    req = _Req({"x-errorta-origin": "cli", "authorization": f"Bearer {_TOKEN}"})
    assert _detail(origin.require_ui_or_cli_origin, req) is None


def test_invalid_token_rejected_even_with_trusted_origin(monkeypatch) -> None:
    monkeypatch.setenv(origin.SIDECAR_TOKEN_ENV, _TOKEN)
    req = _Req({"x-errorta-origin": "cli", "authorization": "Bearer wrong-token"})
    assert _detail(origin.require_ui_or_cli_origin, req) == "token_invalid"


def test_grace_trusted_origin_without_token_allowed(monkeypatch) -> None:
    """Old CLI compat: token configured, but the request presents NO bearer —
    a trusted origin alone is accepted during the alpha grace window."""
    monkeypatch.setenv(origin.SIDECAR_TOKEN_ENV, _TOKEN)
    req = _Req({"x-errorta-origin": "cli"})
    assert _detail(origin.require_ui_or_cli_origin, req) is None


def test_desktop_tauri_ui_origin_only_still_passes(monkeypatch) -> None:
    """The desktop webview sends ``tauri-ui`` and no token — must keep working
    even when the sidecar has a token configured (grace)."""
    monkeypatch.setenv(origin.SIDECAR_TOKEN_ENV, _TOKEN)
    req = _Req({"x-errorta-origin": "tauri-ui"})
    assert _detail(origin.require_ui_or_cli_origin, req) is None


@pytest.mark.parametrize("origin_value", ["evil", "browser", "", None])
def test_untrusted_origin_rejected(monkeypatch, origin_value) -> None:
    """Untrusted origin → 403 ``origin_not_authorized`` (unchanged), regardless
    of whether a (even valid) token is presented."""
    monkeypatch.setenv(origin.SIDECAR_TOKEN_ENV, _TOKEN)
    headers = {"authorization": f"Bearer {_TOKEN}"}
    if origin_value is not None:
        headers["x-errorta-origin"] = origin_value
    assert (
        _detail(origin.require_ui_or_cli_origin, _Req(headers)) == "origin_not_authorized"
    )


def test_bearer_parsing_is_case_insensitive_scheme() -> None:
    assert origin.bearer_token(_Req({"authorization": f"bearer {_TOKEN}"})) == _TOKEN
    assert origin.bearer_token(_Req({"authorization": f"Bearer {_TOKEN}"})) == _TOKEN
    assert origin.bearer_token(_Req({"authorization": _TOKEN})) is None
    assert origin.bearer_token(_Req({})) is None


def test_guard_uses_constant_time_compare() -> None:
    """Security: the token comparison must be constant-time (hmac.compare_digest),
    never a plain ``==`` that leaks length/prefix via timing. The compare lives in
    the shared ``validate_sidecar_token`` helper now."""
    src = inspect.getsource(origin.validate_sidecar_token)
    assert "hmac.compare_digest" in src
    assert "compare_digest" in inspect.getsource(origin)


def test_non_ascii_bearer_fails_closed_not_500(monkeypatch) -> None:
    """A malformed / non-ASCII bearer must return 403 ``token_invalid`` (fail
    closed), never raise TypeError from ``hmac.compare_digest`` → 500."""
    monkeypatch.setenv(origin.SIDECAR_TOKEN_ENV, _TOKEN)
    req = _Req({"x-errorta-origin": "cli", "authorization": "Bearer ünïcödé-Ω"})
    assert _detail(origin.require_ui_or_cli_origin, req) == "token_invalid"


# --------------------------------------------------------------------------- #
# Enforce mode (ERRORTA_SIDECAR_TOKEN_ENFORCE) — the hard gate.
# --------------------------------------------------------------------------- #

def test_enforce_no_bearer_rejected(monkeypatch) -> None:
    """ENFORCE + token configured + NO bearer → 403 (trusted origin alone is
    NOT sufficient — the exact attack R3 targets)."""
    monkeypatch.setenv(origin.SIDECAR_TOKEN_ENV, _TOKEN)
    monkeypatch.setenv(origin.SIDECAR_TOKEN_ENFORCE_ENV, "1")
    assert (
        _detail(origin.require_ui_or_cli_origin, _Req({"x-errorta-origin": "cli"}))
        == "token_required"
    )


def test_enforce_invalid_bearer_rejected(monkeypatch) -> None:
    monkeypatch.setenv(origin.SIDECAR_TOKEN_ENV, _TOKEN)
    monkeypatch.setenv(origin.SIDECAR_TOKEN_ENFORCE_ENV, "true")
    req = _Req({"x-errorta-origin": "cli", "authorization": "Bearer wrong"})
    assert _detail(origin.require_ui_or_cli_origin, req) == "token_invalid"


def test_enforce_valid_bearer_allowed(monkeypatch) -> None:
    monkeypatch.setenv(origin.SIDECAR_TOKEN_ENV, _TOKEN)
    monkeypatch.setenv(origin.SIDECAR_TOKEN_ENFORCE_ENV, "yes")
    req = _Req({"x-errorta-origin": "cli", "authorization": f"Bearer {_TOKEN}"})
    assert _detail(origin.require_ui_or_cli_origin, req) is None


def test_enforce_no_token_configured_is_origin_only(monkeypatch) -> None:
    """ENFORCE ON but sidecar has NO token (desktop-spawned / pre-R3): nothing to
    enforce → origin-only still allows. Enforce can't invent a token the sidecar
    never minted."""
    monkeypatch.delenv(origin.SIDECAR_TOKEN_ENV, raising=False)
    monkeypatch.setenv(origin.SIDECAR_TOKEN_ENFORCE_ENV, "1")
    assert (
        _detail(origin.require_ui_or_cli_origin, _Req({"x-errorta-origin": "tauri-ui"}))
        is None
    )


@pytest.mark.parametrize("raw", ["0", "false", "no", "off", "", "  "])
def test_enforce_falsey_values_stay_in_grace(monkeypatch, raw) -> None:
    """Falsey / blank ENFORCE values keep GRACE (no bearer → allow)."""
    monkeypatch.setenv(origin.SIDECAR_TOKEN_ENV, _TOKEN)
    monkeypatch.setenv(origin.SIDECAR_TOKEN_ENFORCE_ENV, raw)
    assert not origin.token_enforced()
    assert (
        _detail(origin.require_ui_or_cli_origin, _Req({"x-errorta-origin": "cli"}))
        is None
    )


# --------------------------------------------------------------------------- #
# End-to-end through a real mutating route (chokepoint wiring) + a read GET.
# --------------------------------------------------------------------------- #

def test_route_allows_valid_token(client, monkeypatch) -> None:
    monkeypatch.setenv(origin.SIDECAR_TOKEN_ENV, _TOKEN)
    resp = client.request(
        "DELETE",
        _MUTATING_ROUTE,
        headers={"x-errorta-origin": "cli", "authorization": f"Bearer {_TOKEN}"},
    )
    # The guard passed → the handler ran (200 payload for a known provider).
    assert resp.status_code == 200


def test_route_rejects_invalid_token(client, monkeypatch) -> None:
    monkeypatch.setenv(origin.SIDECAR_TOKEN_ENV, _TOKEN)
    resp = client.request(
        "DELETE",
        _MUTATING_ROUTE,
        headers={"x-errorta-origin": "cli", "authorization": "Bearer nope"},
    )
    assert resp.status_code == 403
    assert resp.json()["detail"] == "token_invalid"


def test_route_grace_allows_trusted_origin_without_token(client, monkeypatch) -> None:
    monkeypatch.setenv(origin.SIDECAR_TOKEN_ENV, _TOKEN)
    resp = client.request(
        "DELETE", _MUTATING_ROUTE, headers={"x-errorta-origin": "cli"}
    )
    assert resp.status_code == 200


def test_route_rejects_untrusted_origin(client, monkeypatch) -> None:
    monkeypatch.setenv(origin.SIDECAR_TOKEN_ENV, _TOKEN)
    resp = client.request(
        "DELETE",
        _MUTATING_ROUTE,
        headers={"x-errorta-origin": "evil", "authorization": f"Bearer {_TOKEN}"},
    )
    assert resp.status_code == 403
    assert resp.json()["detail"] == "origin_not_authorized"


def test_read_only_get_route_is_open(client, monkeypatch) -> None:
    """A read-only GET (``/healthz``) never calls the guard → no token required,
    no origin required. It stays open."""
    monkeypatch.setenv(origin.SIDECAR_TOKEN_ENV, _TOKEN)
    resp = client.get("/healthz")  # no origin, no token
    assert resp.status_code == 200


# --------------------------------------------------------------------------- #
# Feature advertisement.
# --------------------------------------------------------------------------- #

def test_healthz_advertises_sidecar_token_when_configured(client, monkeypatch) -> None:
    monkeypatch.setenv(origin.SIDECAR_TOKEN_ENV, _TOKEN)
    feats = client.get("/healthz").json()["features"]
    assert feats["sidecar_token"] is True


def test_healthz_reports_no_sidecar_token_without_one(client, monkeypatch) -> None:
    monkeypatch.delenv(origin.SIDECAR_TOKEN_ENV, raising=False)
    feats = client.get("/healthz").json()["features"]
    assert feats["sidecar_token"] is False


# --------------------------------------------------------------------------- #
# ENTIRE mutation surface — one mutating route per guarded file (the real fix).
#
# Before R3-fix these files each had a LOCAL ``_require_tauri_origin`` doing a
# bare origin check with NO token validation. Each of these routes must now
# enforce the token under enforce mode while KEEPING its tauri-ui-only origin
# policy (it must still reject ``cli``, which the shared guard would accept).
# --------------------------------------------------------------------------- #

# (method, path, json-body) — a real mutating route in each tauri-ui-only file.
_TAURI_ONLY_ROUTES = [
    ("PUT", "/settings/tools", {"searxng_url": None}),
    ("PUT", "/council/model-catalog", {"overrides": {}}),
    ("PUT", "/alpha/telemetry", {"extras_enabled": True}),
    ("PUT", "/aiar/model", {}),
    ("DELETE", "/api/auth/tokens/nonexistent-id", None),
]


def _mutate(client, method, path, body, *, origin_value, bearer=None):
    headers = {"x-errorta-origin": origin_value}
    if bearer is not None:
        headers["authorization"] = f"Bearer {bearer}"
    return client.request(method, path, headers=headers, json=body)


@pytest.mark.parametrize("method,path,body", _TAURI_ONLY_ROUTES)
def test_tauri_only_route_enforce_rejects_missing_bearer(
    client, monkeypatch, method, path, body
) -> None:
    """ENFORCE + token configured + tauri-ui origin + NO bearer → 403 on every
    tauri-ui-only mutating route (token auth now covers them)."""
    monkeypatch.setenv(origin.SIDECAR_TOKEN_ENV, _TOKEN)
    monkeypatch.setenv(origin.SIDECAR_TOKEN_ENFORCE_ENV, "1")
    resp = _mutate(client, method, path, body, origin_value="tauri-ui")
    assert resp.status_code == 403
    assert resp.json()["detail"] == "token_required"


@pytest.mark.parametrize("method,path,body", _TAURI_ONLY_ROUTES)
def test_tauri_only_route_enforce_rejects_invalid_bearer(
    client, monkeypatch, method, path, body
) -> None:
    monkeypatch.setenv(origin.SIDECAR_TOKEN_ENV, _TOKEN)
    monkeypatch.setenv(origin.SIDECAR_TOKEN_ENFORCE_ENV, "1")
    resp = _mutate(client, method, path, body, origin_value="tauri-ui", bearer="wrong")
    assert resp.status_code == 403
    assert resp.json()["detail"] == "token_invalid"


@pytest.mark.parametrize("method,path,body", _TAURI_ONLY_ROUTES)
def test_tauri_only_route_valid_bearer_passes_the_guard(
    client, monkeypatch, method, path, body
) -> None:
    """A valid bearer clears the guard: the response is whatever the handler
    returns (200/404/501/...), never a guard 403."""
    monkeypatch.setenv(origin.SIDECAR_TOKEN_ENV, _TOKEN)
    monkeypatch.setenv(origin.SIDECAR_TOKEN_ENFORCE_ENV, "1")
    resp = _mutate(client, method, path, body, origin_value="tauri-ui", bearer=_TOKEN)
    assert resp.status_code != 403


@pytest.mark.parametrize("method,path,body", _TAURI_ONLY_ROUTES)
def test_tauri_only_route_still_rejects_cli_origin(
    client, monkeypatch, method, path, body
) -> None:
    """SECURITY REGRESSION GUARD: these routes are tauri-ui ONLY. Even with a
    VALID bearer, a ``cli`` origin must still be rejected — R3 must NOT have
    loosened them to accept ``cli`` (which the shared coding/gateway guard does)."""
    monkeypatch.setenv(origin.SIDECAR_TOKEN_ENV, _TOKEN)
    resp = _mutate(client, method, path, body, origin_value="cli", bearer=_TOKEN)
    assert resp.status_code == 403
    # The origin check fires before the token check → an origin-policy detail,
    # never a token_* detail.
    assert resp.json()["detail"] in {"tauri origin required", "origin_not_authorized"}
