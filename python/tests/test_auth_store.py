"""F009-01 S0 — service auth token store."""

from __future__ import annotations

import json
import os
import stat

from errorta_app.auth import store
from errorta_app.paths import auth_tokens_path, revoked_tokens_path


def test_token_store_persists_hash_only(tmp_errorta_home) -> None:
    raw = "ert_" + "a" * 32
    record = store.create_token(
        raw_token=raw,
        app_slug="demo",
        app_name="Demo",
        corpora=["welcome"],
        scopes=["prompt", "meta"],
    )

    text = auth_tokens_path().read_text(encoding="utf-8")
    assert raw not in text
    assert record["token_sha256"] == store.token_hash(raw)
    assert store.find_by_token(raw)["id"] == record["id"]


def test_token_store_tolerates_corrupt_json(tmp_errorta_home) -> None:
    auth_tokens_path().write_text("{not-json", encoding="utf-8")
    revoked_tokens_path().write_text("{not-json", encoding="utf-8")
    store.reset_state_for_tests()

    assert store.load_tokens() == []
    assert store.load_revoked_ids(force=True) == set()


def test_token_store_writes_mode_0600_on_posix(tmp_errorta_home) -> None:
    store.create_token(
        raw_token="ert_" + "b" * 32,
        app_slug="demo",
        app_name="Demo",
        corpora=["welcome"],
    )

    if os.name == "posix":
        mode = stat.S_IMODE(auth_tokens_path().stat().st_mode)
        assert mode == 0o600


def test_revoke_persists_blocklist_and_hides_public_token(tmp_errorta_home) -> None:
    raw = "ert_" + "c" * 32
    record = store.create_token(
        raw_token=raw,
        app_slug="demo",
        app_name="Demo",
        corpora=["welcome"],
    )

    public = store.list_public_tokens()
    assert public == [
        {
            "id": record["id"],
            "app_slug": "demo",
            "app_name": "Demo",
            "corpora": ["welcome"],
            "scopes": ["prompt", "meta"],
            "issued_at": record["issued_at"],
            "last_used_at": None,
        }
    ]
    assert "token_sha256" not in public[0]

    store.revoke_token(record["id"])
    store.reset_state_for_tests()
    assert record["id"] in store.load_revoked_ids()
    assert store.find_by_token(raw) is None
    payload = json.loads(revoked_tokens_path().read_text(encoding="utf-8"))
    assert payload["revoked"] == [record["id"]]
