"""F034-2 — provider_keys store: load/save/mask + 0600 mode + atomic write."""
from __future__ import annotations

import json
import os
import stat
import sys

import pytest

from errorta_app import provider_keys


@pytest.fixture(autouse=True)
def _isolate_home(tmp_path, monkeypatch):
    """Pin ERRORTA_HOME to a tmp dir so the on-disk file is hermetic."""
    monkeypatch.setenv("ERRORTA_HOME", str(tmp_path))
    yield


def test_load_creates_default_file_on_first_call(tmp_path) -> None:
    p = provider_keys.path()
    assert not p.exists()
    keys = provider_keys.load_all()
    assert p.exists(), "first load_all must materialize the file"
    assert keys.get("anthropic") == {}
    assert keys.get("openai") == {}
    assert keys.get("google") == {}
    assert keys.get("custom") == []


def test_round_trip_anthropic_key() -> None:
    provider_keys.upsert_fixed("anthropic", "sk-ant-test-abcd1234")
    reloaded = provider_keys.load_all()
    assert reloaded["anthropic"]["api_key"] == "sk-ant-test-abcd1234"


def test_clear_fixed_resets_to_empty_dict() -> None:
    provider_keys.upsert_fixed("openai", "sk-test-xyz")
    provider_keys.clear_fixed("openai")
    assert provider_keys.load_all()["openai"] == {}


def test_upsert_custom_round_trip() -> None:
    provider_keys.upsert_custom({
        "alias": "lmstudio",
        "base_url": "http://127.0.0.1:1234/v1",
        "api_key": "lm-studio-secret",
        "api_style": "openai_chat_completions",
        "auth_header": "Authorization",
        "auth_prefix": "Bearer ",
    })
    customs = provider_keys.load_all()["custom"]
    assert len(customs) == 1
    assert customs[0]["alias"] == "lmstudio"
    assert customs[0]["api_key"] == "lm-studio-secret"


def test_upsert_custom_replaces_existing_by_alias() -> None:
    provider_keys.upsert_custom({
        "alias": "lmstudio", "base_url": "http://old/v1",
        "api_key": "old-key", "api_style": "openai_chat_completions",
    })
    provider_keys.upsert_custom({
        "alias": "lmstudio", "base_url": "http://new/v1",
        "api_key": "new-key", "api_style": "openai_chat_completions",
    })
    customs = provider_keys.load_all()["custom"]
    assert len(customs) == 1
    assert customs[0]["base_url"] == "http://new/v1"
    assert customs[0]["api_key"] == "new-key"


def test_clear_custom_removes_by_alias() -> None:
    provider_keys.upsert_custom({
        "alias": "lmstudio", "base_url": "http://x/v1",
        "api_key": "k1", "api_style": "openai_chat_completions",
    })
    provider_keys.upsert_custom({
        "alias": "vllm", "base_url": "http://y/v1",
        "api_key": "k2", "api_style": "openai_chat_completions",
    })
    provider_keys.clear_custom("lmstudio")
    aliases = [c["alias"] for c in provider_keys.load_all()["custom"]]
    assert aliases == ["vllm"]


def test_upsert_fixed_rejects_unknown_provider() -> None:
    with pytest.raises(ValueError, match="unknown fixed provider"):
        provider_keys.upsert_fixed("totally-unknown", "key")


def test_upsert_fixed_rejects_empty_key() -> None:
    with pytest.raises(ValueError, match="non-empty"):
        provider_keys.upsert_fixed("anthropic", "")


def test_upsert_custom_rejects_missing_alias() -> None:
    with pytest.raises(ValueError, match="alias"):
        provider_keys.upsert_custom({  # type: ignore[typeddict-item]
            "base_url": "http://x/v1", "api_key": "k",
            "api_style": "openai_chat_completions",
        })


def test_upsert_custom_rejects_unknown_api_style() -> None:
    with pytest.raises(ValueError, match="api_style"):
        provider_keys.upsert_custom({  # type: ignore[typeddict-item]
            "alias": "x", "base_url": "http://x/v1", "api_key": "k",
            "api_style": "not-real",
        })


def test_load_returns_defaults_on_unreadable_file() -> None:
    """A corrupt JSON file should not crash startup."""
    p = provider_keys.path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("{broken json", encoding="utf-8")
    keys = provider_keys.load_all()
    assert keys["anthropic"] == {}
    assert keys["custom"] == []


