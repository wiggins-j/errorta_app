"""First-run onboarding decision + guidance text (F147 S8, §7 / §11).

The whole point of factoring the decision into pure functions is that it tests
without a live terminal or a real sidecar. The provider probes are supplied as
plain dicts / a mock client; the ambient flags are passed explicitly.
"""
from __future__ import annotations

from errorta_cli import onboarding

from .conftest import RouteClient

# ---- payload fixtures ------------------------------------------------------ #

_EMPTY = {"providers": []}
_FREE_ONLY = {"providers": [
    {"provider_class": "local", "configured": True, "connected": None},
    {"provider_class": "fake", "configured": True},
]}
_ANTHROPIC = {"providers": [
    {"provider_class": "local", "configured": True},
    {"provider_class": "anthropic", "configured": True},
]}
_CLI_CONNECTED = {"providers": [
    {"provider_class": "claude_cli", "configured": False, "connected": True},
]}


# --------------------------------------------------------------------------- #
# has_real_provider.
# --------------------------------------------------------------------------- #

def test_no_providers_is_unconfigured() -> None:
    assert onboarding.has_real_provider(_EMPTY) is False


def test_free_providers_do_not_count_as_configured() -> None:
    # local / fake are ALWAYS configured=True — they must not mask a fresh store.
    assert onboarding.has_real_provider(_FREE_ONLY) is False


def test_configured_api_provider_counts() -> None:
    assert onboarding.has_real_provider(_ANTHROPIC) is True


def test_connected_cli_counts_even_when_not_configured() -> None:
    assert onboarding.has_real_provider(_CLI_CONNECTED) is True


def test_keys_api_entry_counts() -> None:
    keys = {"anthropic": {"configured": True, "key_preview": "…abcd"}}
    assert onboarding.has_real_provider(_EMPTY, keys) is True


def test_keys_custom_entry_counts() -> None:
    keys = {"custom": [{"alias": "lmstudio", "base_url": "http://example-host"}]}
    assert onboarding.has_real_provider(_EMPTY, keys) is True


def test_empty_keys_do_not_count() -> None:
    assert onboarding.has_real_provider(_EMPTY, {"custom": [], "anthropic": {}}) is False


def test_garbage_payload_is_unconfigured() -> None:
    assert onboarding.has_real_provider(None) is False
    assert onboarding.has_real_provider("nonsense", 123) is False


# --------------------------------------------------------------------------- #
# should_show — the pure gate.
# --------------------------------------------------------------------------- #

def _show(providers=_EMPTY, keys=None, **kw) -> bool:
    base = dict(interactive=True, json_mode=False, opted=False, command=None)
    base.update(kw)
    return onboarding.should_show(providers, keys, **base)


def test_show_when_unconfigured_and_interactive() -> None:
    assert _show() is True


def test_hidden_in_json_mode() -> None:
    assert _show(json_mode=True) is False


def test_hidden_when_non_interactive() -> None:
    assert _show(interactive=False) is False


def test_hidden_when_opted_out() -> None:
    assert _show(opted=True) is False


def test_hidden_for_connect_command() -> None:
    assert _show(command="connect") is False


def test_hidden_when_already_configured() -> None:
    assert _show(providers=_ANTHROPIC) is False


# --------------------------------------------------------------------------- #
# opted_out — flag + env.
# --------------------------------------------------------------------------- #

def test_opted_out_flag_wins() -> None:
    assert onboarding.opted_out(True, env={}) is True


def test_opted_out_env_truthy() -> None:
    for val in ("1", "true", "yes", "on", "TRUE"):
        assert onboarding.opted_out(False, env={onboarding.ONBOARDING_ENV: val}) is True


def test_opted_out_env_falsey() -> None:
    for val in ("", "0", "false", "no", "off"):
        assert onboarding.opted_out(False, env={onboarding.ONBOARDING_ENV: val}) is False


# --------------------------------------------------------------------------- #
# welcome_text — must guide to connect / wizard / new.
# --------------------------------------------------------------------------- #

def test_welcome_text_mentions_connect_and_setup_paths() -> None:
    text = onboarding.welcome_text()
    assert "connect" in text
    assert "wizard" in text
    assert "new" in text
    # The opt-out is advertised in the message itself.
    assert "--no-onboarding" in text


# --------------------------------------------------------------------------- #
# evaluate — probes a (mock) client, returns text-or-None. Best-effort.
# --------------------------------------------------------------------------- #

def _eval(client, **kw) -> str | None:
    base = dict(interactive=True, json_mode=False, opted=False, command=None)
    base.update(kw)
    return onboarding.evaluate(client, **base)


def test_evaluate_shows_when_unconfigured() -> None:
    client = RouteClient({"/gateway/providers": _EMPTY, "/provider-keys": {}})
    text = _eval(client)
    assert text is not None and "connect" in text
    # It probed both endpoints.
    assert ("GET", "/gateway/providers") in client.calls
    assert ("GET", "/provider-keys") in client.calls


def test_evaluate_silent_when_configured() -> None:
    client = RouteClient({"/gateway/providers": _ANTHROPIC, "/provider-keys": {}})
    assert _eval(client) is None


def test_evaluate_makes_no_probe_in_json_mode() -> None:
    client = RouteClient({"/gateway/providers": _EMPTY, "/provider-keys": {}})
    assert _eval(client, json_mode=True) is None
    assert client.calls == []  # invariant #3 — no network in --json


def test_evaluate_makes_no_probe_when_non_interactive() -> None:
    client = RouteClient({"/gateway/providers": _EMPTY, "/provider-keys": {}})
    assert _eval(client, interactive=False) is None
    assert client.calls == []


def test_evaluate_makes_no_probe_when_opted_out() -> None:
    client = RouteClient({"/gateway/providers": _EMPTY, "/provider-keys": {}})
    assert _eval(client, opted=True) is None
    assert client.calls == []


def test_evaluate_skips_connect_command() -> None:
    client = RouteClient({"/gateway/providers": _EMPTY, "/provider-keys": {}})
    assert _eval(client, command="connect") is None
    assert client.calls == []


def test_evaluate_swallows_probe_errors() -> None:
    class _Boom:
        def get_json(self, path, **kw):
            raise RuntimeError("sidecar exploded")

    # A probe failure must never propagate — onboarding is best-effort.
    assert _eval(_Boom()) is None
