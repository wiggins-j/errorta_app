"""Task 3 tests: Slack channel provisioning (`errorta_slack.provisioning`).

Every test drives a fake ``web_client`` — no network, no real Slack
tokens (this is a PUBLIC repo; placeholder ids only).
"""
from __future__ import annotations

from typing import Any

import pytest

from errorta_slack import provisioning


# --- derive_channel_name -------------------------------------------------


def test_derive_channel_name_slugifies_title() -> None:
    assert provisioning.derive_channel_name("Homeschool Game!") == "homeschool-game"


def test_derive_channel_name_falls_back_to_proj_when_empty() -> None:
    assert provisioning.derive_channel_name("  ") == "proj"


def test_derive_channel_name_falls_back_to_proj_for_all_punctuation() -> None:
    assert provisioning.derive_channel_name("!!!") == "proj"


def test_derive_channel_name_truncates_to_80_chars() -> None:
    name = provisioning.derive_channel_name("a" * 100)
    assert len(name) == 80
    assert name == "a" * 80


def test_derive_channel_name_collapses_repeated_dashes() -> None:
    assert provisioning.derive_channel_name("a---b") == "a-b"


def test_derive_channel_name_strips_leading_and_trailing_dashes() -> None:
    assert provisioning.derive_channel_name("-hello-") == "hello"
    assert provisioning.derive_channel_name("***hello***") == "hello"


def test_derive_channel_name_lowercases() -> None:
    assert provisioning.derive_channel_name("MyProject") == "myproject"


def test_derive_channel_name_preserves_underscores_and_digits() -> None:
    assert provisioning.derive_channel_name("proj_42") == "proj_42"


# --- fakes -----------------------------------------------------------------


class _SlackApiErrorLike(Exception):
    """Duck-typed stand-in for ``slack_sdk.errors.SlackApiError`` — has a
    ``.response`` dict with an ``"error"`` key, nothing more."""

    def __init__(self, error: str) -> None:
        self.response = {"error": error}
        super().__init__(error)


class _FakeWebClient:
    """Happy-path fake: always creates successfully, records invite/topic
    calls."""

    def __init__(self, *, channel_id: str = "C9", channel_name: str = "homeschool-game") -> None:
        self._channel_id = channel_id
        self._channel_name = channel_name
        self.create_calls: list[dict[str, Any]] = []
        self.invite_calls: list[dict[str, Any]] = []
        self.topic_calls: list[dict[str, Any]] = []

    def conversations_create(self, *, name: str, is_private: bool) -> dict[str, Any]:
        self.create_calls.append({"name": name, "is_private": is_private})
        return {"channel": {"id": self._channel_id, "name": name or self._channel_name}}

    def conversations_invite(self, *, channel: str, users: list[str]) -> dict[str, Any]:
        self.invite_calls.append({"channel": channel, "users": users})
        return {"ok": True}

    def conversations_setTopic(self, *, channel: str, topic: str) -> dict[str, Any]:
        self.topic_calls.append({"channel": channel, "topic": topic})
        return {"ok": True}


class _NameTakenThenOkWebClient(_FakeWebClient):
    """Raises ``name_taken`` on the first create call, then succeeds."""

    def conversations_create(self, *, name: str, is_private: bool) -> dict[str, Any]:
        self.create_calls.append({"name": name, "is_private": is_private})
        if len(self.create_calls) == 1:
            raise _SlackApiErrorLike("name_taken")
        return {"channel": {"id": self._channel_id, "name": name}}


class _AlwaysNameTakenWebClient(_FakeWebClient):
    """Every create call reports ``name_taken`` — exercises the bounded
    retry's exhaustion path."""

    def conversations_create(self, *, name: str, is_private: bool) -> dict[str, Any]:
        self.create_calls.append({"name": name, "is_private": is_private})
        raise _SlackApiErrorLike("name_taken")


class _CreateFailsWebClient(_FakeWebClient):
    """Create fails for a non-``name_taken`` reason (e.g. missing scope)."""

    def __init__(self, error: str) -> None:
        super().__init__()
        self._error = error

    def conversations_create(self, *, name: str, is_private: bool) -> dict[str, Any]:
        self.create_calls.append({"name": name, "is_private": is_private})
        raise _SlackApiErrorLike(self._error)


class _CreateRaisesPlainRuntimeErrorWebClient(_FakeWebClient):
    """Create raises a plain exception with no ``.response`` at all —
    not a SlackApiError-shaped error, e.g. a network/library bug."""

    def conversations_create(self, *, name: str, is_private: bool) -> dict[str, Any]:
        self.create_calls.append({"name": name, "is_private": is_private})
        raise RuntimeError("boom")


class _InviteFailsWebClient(_FakeWebClient):
    def __init__(self, invite_error: str) -> None:
        super().__init__()
        self._invite_error = invite_error

    def conversations_invite(self, *, channel: str, users: list[str]) -> dict[str, Any]:
        self.invite_calls.append({"channel": channel, "users": users})
        raise _SlackApiErrorLike(self._invite_error)


