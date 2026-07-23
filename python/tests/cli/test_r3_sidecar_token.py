"""R3 (CLI side) — per-sidecar bearer token: mint / store 0600 / read / send.

The CLI mints a token at ``spawn()``, stores it in a SEPARATE 0600 file (never
world-readable ``sidecar.json``), hands it to the spawned sidecar via
``ERRORTA_SIDECAR_TOKEN``, and re-reads it on adoption. The client attaches
``Authorization: Bearer <token>`` when a token is available and origin-only
otherwise (grace).
"""
from __future__ import annotations

import stat
from pathlib import Path

import httpx
import pytest

from errorta_cli import config, sidecar
from errorta_cli.client import AUTH_HEADER, ORIGIN_HEADER, ORIGIN_VALUE, SidecarClient


class _FakeProc:
    def __init__(self, pid: int = 4321) -> None:
        self.pid = pid
        self.returncode = None

    def poll(self):
        return None


# --------------------------------------------------------------------------- #
# mint / store 0600 / read.
# --------------------------------------------------------------------------- #

def test_mint_token_is_unguessable_and_unique() -> None:
    a, b = sidecar.mint_token(), sidecar.mint_token()
    assert a != b
    assert len(a) >= 32


def test_write_token_is_0600_and_read_roundtrips(tmp_path: Path) -> None:
    assert sidecar.read_token(tmp_path) is None
    sidecar.write_token(tmp_path, "abc123")
    path = config.sidecar_token_path(tmp_path)
    assert path.name == "sidecar-token"
    mode = stat.S_IMODE(path.stat().st_mode)
    assert mode == 0o600, f"token file must be 0600, got {oct(mode)}"
    assert sidecar.read_token(tmp_path) == "abc123"


def test_read_token_none_when_empty(tmp_path: Path) -> None:
    config.sidecar_token_path(tmp_path).write_text("   \n", "utf-8")
    assert sidecar.read_token(tmp_path) is None


def test_clear_record_removes_token_file(tmp_path: Path) -> None:
    """R3 stale-secret cleanup: dropping the sidecar record also unlinks the
    0600 token file so a dead/stopped sidecar leaves no bearer on disk."""
    sidecar.write_record(tmp_path, {"port": 1, "pid": 2, "started_by": "cli"})
    sidecar.write_token(tmp_path, "leftover-token")
    assert config.sidecar_token_path(tmp_path).exists()

    sidecar.clear_record(tmp_path)

    assert not config.sidecar_record_path(tmp_path).exists()
    assert not config.sidecar_token_path(tmp_path).exists()
    assert sidecar.read_token(tmp_path) is None


def test_failed_spawn_removes_token_file(monkeypatch, tmp_path: Path) -> None:
    """A spawn whose child never becomes ready is killed AND its just-minted
    token file is removed (no stale secret for a sidecar that never came up)."""
    from errorta_cli.errors import SidecarUnreachable

    def fake_launch(argv, env):
        return _FakeProc(pid=4321)

    monkeypatch.setattr(sidecar, "_launch", fake_launch)
    # Never ready → _wait_ready raises → spawn cleans up.
    monkeypatch.setattr(sidecar, "probe_healthz", lambda port, **k: None)
    monkeypatch.setattr(sidecar, "_SPAWN_READY_BUDGET", 0.01)
    monkeypatch.setattr(sidecar, "_kill_child", lambda proc: None)

    with pytest.raises(SidecarUnreachable):
        sidecar.spawn(tmp_path, our_commit="abc")

    assert not config.sidecar_token_path(tmp_path).exists()


# --------------------------------------------------------------------------- #
# spawn: mint + store + hand to child via env; token on the handle.
# --------------------------------------------------------------------------- #

def _spawn_with_fake_launch(monkeypatch, tmp_path, launched: dict) -> sidecar.SidecarHandle:
    def fake_launch(argv, env):
        launched["env"] = env
        launched["port"] = int(env["ERRORTA_SIDECAR_PORT"])
        return _FakeProc(pid=4321)

    monkeypatch.setattr(sidecar, "_launch", fake_launch)
    monkeypatch.setattr(
        sidecar,
        "probe_healthz",
        lambda port, **k: {"build": {"commit": "abc"}}
        if port == launched.get("port")
        else None,
    )
    monkeypatch.setattr(sidecar, "_scan_errorta_processes", lambda **k: [])
    return sidecar.resolve(tmp_path, our_commit="abc")


def test_spawn_mints_token_stores_0600_and_passes_to_child(monkeypatch, tmp_path: Path) -> None:
    launched: dict = {}
    handle = _spawn_with_fake_launch(monkeypatch, tmp_path, launched)

    # Token is on the handle, on disk (0600), and handed to the child via env.
    assert handle.token
    token_on_disk = sidecar.read_token(tmp_path)
    assert token_on_disk == handle.token
    assert launched["env"][sidecar.SIDECAR_TOKEN_ENV] == handle.token
    mode = stat.S_IMODE(config.sidecar_token_path(tmp_path).stat().st_mode)
    assert mode == 0o600


