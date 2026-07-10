"""F-INFRA-06 — Local diagnostic bundle export tests."""
from __future__ import annotations

import json
import socket
import zipfile
from pathlib import Path

import pytest

from errorta_diagnostics import build_bundle
from errorta_diagnostics import bundle as bundle_mod
from errorta_diagnostics import redact
from errorta_diagnostics.log_buffer import LogBuffer

SENTINEL = "__SENTINEL_PROMPT__"


# --- Fixtures ---------------------------------------------------------------


@pytest.fixture
def seeded_errorta_home(tmp_errorta_home: Path) -> Path:
    """Drop representative hardware.json / verdicts.jsonl / grounding.json files."""
    edir = tmp_errorta_home / ".errorta"
    edir.mkdir(parents=True, exist_ok=True)

    (edir / "hardware.json").write_text(
        json.dumps({"cpu": {"name": "TestCPU"}, "ram_gb": 16}), encoding="utf-8"
    )

    verdict_lines = [
        json.dumps(
            {
                "id": "v-1",
                "prompt": f"please leak {SENTINEL} now",
                "answer": "this contains the sentinel too: " + SENTINEL,
                "correction": "and the correction: " + SENTINEL,
                "verdict": {
                    "rating": "pass",
                    "reason": SENTINEL,
                    "failure_tags": [],
                    "confidence": 0.9,
                    "latency_ms": 42,
                },
                "judge_model": "qwen",
                "accepted": False,
                "correction_status": None,
                "created_at": "2026-06-07T12:00:00+00:00",
            }
        ),
        json.dumps(
            {
                "id": "v-2",
                "prompt": "ok",
                "answer": "ok",
                "verdict": {
                    "rating": "fail",
                    "failure_tags": ["hallucination"],
                    "latency_ms": 100,
                },
                "created_at": "2026-06-07T12:05:00+00:00",
            }
        ),
    ]
    (edir / "verdicts.jsonl").write_text("\n".join(verdict_lines) + "\n", encoding="utf-8")

    (edir / "grounding.json").write_text(
        json.dumps({"sig-a": {"correction": "x"}, "sig-b": {"correction": "y"}}),
        encoding="utf-8",
    )
    return edir


# --- Redaction unit tests ---------------------------------------------------