class _TopicFailsWebClient(_FakeWebClient):
    def conversations_setTopic(self, *, channel: str, topic: str) -> dict[str, Any]:
        self.topic_calls.append({"channel": channel, "topic": topic})
        raise _SlackApiErrorLike("some_topic_error")


# --- create_project_channel: happy path -------------------------------------


def test_create_project_channel_happy_path_returns_channel_id_and_name() -> None:
    client = _FakeWebClient()
    result = provisioning.create_project_channel(
        client, title="Homeschool Game!", invite_user_ids=["U1", "U2"]
    )
    assert result == {"channel_id": "C9", "name": "homeschool-game"}


def test_create_project_channel_calls_conversations_create_with_derived_name() -> None:
    client = _FakeWebClient()
    provisioning.create_project_channel(client, title="Homeschool Game!", invite_user_ids=[])
    assert client.create_calls == [{"name": "homeschool-game", "is_private": False}]


def test_create_project_channel_invites_given_user_ids() -> None:
    client = _FakeWebClient()
    provisioning.create_project_channel(
        client, title="Homeschool Game!", invite_user_ids=["U1", "U2"]
    )
    assert client.invite_calls == [{"channel": "C9", "users": ["U1", "U2"]}]


def test_create_project_channel_skips_invite_call_when_no_user_ids() -> None:
    """An empty invite list must not call conversations_invite at all —
    on real Slack an empty ``users`` list can itself error, and any such
    error not in the swallow-whitelist would otherwise raise even though
    the channel was already created."""
    client = _FakeWebClient()
    result = provisioning.create_project_channel(
        client, title="Homeschool Game!", invite_user_ids=[]
    )
    assert result == {"channel_id": "C9", "name": "homeschool-game"}
    assert client.invite_calls == []


def test_create_project_channel_sets_topic_when_purpose_given() -> None:
    client = _FakeWebClient()
    provisioning.create_project_channel(
        client, title="Homeschool Game!", invite_user_ids=[], purpose="Build a math game"
    )
    assert client.topic_calls == [{"channel": "C9", "topic": "Build a math game"}]


def test_create_project_channel_truncates_topic_to_250_chars() -> None:
    client = _FakeWebClient()
    long_purpose = "x" * 300
    provisioning.create_project_channel(
        client, title="Homeschool Game!", invite_user_ids=[], purpose=long_purpose
    )
    assert client.topic_calls[0]["topic"] == "x" * 250


def test_create_project_channel_skips_topic_when_purpose_empty() -> None:
    client = _FakeWebClient()
    provisioning.create_project_channel(client, title="Homeschool Game!", invite_user_ids=[])
    assert client.topic_calls == []


# --- create_project_channel: name_taken retry -------------------------------


def test_create_project_channel_retries_with_suffix_on_name_taken() -> None:
    client = _NameTakenThenOkWebClient()
    result = provisioning.create_project_channel(
        client, title="Homeschool Game!", invite_user_ids=[]
    )
    assert len(client.create_calls) == 2
    assert client.create_calls[0]["name"] == "homeschool-game"
    retried_name = client.create_calls[1]["name"]
    assert retried_name.startswith("homeschool-game-")
    assert result["name"] == retried_name


def test_create_project_channel_name_taken_retry_is_bounded() -> None:
    client = _AlwaysNameTakenWebClient()
    with pytest.raises(provisioning.ProvisioningError) as exc_info:
        provisioning.create_project_channel(client, title="Homeschool Game!", invite_user_ids=[])
    assert exc_info.value.code == "name_taken"
    # Bounded: must not retry forever.
    assert len(client.create_calls) <= 9


# --- create_project_channel: missing_scope / other create errors -----------


def test_create_project_channel_wraps_missing_scope_as_provisioning_error() -> None:
    client = _CreateFailsWebClient("missing_scope")
    with pytest.raises(provisioning.ProvisioningError) as exc_info:
        provisioning.create_project_channel(client, title="Homeschool Game!", invite_user_ids=[])
    assert exc_info.value.code == "missing_scope"


def test_create_project_channel_wraps_invalid_name_as_provisioning_error() -> None:
    client = _CreateFailsWebClient("invalid_name")
    with pytest.raises(provisioning.ProvisioningError) as exc_info:
        provisioning.create_project_channel(client, title="Homeschool Game!", invite_user_ids=[])
    assert exc_info.value.code == "invalid_name"


def test_create_project_channel_does_not_leak_raw_slack_api_error() -> None:
    """A raw SlackApiError-shaped exception must never escape — only
    ProvisioningError."""
    client = _CreateFailsWebClient("missing_scope")
    with pytest.raises(Exception) as exc_info:
        provisioning.create_project_channel(client, title="Homeschool Game!", invite_user_ids=[])
    assert isinstance(exc_info.value, provisioning.ProvisioningError)
    assert not isinstance(exc_info.value, _SlackApiErrorLike)