def test_token_is_not_written_into_sidecar_json(monkeypatch, tmp_path: Path) -> None:
    launched: dict = {}
    handle = _spawn_with_fake_launch(monkeypatch, tmp_path, launched)

    record = sidecar.read_record(tmp_path)
    assert record is not None
    # The secret must never appear in the world-readable 0644 discovery file.
    assert "token" not in record
    assert handle.token not in str(record)
    # And sidecar.json is NOT chmod-restricted (confirming why the token needs
    # its own 0600 file), while the token file IS restricted.
    json_mode = stat.S_IMODE(config.sidecar_record_path(tmp_path).stat().st_mode)
    token_mode = stat.S_IMODE(config.sidecar_token_path(tmp_path).stat().st_mode)
    assert token_mode == 0o600
    assert json_mode != 0o600


# --------------------------------------------------------------------------- #
# adoption reads the token (does not mint); respawn rotates it.
# --------------------------------------------------------------------------- #

def test_adoption_reads_existing_token_without_minting(monkeypatch, tmp_path: Path) -> None:
    # A live CLI sidecar already advertised, with its token file on disk.
    sidecar.write_record(
        tmp_path, {"port": 5555, "pid": 77, "commit": "abc", "started_by": "cli"}
    )
    sidecar.write_token(tmp_path, "pre-existing-token")
    monkeypatch.setattr(
        sidecar, "probe_healthz", lambda port, **k: {"build": {"commit": "abc"}}
    )

    def boom(*a, **k):
        raise AssertionError("must adopt, not spawn (no new mint)")

    monkeypatch.setattr(sidecar, "_launch", boom)

    handle = sidecar.resolve(tmp_path, our_commit="abc")
    assert handle.adopted is True
    # Adoption reads the SAME token; it did not mint/overwrite.
    assert handle.token == "pre-existing-token"
    assert sidecar.read_token(tmp_path) == "pre-existing-token"


def test_respawn_rotates_token(monkeypatch, tmp_path: Path) -> None:
    # A stale (dead) record + an old token file.
    sidecar.write_record(
        tmp_path, {"port": 5555, "pid": 77, "commit": "abc", "started_by": "cli"}
    )
    sidecar.write_token(tmp_path, "old-token")
    spawned: dict = {}

    def fake_launch(argv, env):
        spawned["port"] = int(env["ERRORTA_SIDECAR_PORT"])
        spawned["env"] = env
        return _FakeProc(pid=9001)

    monkeypatch.setattr(sidecar, "_launch", fake_launch)
    monkeypatch.setattr(
        sidecar,
        "probe_healthz",
        lambda port, **k: {"build": {"commit": "abc"}} if port == spawned.get("port") else None,
    )
    monkeypatch.setattr(sidecar, "_pid_alive", lambda pid: False)  # dead → respawn
    monkeypatch.setattr(sidecar, "_scan_errorta_processes", lambda **k: [])

    handle = sidecar.resolve(tmp_path, our_commit="abc")
    assert handle.adopted is False
    # A fresh token was minted, overwriting the old one, and handed to the child.
    assert handle.token and handle.token != "old-token"
    assert sidecar.read_token(tmp_path) == handle.token
    assert spawned["env"][sidecar.SIDECAR_TOKEN_ENV] == handle.token


# --------------------------------------------------------------------------- #
# client: sends the bearer when available, origin-only otherwise.
# --------------------------------------------------------------------------- #

def _capture(handler_store: dict):
    def handler(request: httpx.Request) -> httpx.Response:
        handler_store["origin"] = request.headers.get(ORIGIN_HEADER)
        handler_store["auth"] = request.headers.get(AUTH_HEADER)
        return httpx.Response(200, json={"ok": True})

    return handler


def test_client_sends_bearer_when_token_present() -> None:
    seen: dict = {}
    client = SidecarClient(
        "http://127.0.0.1:9",
        token="tok-xyz",
        transport=httpx.MockTransport(_capture(seen)),
    )
    with client:
        client.post_json("/coding/x", json={"a": 1})
    assert seen["origin"] == ORIGIN_VALUE
    assert seen["auth"] == "Bearer tok-xyz"


def test_client_omits_bearer_when_no_token() -> None:
    seen: dict = {}
    client = SidecarClient(
        "http://127.0.0.1:9", transport=httpx.MockTransport(_capture(seen))
    )
    with client:
        client.get_json("/healthz")
    assert seen["origin"] == ORIGIN_VALUE
    assert seen["auth"] is None
