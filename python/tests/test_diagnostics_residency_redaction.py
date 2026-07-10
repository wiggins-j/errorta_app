"""F-INFRA-12 Phase B Slice 10 — diagnostic bundle redaction of
``data-residency.json``.

The diagnostics bundle (F-INFRA-06-local) is the support-hand-off pathway:
operators paste it into an issue or DM it to a maintainer. The residency
config has fields a support engineer should NEVER receive verbatim
(SSH host, key path, cloud URL, cloud token). This test locks that contract.

Allow-list (passed through verbatim): ``mode``, ``ssh_port``,
``remote_sidecar_port``, ``tunnel_state``, ``updated_at``.
Everything else is masked to the literal string ``"<redacted>"`` and the
raw values must not leak into any other bundle entry either.
"""
from __future__ import annotations

import json
import zipfile
from pathlib import Path

import pytest

from errorta_diagnostics import build_bundle
from errorta_diagnostics import bundle as bundle_mod
from errorta_diagnostics.log_buffer import LogBuffer


SECRET_HOST = "example-host.private.example.com"
SECRET_KEY_PATH = "/Users/leaktest/.ssh/super_secret_ed25519"
SECRET_USERNAME = "leakuser"
SECRET_CLOUD_URL = "https://errorta-private.do-not-leak.example"
SECRET_CLOUD_TOKEN = "tok_THIS_MUST_NEVER_APPEAR_IN_THE_BUNDLE_xyz123"


@pytest.fixture
def seeded_with_residency(tmp_errorta_home: Path) -> Path:
    """Drop a fully-populated data-residency.json under ~/.errorta."""
    edir = tmp_errorta_home / ".errorta"
    edir.mkdir(parents=True, exist_ok=True)
    (edir / "data-residency.json").write_text(
        json.dumps(
            {
                "mode": "ssh-remote",
                "ssh_host": SECRET_HOST,
                "ssh_port": 2202,
                "ssh_key_path": SECRET_KEY_PATH,
                "ssh_username": SECRET_USERNAME,
                "remote_sidecar_port": 8770,
                "cloud_url": SECRET_CLOUD_URL,
                "cloud_token": SECRET_CLOUD_TOKEN,
                "updated_at": "2026-06-08T12:34:56.000000Z",
                "tunnel_state": "up",
            },
            indent=2,
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    return edir


def _extract_residency(zip_path: Path) -> dict:
    with zipfile.ZipFile(zip_path) as zf:
        return json.loads(zf.read("data-residency.json"))


def test_residency_bundle_entry_present(
    seeded_with_residency: Path, tmp_path: Path
) -> None:
    dest = tmp_path / "bundle.zip"
    build_bundle(dest, log_buffer=LogBuffer())
    assert "data-residency.json" in bundle_mod.BUNDLE_FILES
    with zipfile.ZipFile(dest) as zf:
        assert "data-residency.json" in zf.namelist()


def test_allowed_fields_pass_through_verbatim(
    seeded_with_residency: Path, tmp_path: Path
) -> None:
    dest = tmp_path / "bundle.zip"
    build_bundle(dest, log_buffer=LogBuffer())
    payload = _extract_residency(dest)
    assert payload["mode"] == "ssh-remote"
    assert payload["ssh_port"] == 2202
    assert payload["remote_sidecar_port"] == 8770
    assert payload["tunnel_state"] == "up"
    assert payload["updated_at"] == "2026-06-08T12:34:56.000000Z"


def test_disallowed_fields_are_masked(
    seeded_with_residency: Path, tmp_path: Path
) -> None:
    dest = tmp_path / "bundle.zip"
    build_bundle(dest, log_buffer=LogBuffer())
    payload = _extract_residency(dest)
    # ssh_host out, ssh_key_path out, ssh_username out, cloud_url out,
    # cloud_token out — every one masked to the literal redaction string.
    masked = "<redacted>"
    assert payload["ssh_host"] == masked
    assert payload["ssh_key_path"] == masked
    assert payload["ssh_username"] == masked
    assert payload["cloud_url"] == masked
    assert payload["cloud_token"] == masked


def test_raw_secret_strings_do_not_appear_anywhere_in_bundle(
    seeded_with_residency: Path, tmp_path: Path
) -> None:
    dest = tmp_path / "bundle.zip"
    build_bundle(dest, log_buffer=LogBuffer())
    # Walk every entry; none may contain any of the raw secret strings.
    secrets = (
        SECRET_HOST,
        SECRET_KEY_PATH,
        SECRET_USERNAME,
        SECRET_CLOUD_URL,
        SECRET_CLOUD_TOKEN,
    )
    with zipfile.ZipFile(dest) as zf:
        for name in zf.namelist():
            blob = zf.read(name).decode("utf-8", errors="replace")
            for secret in secrets:
                assert secret not in blob, (
                    f"secret {secret!r} leaked into bundle entry {name!r}"
                )


def test_missing_residency_yields_local_default(
    tmp_errorta_home: Path, tmp_path: Path
) -> None:
    """When ~/.errorta/data-residency.json is absent, the bundle entry is
    the literal ``{"mode": "local"}`` rather than a file-not-found error.

    This makes the bundle self-describing: a support engineer can tell at a
    glance that the operator was running in default Local mode.
    """
    edir = tmp_errorta_home / ".errorta"
    edir.mkdir(parents=True, exist_ok=True)
    assert not (edir / "data-residency.json").exists()
    dest = tmp_path / "bundle.zip"
    build_bundle(dest, log_buffer=LogBuffer())
    payload = _extract_residency(dest)
    assert payload == {"mode": "local"}


def test_corrupt_residency_yields_local_default(
    tmp_errorta_home: Path, tmp_path: Path
) -> None:
    """A malformed data-residency.json must not crash the bundle build, and
    must NOT leak any of the malformed payload."""
    edir = tmp_errorta_home / ".errorta"
    edir.mkdir(parents=True, exist_ok=True)
    (edir / "data-residency.json").write_text(
        "this is not json " + SECRET_CLOUD_TOKEN, encoding="utf-8"
    )
    dest = tmp_path / "bundle.zip"
    build_bundle(dest, log_buffer=LogBuffer())
    payload = _extract_residency(dest)
    assert payload == {"mode": "local"}
    # Even the corrupt original cannot leak through.
    with zipfile.ZipFile(dest) as zf:
        for name in zf.namelist():
            blob = zf.read(name).decode("utf-8", errors="replace")
            assert SECRET_CLOUD_TOKEN not in blob
