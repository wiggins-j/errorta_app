from __future__ import annotations

from pathlib import Path

import pytest

from errorta_slack import linking, store

CHANNEL_ID = "C123"
PROJECT_ID = "errorta"
REQUESTER_USER_ID = "U123"


@pytest.fixture(autouse=True)
def _isolated_errorta_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("ERRORTA_HOME", str(tmp_path))
    return tmp_path


def test_request_link_returns_link_id_in_awaiting_owner_state() -> None:
    link_id = linking.request_link(CHANNEL_ID, PROJECT_ID, REQUESTER_USER_ID)

    assert isinstance(link_id, str)
    assert link_id

    record = linking.status(link_id)
    assert record is not None
    assert record["state"] == "awaiting_owner"
    assert record["channel_id"] == CHANNEL_ID
    assert record["project_id"] == PROJECT_ID
    assert record["requester_user_id"] == REQUESTER_USER_ID


def test_status_for_unknown_link_id_returns_none() -> None:
    assert linking.status("nope") is None


def test_approve_link_binds_channel_and_updates_state() -> None:
    link_id = linking.request_link(CHANNEL_ID, PROJECT_ID, REQUESTER_USER_ID)

    record = linking.approve_link(link_id)

    assert record["state"] == "approved"
    assert store.binding_for(CHANNEL_ID) is not None
    assert store.binding_for(CHANNEL_ID)["project_id"] == PROJECT_ID

    persisted = linking.status(link_id)
    assert persisted is not None
    assert persisted["state"] == "approved"


def test_deny_link_leaves_no_binding() -> None:
    link_id = linking.request_link(CHANNEL_ID, PROJECT_ID, REQUESTER_USER_ID)

    record = linking.deny_link(link_id)

    assert record["state"] == "denied"
    assert store.binding_for(CHANNEL_ID) is None

    persisted = linking.status(link_id)
    assert persisted is not None
    assert persisted["state"] == "denied"


def test_multiple_pending_links_are_independent() -> None:
    link_id_1 = linking.request_link("C111", "proj-a", "U1")
    link_id_2 = linking.request_link("C222", "proj-b", "U2")

    assert link_id_1 != link_id_2

    linking.approve_link(link_id_1)
    linking.deny_link(link_id_2)

    assert store.binding_for("C111") is not None
    assert store.binding_for("C222") is None
    assert linking.status(link_id_1)["state"] == "approved"
    assert linking.status(link_id_2)["state"] == "denied"


def test_approve_unknown_link_id_raises_key_error() -> None:
    with pytest.raises(KeyError):
        linking.approve_link("nope")


def test_deny_unknown_link_id_raises_key_error() -> None:
    with pytest.raises(KeyError):
        linking.deny_link("nope")


# --- Terminal-state guards (approve-after-deny / deny-after-approve) ------


def test_approve_after_deny_is_rejected_and_leaves_no_binding() -> None:
    link_id = linking.request_link(CHANNEL_ID, PROJECT_ID, REQUESTER_USER_ID)
    linking.deny_link(link_id)

    with pytest.raises(linking.LinkingError) as excinfo:
        linking.approve_link(link_id)

    assert excinfo.value.code == "link_not_pending"
    assert store.binding_for(CHANNEL_ID) is None
    assert linking.status(link_id)["state"] == "denied"


def test_deny_after_approve_is_rejected_and_binding_is_unchanged() -> None:
    link_id = linking.request_link(CHANNEL_ID, PROJECT_ID, REQUESTER_USER_ID)
    linking.approve_link(link_id)

    with pytest.raises(linking.LinkingError) as excinfo:
        linking.deny_link(link_id)

    assert excinfo.value.code == "link_not_pending"
    assert store.binding_for(CHANNEL_ID) is not None
    assert store.binding_for(CHANNEL_ID)["project_id"] == PROJECT_ID
    assert linking.status(link_id)["state"] == "approved"


def test_double_approve_is_rejected() -> None:
    link_id = linking.request_link(CHANNEL_ID, PROJECT_ID, REQUESTER_USER_ID)
    linking.approve_link(link_id)

    with pytest.raises(linking.LinkingError):
        linking.approve_link(link_id)

    assert linking.status(link_id)["state"] == "approved"


def test_double_deny_is_rejected() -> None:
    link_id = linking.request_link(CHANNEL_ID, PROJECT_ID, REQUESTER_USER_ID)
    linking.deny_link(link_id)

    with pytest.raises(linking.LinkingError):
        linking.deny_link(link_id)

    assert linking.status(link_id)["state"] == "denied"
