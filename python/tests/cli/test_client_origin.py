"""The origin header is on every request; status → typed-exception mapping.

Golden invariant #2 (F147 plan §4): every request carries the trusted origin
header ``x-errorta-origin`` — the sole guard on coding/gateway mutations. S9b
sends ``cli`` (audit-distinguishable from the GUI's ``tauri-ui``; both trusted).
"""
from __future__ import annotations

import httpx
import pytest

from errorta_cli import errors
from errorta_cli.client import ORIGIN_HEADER, ORIGIN_VALUE, SidecarClient


def _client(handler) -> SidecarClient:
    return SidecarClient("http://127.0.0.1:9", transport=httpx.MockTransport(handler))


def test_get_sends_origin_header() -> None:
    seen: dict[str, str | None] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["origin"] = request.headers.get(ORIGIN_HEADER)
        return httpx.Response(200, json={"ok": True})

    with _client(handler) as client:
        client.get_json("/healthz")
    assert seen["origin"] == ORIGIN_VALUE


def test_every_verb_sends_origin_header() -> None:
    origins: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        origins.append(request.headers.get(ORIGIN_HEADER))
        return httpx.Response(200, json={})

    with _client(handler) as client:
        client.get_json("/x")
        client.post_json("/x", json={"a": 1})
        client.put_json("/x", json={"a": 1})
        client.delete_json("/x")
    assert origins == [ORIGIN_VALUE] * 4


@pytest.mark.parametrize(
    ("status", "body", "expected"),
    [
        (403, "origin_not_authorized", errors.OriginDenied),
        (403, {"error": "alpha_locked", "state": "locked"}, errors.AlphaLocked),
        (409, "a run is already in progress", errors.LockBusy),
        (409, {"code": "residency_unsupported_path", "message": "no"}, errors.ResidencyRefused),
        (404, "project not found", errors.NotFound),
        (501, {"detail": "cloud"}, errors.ResidencyRefused),
        (503, "tunnel down", errors.ResidencyRefused),
        (500, "boom", errors.CliError),
    ],
)
def test_status_maps_to_typed_exception(status, body, expected) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if isinstance(body, str):
            return httpx.Response(status, json={"detail": body})
        return httpx.Response(status, json={"detail": body})

    with _client(handler) as client:
        with pytest.raises(expected):
            client.get_json("/coding/projects/p/run")


def test_alpha_locked_and_origin_denied_are_distinct() -> None:
    # Both are 403 but must map to different exit-code classes.
    assert errors.AlphaLocked.exit_code != errors.OriginDenied.exit_code


def test_connection_failure_maps_to_sidecar_unreachable() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    with _client(handler) as client:
        with pytest.raises(errors.SidecarUnreachable):
            client.get_json("/healthz")


def test_empty_2xx_body_returns_none() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(204)

    with _client(handler) as client:
        assert client.post_json("/coding/projects/p/run/cancel") is None
