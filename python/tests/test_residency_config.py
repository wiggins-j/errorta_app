"""Tests for errorta_residency.config — F-INFRA-12 Phase B Slice 1."""
from __future__ import annotations

import importlib
import json
import re
from pathlib import Path
from threading import Thread

import pytest


@pytest.fixture
def residency_module(tmp_errorta_home: Path):
    """Reload errorta_residency.config under an isolated HOME so it picks
    up the tmp data dir and resets module-level state between tests."""
    # Also force errorta_app.paths to re-resolve under the new HOME.
    import errorta_app.paths as paths_mod
    importlib.reload(paths_mod)
    import errorta_residency.config as config
    importlib.reload(config)
    return config


def _residency_file(tmp_errorta_home: Path) -> Path:
    return tmp_errorta_home / ".errorta" / "data-residency.json"


def test_load_returns_default_when_no_file(residency_module) -> None:
    state = residency_module.load()
    assert state.mode == "local"
    assert state.ssh_host is None
    assert state.ssh_port == 22
    assert state.cloud_url is None
    assert state.cloud_token is None
    assert state.updated_at is None


def test_load_returns_default_on_malformed_json(
    residency_module, tmp_errorta_home: Path
) -> None:
    path = _residency_file(tmp_errorta_home)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json")
    state = residency_module.load()
    assert state.mode == "local"


def test_save_and_load_roundtrip_ssh_remote(
    residency_module, tmp_errorta_home: Path
) -> None:
    state = residency_module.ResidencyState(
        mode="ssh-remote",
        ssh_host="example-host",
        ssh_port=2222,
        ssh_key_path="~/.ssh/id_ed25519",
        ssh_username="ops",
        remote_sidecar_port=8770,
        local_tunnel_port=18770,
    )
    residency_module.save(state)
    reloaded = residency_module.load()
    assert reloaded.mode == "ssh-remote"
    assert reloaded.ssh_host == "example-host"
    assert reloaded.ssh_port == 2222
    assert reloaded.ssh_key_path == "~/.ssh/id_ed25519"
    assert reloaded.ssh_username == "ops"
    assert reloaded.remote_sidecar_port == 8770
    assert reloaded.local_tunnel_port is None


def test_update_cloud_mode_happy_path(residency_module) -> None:
    state = residency_module.update(mode="cloud", cloud_url="https://x.example.com")
    assert state.mode == "cloud"
    assert state.cloud_url == "https://x.example.com"


def test_update_cloud_mode_without_url_raises(residency_module) -> None:
    with pytest.raises(ValueError, match="cloud_url"):
        residency_module.update(mode="cloud")


def test_update_cloud_mode_http_scheme_raises(residency_module) -> None:
    with pytest.raises(ValueError, match="https://"):
        residency_module.update(mode="cloud", cloud_url="http://x.example.com")


def test_update_ssh_remote_without_host_raises(residency_module) -> None:
    with pytest.raises(ValueError, match="ssh_host"):
        residency_module.update(mode="ssh-remote")


def test_update_bogus_mode_raises(residency_module) -> None:
    with pytest.raises(ValueError, match="mode"):
        residency_module.update(mode="bogus")


def test_update_invalid_ssh_port_raises(residency_module) -> None:
    with pytest.raises(ValueError, match="ssh_port"):
        residency_module.update(ssh_port=70000)
    with pytest.raises(ValueError, match="ssh_port"):
        residency_module.update(ssh_port=0)


def test_update_invalid_tunnel_port_raises(residency_module) -> None:
    with pytest.raises(ValueError, match="local_tunnel_port"):
        residency_module.update(local_tunnel_port=0)
    with pytest.raises(ValueError, match="remote_sidecar_port"):
        residency_module.update(remote_sidecar_port=0)


def test_cloud_token_redacted_on_disk_but_returned_in_memory(
    residency_module, tmp_errorta_home: Path
) -> None:
    secret = "super-secret-bearer-token-do-not-leak"
    state = residency_module.update(
        mode="cloud",
        cloud_url="https://x.example.com",
        cloud_token=secret,
    )
    # In-memory returned state carries the token.
    assert state.cloud_token == secret

    # Disk has it redacted.
    raw = _residency_file(tmp_errorta_home).read_text()
    assert secret not in raw
    on_disk = json.loads(raw)
    assert on_disk["cloud_token"] is None

    # A fresh load() does NOT surface the in-memory token (it lives only
    # in process memory; reload starts fresh).
    reloaded = residency_module.load()
    assert reloaded.cloud_token is None
    assert reloaded.cloud_url == "https://x.example.com"


def test_update_stamps_iso_z_timestamp(residency_module) -> None:
    state = residency_module.update(mode="local")
    assert state.updated_at is not None
    # ISO 8601 with Z suffix, e.g. "2026-06-08T12:34:56.789012Z"
    assert re.match(
        r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?Z$",
        state.updated_at,
    ), f"updated_at not ISO Z: {state.updated_at!r}"


def test_concurrent_updates_do_not_corrupt_file(
    residency_module, tmp_errorta_home: Path
) -> None:
    ports = list(range(2200, 2210))
    errors: list[BaseException] = []

    def worker(port: int) -> None:
        try:
            residency_module.update(
                mode="ssh-remote",
                ssh_host="example-host",
                ssh_port=port,
            )
        except BaseException as exc:  # pragma: no cover - surfaced below
            errors.append(exc)

    threads = [Thread(target=worker, args=(p,)) for p in ports]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, f"concurrent updates raised: {errors!r}"

    # File must be valid JSON, deserialize cleanly, and carry one of the
    # written ports.
    raw = _residency_file(tmp_errorta_home).read_text()
    payload = json.loads(raw)
    assert payload["mode"] == "ssh-remote"
    assert payload["ssh_host"] == "example-host"
    assert payload["ssh_port"] in ports

    final = residency_module.load()
    assert final.mode == "ssh-remote"
    assert final.ssh_port in ports


def test_ssh_remote_keeps_local_tunnel_port_runtime_only(
    residency_module, tmp_errorta_home: Path
) -> None:
    state = residency_module.update(
        mode="ssh-remote",
        ssh_host="example-host",
        remote_sidecar_port=8770,
        local_tunnel_port=18770,
    )

    assert state.local_tunnel_port == 18770
    payload = json.loads(_residency_file(tmp_errorta_home).read_text())
    assert payload["local_tunnel_port"] is None
    assert residency_module.load().local_tunnel_port == 18770


def test_load_ignores_stale_local_tunnel_port_on_disk(
    residency_module, tmp_errorta_home: Path
) -> None:
    path = _residency_file(tmp_errorta_home)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "mode": "ssh-remote",
                "ssh_host": "example-host",
                "ssh_port": 22,
                "remote_sidecar_port": 8770,
                "local_tunnel_port": 18770,
            }
        )
    )

    state = residency_module.load()

    assert state.mode == "ssh-remote"
    assert state.local_tunnel_port is None