def test_redact_home_path_substitutes_and_counts(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", "/Users/alice")
    text = "log at /Users/alice/.errorta/x and also /Users/alice/y"
    out, n = redact.redact_home_path(text)
    assert "/Users/alice" not in out
    assert out.count("$HOME") == 2
    assert n == 2


def test_redact_tokens_catches_known_prefixes() -> None:
    text = (
        "API sk-AbCdEfGhIjKlMnOpQrSt and pat ghp_aaaaaaaaaaaaaaaaaaaaaaaa "
        "and AKIAABCDEFGHIJKLMNOP done"
    )
    out, n = redact.redact_tokens(text)
    assert n == 3
    assert "sk-" not in out
    assert "ghp_" not in out
    assert "AKIA" not in out
    assert out.count("<token-redacted>") == 3


def test_redact_ips_skips_loopback() -> None:
    text = "connect 198.51.100.5 then 127.0.0.1 then 10.0.0.4"
    out, n = redact.redact_ips(text)
    assert n == 2
    assert "127.0.0.1" in out
    assert "198.51.100.5" not in out
    assert out.count("<ip-redacted>") == 2


# --- Bundle integration -----------------------------------------------------


def test_build_bundle_produces_expected_files(
    seeded_errorta_home: Path, tmp_path: Path
) -> None:
    dest = tmp_path / "out" / "errorta-diagnostics.zip"
    result = build_bundle(dest, user_note="please help", log_buffer=LogBuffer())

    assert Path(result["path"]).exists()
    assert len(result["sha256"]) == 64
    assert set(result["files"]) == set(bundle_mod.BUNDLE_FILES)

    with zipfile.ZipFile(dest) as zf:
        names = set(zf.namelist())
        for name in bundle_mod.BUNDLE_FILES:
            assert name in names, f"missing {name}"


def test_sentinel_never_appears_in_any_bundle_file(
    seeded_errorta_home: Path, tmp_path: Path
) -> None:
    dest = tmp_path / "bundle.zip"
    build_bundle(dest, user_note="", log_buffer=LogBuffer())

    with zipfile.ZipFile(dest) as zf:
        for name in zf.namelist():
            data = zf.read(name).decode("utf-8", errors="replace")
            assert SENTINEL not in data, f"sentinel leaked into {name}"


def test_verdicts_summary_contains_only_allowlisted_fields(
    seeded_errorta_home: Path, tmp_path: Path
) -> None:
    dest = tmp_path / "bundle.zip"
    build_bundle(dest, log_buffer=LogBuffer())
    with zipfile.ZipFile(dest) as zf:
        verdicts = json.loads(zf.read("verdicts-summary.json"))

    assert isinstance(verdicts, list)
    assert len(verdicts) == 2
    allowed = set(bundle_mod.VERDICT_KEEP_FIELDS)
    for v in verdicts:
        assert set(v.keys()) <= allowed
        # No prompt/answer/correction fields ever.
        assert "prompt" not in v
        assert "answer" not in v
        assert "correction" not in v


def test_pm_working_memory_diagnostics_contains_status_not_raw_content(
    seeded_errorta_home: Path, tmp_path: Path
) -> None:
    from errorta_council.coding.ledger import LedgerStore
    from errorta_project_grounding.update_pipeline import sync_from_ledger

    root = seeded_errorta_home / "council" / "coding-projects"
    store = LedgerStore("diag-pmwm", root=root)
    store.create_project(
        north_star="PRIVATE PM MEMORY BODY",
        definition_of_done="done",
        target="new",
        repo_path=None,
    )
    sync_from_ledger(store)

    dest = tmp_path / "bundle.zip"
    build_bundle(dest, log_buffer=LogBuffer())
    with zipfile.ZipFile(dest) as zf:
        data = zf.read("pm-working-memory.json").decode("utf-8")
        payload = json.loads(data)

    assert payload["projects"][0]["project_id"] == "diag-pmwm"
    assert payload["projects"][0]["memory_ref"].startswith("mem:")
    assert "PRIVATE PM MEMORY BODY" not in data


def test_redaction_manifest_has_integer_counts(
    seeded_errorta_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(seeded_errorta_home.parent))
    dest = tmp_path / "bundle.zip"
    build_bundle(dest, user_note="path /Users/x and ip 8.8.8.8", log_buffer=LogBuffer())
    with zipfile.ZipFile(dest) as zf:
        manifest = json.loads(zf.read("redaction-manifest.json"))

    assert "rules" in manifest
    for key in ("home_path", "username", "ips", "tokens", "corpus_paths"):
        assert key in manifest["rules"]
        assert isinstance(manifest["rules"][key], int)


# --- Static & network hygiene ----------------------------------------------


def test_no_network_imports_in_diagnostics_package() -> None:
    pkg_dir = Path(bundle_mod.__file__).parent
    forbidden = ("import socket", "import httpx", "import urllib.request", "import requests")
    for py in pkg_dir.glob("*.py"):
        text = py.read_text(encoding="utf-8")
        for needle in forbidden:
            assert needle not in text, f"{py.name} contains forbidden {needle!r}"


def test_build_bundle_works_with_socket_blocked(
    seeded_errorta_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _boom(*a, **kw):  # pragma: no cover - if called the test fails
        raise OSError("network blocked")

    monkeypatch.setattr(socket, "socket", _boom)
    dest = tmp_path / "bundle.zip"
    result = build_bundle(dest, log_buffer=LogBuffer())
    assert Path(result["path"]).exists()


# --- Log buffer -------------------------------------------------------------


def test_log_buffer_ring_caps_at_capacity() -> None:
    buf = LogBuffer(capacity_bytes=64)
    for i in range(50):
        buf.append(f"line-{i:03d}-padded-padded")
    assert buf.size_bytes <= 64
    # Oldest entries should have been dropped.
    snapshot = buf.snapshot()
    assert snapshot, "buffer should not be empty"
    assert all("line-" in line for line in snapshot)
    # The very first lines must have been evicted.
    assert "line-000-padded-padded" not in snapshot
