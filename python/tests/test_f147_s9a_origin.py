"""F147 S9a — first-class ``cli`` origin.

The coding + gateway mutation guards must accept ``x-errorta-origin: cli`` in
addition to ``tauri-ui`` (both loopback-trusted, distinguishable in audit) and
reject anything else.
"""
from __future__ import annotations

import pytest
from fastapi import HTTPException


class _Req:
    def __init__(self, origin: object) -> None:
        self.headers = {} if origin is None else {"x-errorta-origin": origin}


def _accepts(fn, origin: object) -> bool:
    try:
        fn(_Req(origin))
        return True
    except HTTPException as exc:
        assert exc.status_code == 403
        assert exc.detail == "origin_not_authorized"
        return False


@pytest.mark.parametrize("origin", ["tauri-ui", "cli", "CLI", "Tauri-UI"])
def test_shared_helper_accepts_trusted(origin: str) -> None:
    from errorta_app.origin import require_ui_or_cli_origin

    assert _accepts(require_ui_or_cli_origin, origin)


@pytest.mark.parametrize("origin", ["evil", "browser", "", None])
def test_shared_helper_rejects_others(origin: object) -> None:
    from errorta_app.origin import require_ui_or_cli_origin

    assert not _accepts(require_ui_or_cli_origin, origin)


def test_coding_guard_accepts_cli_and_tauri() -> None:
    from errorta_app.routes.coding import _require_tauri_origin

    assert _accepts(_require_tauri_origin, "cli")
    assert _accepts(_require_tauri_origin, "tauri-ui")
    assert not _accepts(_require_tauri_origin, "evil")


def test_gateway_guard_accepts_cli_and_tauri() -> None:
    from errorta_app.routes.gateway import _require_tauri_origin

    assert _accepts(_require_tauri_origin, "cli")
    assert _accepts(_require_tauri_origin, "tauri-ui")
    assert not _accepts(_require_tauri_origin, "evil")
