"""Tests for errorta_welcome.downloader.

Covers streaming download, size cap enforcement, HTTP error handling, and
SHA-256 verification against the pinned hash. The network layer is faked —
no real httpx calls are made.
"""
from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Iterable, List, Optional

import httpx
import pytest

from errorta_welcome.downloader import (
    DownloadResult,
    DownloadTooLargeError,
    HashMismatchError,
    PinnedHash,
    stream_download,
    verify_sha256,
)


# ---------------------------------------------------------------------------
# Fakes for httpx streaming
# ---------------------------------------------------------------------------


class _FakeStreamResponse:
    def __init__(
        self,
        chunks: Iterable[bytes],
        *,
        status_code: int = 200,
        content_length: Optional[int] = None,
    ) -> None:
        self._chunks = list(chunks)
        self.status_code = status_code
        self.headers: dict[str, str] = {}
        if content_length is not None:
            self.headers["content-length"] = str(content_length)

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            request = httpx.Request("GET", "https://example.invalid/welcome.tar.gz")
            response = httpx.Response(self.status_code, request=request)
            raise httpx.HTTPStatusError(
                f"HTTP {self.status_code}", request=request, response=response
            )

    async def aiter_bytes(self, chunk_size: int = 64 * 1024):
        for chunk in self._chunks:
            yield chunk


class _FakeStreamCM:
    def __init__(self, response: _FakeStreamResponse) -> None:
        self._response = response

    async def __aenter__(self) -> _FakeStreamResponse:
        return self._response

    async def __aexit__(self, exc_type, exc, tb) -> None:  # noqa: D401
        return None


class _FakeAsyncClient:
    def __init__(self, response: _FakeStreamResponse) -> None:
        self._response = response
        self.closed = False
        self.requested_url: Optional[str] = None

    def stream(self, method: str, url: str) -> _FakeStreamCM:
        self.requested_url = url
        return _FakeStreamCM(self._response)

    async def aclose(self) -> None:
        self.closed = True


def _pin(
    *,
    sha256: str = "0" * 64,
    max_bytes: int = 5 * 1024 * 1024,
    source_url: str = "https://example.invalid/welcome.tar.gz",
) -> PinnedHash:
    return PinnedHash(
        tarball="welcome-corpus.tar.gz",
        source_url=source_url,
        version="0.1.0",
        sha256=sha256,
        max_bytes=max_bytes,
    )


# ---------------------------------------------------------------------------
# stream_download
# ---------------------------------------------------------------------------


async def test_stream_download_writes_bytes_to_target(tmp_path: Path) -> None:
    payload = b"hello world" * 100
    chunks = [payload[:300], payload[300:]]
    fake = _FakeAsyncClient(_FakeStreamResponse(chunks, content_length=len(payload)))
    pin = _pin()

    dest = tmp_path / "out" / "welcome.tar.gz"
    result = await stream_download(dest, pin=pin, client=fake)

    assert dest.exists()
    assert dest.read_bytes() == payload
    assert result.bytes_downloaded == len(payload)
    assert result.sha256 == hashlib.sha256(payload).hexdigest()
    # Externally-supplied client is not closed by stream_download.
    assert fake.closed is False
    assert fake.requested_url == pin.source_url


async def test_stream_download_invokes_progress_cb(tmp_path: Path) -> None:
    chunks = [b"a" * 10, b"b" * 20, b"c" * 30]
    total = sum(len(c) for c in chunks)
    fake = _FakeAsyncClient(_FakeStreamResponse(chunks, content_length=total))

    seen: List[tuple[int, Optional[int]]] = []
    dest = tmp_path / "welcome.tar.gz"
    await stream_download(dest, progress_cb=lambda w, t: seen.append((w, t)), pin=_pin(), client=fake)

    assert [w for w, _ in seen] == [10, 30, 60]
    assert all(t == total for _, t in seen)


async def test_stream_download_raises_on_http_error(tmp_path: Path) -> None:
    fake = _FakeAsyncClient(_FakeStreamResponse([b""], status_code=404))
    dest = tmp_path / "welcome.tar.gz"

    with pytest.raises(httpx.HTTPStatusError):
        await stream_download(dest, pin=_pin(), client=fake)


async def test_stream_download_enforces_max_bytes_cap(tmp_path: Path) -> None:
    # max_bytes = 50, payload = 100 → should trip before completion.
    chunks = [b"x" * 40, b"y" * 60]
    fake = _FakeAsyncClient(_FakeStreamResponse(chunks))
    dest = tmp_path / "welcome.tar.gz"

    with pytest.raises(DownloadTooLargeError):
        await stream_download(dest, pin=_pin(max_bytes=50), client=fake)


async def test_stream_download_skips_empty_chunks(tmp_path: Path) -> None:
    payload = b"meaningful"
    chunks = [b"", payload, b""]
    fake = _FakeAsyncClient(_FakeStreamResponse(chunks, content_length=len(payload)))
    dest = tmp_path / "welcome.tar.gz"

    result = await stream_download(dest, pin=_pin(), client=fake)
    assert dest.read_bytes() == payload
    assert result.bytes_downloaded == len(payload)


# ---------------------------------------------------------------------------
# verify_sha256
# ---------------------------------------------------------------------------


def test_verify_sha256_passes_on_match(tmp_path: Path) -> None:
    payload = b"abc123"
    digest = hashlib.sha256(payload).hexdigest()
    result = DownloadResult(path=tmp_path / "x", bytes_downloaded=len(payload), sha256=digest)
    # Must not raise.
    verify_sha256(result, pin=_pin(sha256=digest))


def test_verify_sha256_raises_on_mismatch(tmp_path: Path) -> None:
    expected = hashlib.sha256(b"expected").hexdigest()
    actual = hashlib.sha256(b"actual").hexdigest()
    result = DownloadResult(path=tmp_path / "x", bytes_downloaded=6, sha256=actual)

    with pytest.raises(HashMismatchError) as excinfo:
        verify_sha256(result, pin=_pin(sha256=expected))
    assert expected in str(excinfo.value)
    assert actual in str(excinfo.value)


def test_verify_sha256_rejects_placeholder_pin(tmp_path: Path) -> None:
    # When the pinned SHA-256 is all zeros (placeholder), refuse outright —
    # even if the downloaded hash also happens to be zeros.
    result = DownloadResult(path=tmp_path / "x", bytes_downloaded=0, sha256="0" * 64)
    with pytest.raises(HashMismatchError, match="placeholder"):
        verify_sha256(result, pin=_pin(sha256="0" * 64))


def test_verify_sha256_is_case_insensitive(tmp_path: Path) -> None:
    payload = b"case-check"
    digest = hashlib.sha256(payload).hexdigest()
    result = DownloadResult(
        path=tmp_path / "x", bytes_downloaded=len(payload), sha256=digest.upper()
    )
    verify_sha256(result, pin=_pin(sha256=digest.lower()))