def test_load_coerces_malformed_entries() -> None:
    """Non-dict provider entries get replaced by empty dicts."""
    p = provider_keys.path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({
        "anthropic": "this should be a dict, not a string",
        "openai": {"api_key": "valid-key"},
        "google": None,
        "custom": "not a list",
    }), encoding="utf-8")
    keys = provider_keys.load_all()
    assert keys["anthropic"] == {}
    assert keys["openai"]["api_key"] == "valid-key"
    assert keys["google"] == {}
    assert keys["custom"] == []


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX mode bits")
def test_save_enforces_0600_mode() -> None:
    provider_keys.upsert_fixed("anthropic", "sk-test-123")
    mode = stat.S_IMODE(os.stat(provider_keys.path()).st_mode)
    assert mode == 0o600, f"expected 0o600, got {oct(mode)}"


def test_save_is_atomic_no_tmpfiles_left_behind() -> None:
    """After a save, no .tmp files should remain in the dir."""
    provider_keys.upsert_fixed("openai", "sk-test")
    parent = provider_keys.path().parent
    leftovers = list(parent.glob(".provider-keys-*.tmp"))
    assert leftovers == [], f"tmpfiles leaked: {leftovers}"


def test_get_fixed_key_returns_none_for_missing() -> None:
    assert provider_keys.get_fixed_key("anthropic") is None


def test_get_fixed_key_returns_raw_when_set() -> None:
    provider_keys.upsert_fixed("google", "raw-gemini-key")
    assert provider_keys.get_fixed_key("google") == "raw-gemini-key"


def test_get_custom_entry_round_trip() -> None:
    provider_keys.upsert_custom({
        "alias": "lmstudio", "base_url": "http://127.0.0.1:1234/v1",
        "api_key": "lm-secret", "api_style": "openai_chat_completions",
    })
    entry = provider_keys.get_custom_entry("lmstudio")
    assert entry is not None
    assert entry["api_key"] == "lm-secret"


# ----------------------------------------------------------------------
# Masking
# ----------------------------------------------------------------------


def test_mask_all_no_keys_present() -> None:
    out = provider_keys.mask_all()
    assert out["anthropic"]["configured"] is False
    assert out["anthropic"]["key_preview"] is None
    assert out["custom"] == []


def test_mask_all_anthropic_key_shows_last4() -> None:
    provider_keys.upsert_fixed("anthropic", "sk-ant-test-abcd1234")
    out = provider_keys.mask_all()
    assert out["anthropic"]["configured"] is True
    assert out["anthropic"]["key_preview"] == "…1234"


def test_mask_all_custom_shows_metadata_and_masked_key() -> None:
    provider_keys.upsert_custom({
        "alias": "lmstudio",
        "base_url": "http://127.0.0.1:1234/v1",
        "api_key": "lm-studio-supersecret",
        "api_style": "openai_chat_completions",
        "auth_header": "Authorization",
        "auth_prefix": "Bearer ",
    })
    out = provider_keys.mask_all()
    assert len(out["custom"]) == 1
    c = out["custom"][0]
    assert c["alias"] == "lmstudio"
    assert c["base_url"] == "http://127.0.0.1:1234/v1"
    assert c["api_style"] == "openai_chat_completions"
    assert c["configured"] is True
    assert c["key_preview"] == "…cret"
    # Raw key MUST NOT appear in masked output.
    assert "lm-studio-supersecret" not in json.dumps(out)


def test_mask_all_never_includes_raw_keys() -> None:
    """Marquee invariant — operator-facing mask never leaks raw keys."""
    provider_keys.upsert_fixed("anthropic", "sk-ant-DO-NOT-LEAK-12345")
    provider_keys.upsert_fixed("openai", "sk-DO-NOT-LEAK-OPENAI-67890")
    provider_keys.upsert_custom({
        "alias": "x", "base_url": "http://x/v1",
        "api_key": "DO-NOT-LEAK-CUSTOM-abcde",
        "api_style": "openai_chat_completions",
    })
    blob = json.dumps(provider_keys.mask_all())
    for forbidden in (
        "sk-ant-DO-NOT-LEAK-12345",
        "sk-DO-NOT-LEAK-OPENAI-67890",
        "DO-NOT-LEAK-CUSTOM-abcde",
    ):
        assert forbidden not in blob, f"raw key leaked: {forbidden}"
