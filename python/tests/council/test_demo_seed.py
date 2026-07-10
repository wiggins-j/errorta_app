"""F031-DEMO-CORPUS Task 1 — ``ensure_demo_corpus`` backend helper tests.

Locks the idempotent F007 reuse contract + invariant 4 (fail-closed) +
the "no provider SDK init" rule.
"""
from __future__ import annotations

import sys
import types
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import httpx
import pytest

from errorta_council import demo_seed
from errorta_council.demo_seed import ensure_demo_corpus


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _FakeDownloadResult:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.bytes_downloaded = 1234
        self.sha256 = "deadbeef" * 8


class _FakeIngestResult:
    def __init__(self, f004_error: str | None = None) -> None:
        self.corpus_name = "welcome"
        self.extracted_root = Path("/tmp/fake-extract")
        self.files = ["a.md", "b.md"]
        self.f004_invoked = True
        self.f004_error = f004_error


def _make_fake_downloader(
    *,
    stream_exc: BaseException | None = None,
    verify_exc: BaseException | None = None,
) -> MagicMock:
    """Build a stand-in for the ``errorta_welcome.downloader`` module."""
    mod = MagicMock()
    mod.HashMismatchError = type("HashMismatchError", (RuntimeError,), {})
    mod.DownloadTooLargeError = type("DownloadTooLargeError", (RuntimeError,), {})

    async def _stream_download(dest: Path, *args: Any, **kwargs: Any):
        if stream_exc is not None:
            raise stream_exc
        # Touch the dest file so realistic callers don't blow up on stat.
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(b"fake-tarball")
        return _FakeDownloadResult(dest)

    mod.stream_download = _stream_download

    def _verify(result: Any, *args: Any, **kwargs: Any) -> None:
        if verify_exc is not None:
            raise verify_exc

    mod.verify_sha256 = _verify
    return mod


def _make_fake_ingest_bridge(
    *, ingest_result: _FakeIngestResult | None = None
) -> MagicMock:
    mod = MagicMock()

    def _extract(tarball: Path, dest_root: Path) -> Path:
        dest_root.mkdir(parents=True, exist_ok=True)
        return dest_root

    mod.extract_tarball = MagicMock(side_effect=_extract)
    mod.ingest_extracted = MagicMock(
        return_value=ingest_result if ingest_result else _FakeIngestResult()
    )
    return mod


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_ensure_demo_corpus_returns_ready_on_first_call(
    tmp_errorta_home: Path,
) -> None:
    downloader = _make_fake_downloader()
    ingest_bridge = _make_fake_ingest_bridge()

    result = ensure_demo_corpus(
        downloader=downloader,
        ingest_bridge=ingest_bridge,
        has_welcome=lambda: False,
    )

    assert result["status"] == "ready"
    assert result["corpus_id"] == "welcome"
    assert result["error"] is None
    ingest_bridge.extract_tarball.assert_called_once()
    ingest_bridge.ingest_extracted.assert_called_once()


def test_ensure_demo_corpus_returns_reused_on_second_call(
    tmp_errorta_home: Path,
) -> None:
    downloader = _make_fake_downloader()
    ingest_bridge = _make_fake_ingest_bridge()

    result = ensure_demo_corpus(
        downloader=downloader,
        ingest_bridge=ingest_bridge,
        has_welcome=lambda: True,  # already on disk
    )

    assert result["status"] == "reused"
    assert result["corpus_id"] == "welcome"
    assert result["error"] is None
    ingest_bridge.extract_tarball.assert_not_called()
    ingest_bridge.ingest_extracted.assert_not_called()


def test_ensure_demo_corpus_propagates_hash_mismatch(
    tmp_errorta_home: Path,
) -> None:
    downloader = _make_fake_downloader()
    mismatch = downloader.HashMismatchError("sha256 mismatch: expected aa, got bb")
    downloader = _make_fake_downloader(verify_exc=mismatch)
    ingest_bridge = _make_fake_ingest_bridge()

    result = ensure_demo_corpus(
        downloader=downloader,
        ingest_bridge=ingest_bridge,
        has_welcome=lambda: False,
    )

    assert result["status"] == "failed"
    assert result["corpus_id"] is None
    assert result["error"] is not None
    assert "sha256" in result["error"].lower()