def test_create_project_channel_reraises_non_slack_shaped_create_error_unwrapped() -> None:
    """An exception with no ``.response`` (not SlackApiError-shaped) is a
    bug/network failure, not a Slack error code — must propagate as-is,
    not be masked as a fabricated ProvisioningError."""
    client = _CreateRaisesPlainRuntimeErrorWebClient()
    with pytest.raises(RuntimeError, match="boom") as exc_info:
        provisioning.create_project_channel(client, title="Homeschool Game!", invite_user_ids=[])
    assert not isinstance(exc_info.value, provisioning.ProvisioningError)


# --- create_project_channel: invite best-effort ------------------------------


def test_create_project_channel_swallows_already_in_channel_invite_error() -> None:
    client = _InviteFailsWebClient("already_in_channel")
    result = provisioning.create_project_channel(
        client, title="Homeschool Game!", invite_user_ids=["U1"]
    )
    assert result == {"channel_id": "C9", "name": "homeschool-game"}


def test_create_project_channel_swallows_cant_invite_self_error() -> None:
    client = _InviteFailsWebClient("cant_invite_self")
    result = provisioning.create_project_channel(
        client, title="Homeschool Game!", invite_user_ids=["U1"]
    )
    assert result == {"channel_id": "C9", "name": "homeschool-game"}


def test_create_project_channel_propagates_other_invite_errors() -> None:
    client = _InviteFailsWebClient("some_other_error")
    with pytest.raises(_SlackApiErrorLike):
        provisioning.create_project_channel(
            client, title="Homeschool Game!", invite_user_ids=["U1"]
        )


# --- create_project_channel: topic best-effort -------------------------------


def test_create_project_channel_swallows_topic_errors() -> None:
    client = _TopicFailsWebClient()
    result = provisioning.create_project_channel(
        client, title="Homeschool Game!", invite_user_ids=[], purpose="anything"
    )
    assert result == {"channel_id": "C9", "name": "homeschool-game"}


# --- archive_channel ---------------------------------------------------


class _ArchiveWebClient:
    """Fake client recording ``conversations_archive`` calls."""

    def __init__(self) -> None:
        self.archive_calls: list[dict[str, Any]] = []

    def conversations_archive(self, *, channel: str) -> dict[str, Any]:
        self.archive_calls.append({"channel": channel})
        return {"ok": True}


class _ArchiveFailsWebClient:
    def __init__(self, error: str) -> None:
        self._error = error
        self.archive_calls: list[dict[str, Any]] = []

    def conversations_archive(self, *, channel: str) -> dict[str, Any]:
        self.archive_calls.append({"channel": channel})
        raise _SlackApiErrorLike(self._error)


class _ArchiveRaisesPlainRuntimeErrorWebClient:
    def __init__(self) -> None:
        self.archive_calls: list[dict[str, Any]] = []

    def conversations_archive(self, *, channel: str) -> dict[str, Any]:
        self.archive_calls.append({"channel": channel})
        raise RuntimeError("boom")


def test_archive_channel_happy_path_returns_archived_true() -> None:
    client = _ArchiveWebClient()
    result = provisioning.archive_channel(client, "C1")
    assert result == {"channel_id": "C1", "archived": True}
    assert client.archive_calls == [{"channel": "C1"}]


def test_archive_channel_already_archived_is_treated_as_success() -> None:
    client = _ArchiveFailsWebClient("already_archived")
    result = provisioning.archive_channel(client, "C1")
    assert result == {"channel_id": "C1", "archived": True}


def test_archive_channel_wraps_cant_archive_general_as_provisioning_error() -> None:
    client = _ArchiveFailsWebClient("cant_archive_general")
    with pytest.raises(provisioning.ProvisioningError) as exc_info:
        provisioning.archive_channel(client, "C1")
    assert exc_info.value.code == "cant_archive_general"


def test_archive_channel_wraps_missing_scope_as_provisioning_error() -> None:
    client = _ArchiveFailsWebClient("missing_scope")
    with pytest.raises(provisioning.ProvisioningError) as exc_info:
        provisioning.archive_channel(client, "C1")
    assert exc_info.value.code == "missing_scope"


def test_archive_channel_wraps_channel_not_found_as_provisioning_error() -> None:
    client = _ArchiveFailsWebClient("channel_not_found")
    with pytest.raises(provisioning.ProvisioningError) as exc_info:
        provisioning.archive_channel(client, "C1")
    assert exc_info.value.code == "channel_not_found"


def test_archive_channel_does_not_leak_raw_slack_api_error() -> None:
    client = _ArchiveFailsWebClient("missing_scope")
    with pytest.raises(Exception) as exc_info:
        provisioning.archive_channel(client, "C1")
    assert isinstance(exc_info.value, provisioning.ProvisioningError)
    assert not isinstance(exc_info.value, _SlackApiErrorLike)


def test_archive_channel_reraises_non_slack_shaped_error_unwrapped() -> None:
    client = _ArchiveRaisesPlainRuntimeErrorWebClient()
    with pytest.raises(RuntimeError, match="boom") as exc_info:
        provisioning.archive_channel(client, "C1")
    assert not isinstance(exc_info.value, provisioning.ProvisioningError)
