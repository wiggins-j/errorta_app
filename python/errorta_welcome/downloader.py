"""Stream the welcome-corpus tarball and verify its SHA-256 against the pin."""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import AsyncIterator, Optional

import aiofiles
import httpx

from . import PINNED_HASH_PATH


@dataclass
class PinnedHash:
    tarball: str
    source_url: str
    version: str
    sha256: str
    max_bytes: int


def load_pinned_hash() -> PinnedHash:
    raw = json.loads(PINNED_HASH_PATH.read_text(encoding="utf-8"))
    return PinnedHash(
        tarball=raw["tarball"],
        source_url=raw["source_url"],
        version=raw["version"],
        sha256=raw["sha256"].lower(),
        max_bytes=int(raw["max_bytes"]),
    )


class HashMismatchError(RuntimeError):
    """Raised when the downloaded tarball's SHA-256 does not match the pin."""


class DownloadTooLargeError(RuntimeError):
    """Raised when the stream exceeds the pinned max_bytes cap."""


@dataclass
class DownloadResult:
    path: Path
    bytes_downloaded: int
    sha256: str


async def stream_download(
    dest: Path,
    progress_cb: Optional[callable] = None,
    *,
    pin: Optional[PinnedHash] = None,
    client: Optional[httpx.AsyncClient] = None,
) -> DownloadResult:
    """Download the tarball to ``dest`` with streaming + size cap.

    Calls ``progress_cb(bytes_downloaded, total_bytes_or_none)`` after each chunk
    if provided. Does not verify the hash — call :func:`verify_sha256` next.
    """
    pin = pin or load_pinned_hash()
    own_client = client is None
    if client is None:
        client = httpx.AsyncClient(follow_redirects=True, timeout=30.0)

    sha = hashlib.sha256()
    total: Optional[int] = None
    written = 0

    try:
        async with client.stream("GET", pin.source_url) as resp:
            resp.raise_for_status()
            content_length = resp.headers.get("content-length")
            if content_length is not None:
                try:
                    total = int(content_length)
                except ValueError:
                    total = None
            dest.parent.mkdir(parents=True, exist_ok=True)
            async with aiofiles.open(dest, "wb") as fh:
                async for chunk in resp.aiter_bytes(chunk_size=64 * 1024):
                    if not chunk:
                        continue
                    written += len(chunk)
                    if written > pin.max_bytes:
                        raise DownloadTooLargeError(
                            f"welcome tarball exceeded {pin.max_bytes} bytes"
                        )
                    sha.update(chunk)
                    await fh.write(chunk)
                    if progress_cb is not None:
                        progress_cb(written, total)
    finally:
        if own_client:
            await client.aclose()

    return DownloadResult(path=dest, bytes_downloaded=written, sha256=sha.hexdigest())


def verify_sha256(result: DownloadResult, pin: Optional[PinnedHash] = None) -> None:
    pin = pin or load_pinned_hash()
    actual = result.sha256.lower()
    expected = pin.sha256.lower()
    if expected == "0" * 64:
        raise HashMismatchError(
            "pinned SHA-256 is a placeholder; refuse to ingest unverified tarball"
        )
    if actual != expected:
        raise HashMismatchError(
            f"sha256 mismatch: expected {expected}, got {actual}"
        )


async def _noop() -> AsyncIterator[None]:  # pragma: no cover — typing stub
    yield None