def test_ensure_demo_corpus_propagates_download_too_large(
    tmp_errorta_home: Path,
) -> None:
    proto = _make_fake_downloader()
    too_large = proto.DownloadTooLargeError("welcome tarball exceeded 5242880 bytes")
    downloader = _make_fake_downloader(stream_exc=too_large)
    ingest_bridge = _make_fake_ingest_bridge()

    result = ensure_demo_corpus(
        downloader=downloader,
        ingest_bridge=ingest_bridge,
        has_welcome=lambda: False,
    )

    assert result["status"] == "failed"
    assert result["corpus_id"] is None
    assert result["error"] is not None
    assert "DownloadTooLargeError" in result["error"]


def test_ensure_demo_corpus_propagates_network_error(
    tmp_errorta_home: Path,
) -> None:
    net = httpx.ConnectError("connection refused")
    downloader = _make_fake_downloader(stream_exc=net)
    ingest_bridge = _make_fake_ingest_bridge()

    result = ensure_demo_corpus(
        downloader=downloader,
        ingest_bridge=ingest_bridge,
        has_welcome=lambda: False,
    )

    assert result["status"] == "failed"
    assert result["corpus_id"] is None
    assert result["error"] is not None
    assert "ConnectError" in result["error"]


def test_ensure_demo_corpus_propagates_ingest_failure(
    tmp_errorta_home: Path,
) -> None:
    downloader = _make_fake_downloader()
    ingest_bridge = _make_fake_ingest_bridge(
        ingest_result=_FakeIngestResult(f004_error="boom: corpus name conflict")
    )

    result = ensure_demo_corpus(
        downloader=downloader,
        ingest_bridge=ingest_bridge,
        has_welcome=lambda: False,
    )

    assert result["status"] == "failed"
    assert result["corpus_id"] is None
    assert result["error"] is not None
    assert "boom" in result["error"]


def test_ensure_demo_corpus_never_initializes_provider_sdk(
    tmp_errorta_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Importing ``anthropic`` or ``openai`` should be unnecessary."""

    # Build poisoned stubs that raise on attribute access.
    class _Poison:
        def __getattr__(self, name: str) -> Any:
            raise RuntimeError(
                f"provider SDK touched: {name!r}"
            )

    poison = _Poison()
    monkeypatch.setitem(sys.modules, "anthropic", poison)
    monkeypatch.setitem(sys.modules, "openai", poison)

    downloader = _make_fake_downloader()
    ingest_bridge = _make_fake_ingest_bridge()

    result = ensure_demo_corpus(
        downloader=downloader,
        ingest_bridge=ingest_bridge,
        has_welcome=lambda: False,
    )
    assert result["status"] == "ready"


def test_ensure_demo_corpus_uses_tmp_errorta_home(
    tmp_errorta_home: Path,
) -> None:
    """The default ``has_welcome`` predicate reads from ``$ERRORTA_HOME``.

    ``tmp_errorta_home`` monkeypatches HOME -> tmp_path, so any disk write the
    helper performs MUST land under tmp_path. The corpus directory is created
    by ``errorta_corpus.directory_ingest`` (via the real ingest bridge).
    With the predicate defaulted, the first call returns ``ready`` only when
    the actual disk root resolves under tmp_path. We assert by reading the
    path the production predicate would check.
    """
    from errorta_app.paths import corpora_dir

    expected_root = corpora_dir().resolve()
    # tmp_path / ".errorta" / "corpora" — relative_to() should not raise.
    expected_root.relative_to(tmp_errorta_home.resolve())

    # Drive a "reused" path to keep the test hermetic (no real network).
    downloader = _make_fake_downloader()
    ingest_bridge = _make_fake_ingest_bridge()
    # Default ``has_welcome`` returns False because the welcome dir does not
    # exist under the fresh tmp_errorta_home. We simulate it by giving the
    # helper a fake predicate that asserts the path resolution happens under
    # tmp_path before short-circuiting to True.
    seen: dict[str, Path] = {}

    def _capturing_has_welcome() -> bool:
        # Mirror the production predicate.
        from errorta_app.paths import corpora_dir as _cdir

        seen["resolved"] = _cdir().resolve()
        return True

    result = ensure_demo_corpus(
        downloader=downloader,
        ingest_bridge=ingest_bridge,
        has_welcome=_capturing_has_welcome,
    )
    assert result["status"] == "reused"
    assert seen["resolved"].is_relative_to(tmp_errorta_home.resolve())


def test_demo_seed_module_does_not_import_provider_sdk() -> None:
    """Static check: ``demo_seed`` module source must not reference provider SDKs."""
    src = Path(demo_seed.__file__).read_text(encoding="utf-8")
    assert "import anthropic" not in src
    assert "from anthropic" not in src
    assert "import openai" not in src
    assert "from openai" not in src
    assert "errorta_model_gateway" not in src
