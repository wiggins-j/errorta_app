"""HEAD-probe the F-INFRA-11 welcome-corpus release URL.

Catches the "demo fails because the F-INFRA-11 release is still draft"
trap before it bites a viewer. Skips cleanly under DEMO_OFFLINE=1 OR
when the pinned URL is empty / missing (defensive draft marker).

The non-200 path is treated as the de-facto draft signal: as of
2026-06-12 the `latest/download/` URL pattern in pinned_hash.json
returns 404 until F-INFRA-11 slice (e) un-drafts the release on
wiggins-j/errorta-downloads. The AssertionError points at the local
tarball fallback path documented in DEVELOPING.md.

See: docs/specs/F031-DEMO-BOOT-VERIFY-boot-sequence.md
"""
from __future__ import annotations

import json
import os
import pathlib

import httpx
import pytest

# Anchor the pinned-hash path off this file so the test runs from any
# cwd. python/tests/test_*.py -> python/errorta_welcome/pinned_hash.json
_PINNED_HASH = (
    pathlib.Path(__file__).resolve().parent.parent
    / "errorta_welcome"
    / "pinned_hash.json"
)


def _is_truthy_env(name: str) -> bool:
    raw = os.environ.get(name, "")
    return raw.lower() in ("1", "true", "yes")


def _load_source_url() -> str:
    """Return the source_url from pinned_hash.json, or '' if missing.

    The file shape was verified at slice plan time:
        {
          "tarball": "welcome-corpus.tar.gz",
          "source_url": "https://github.com/wiggins-j/errorta-downloads/...",
          "version": "0.1.0",
          "sha256": "...",
          "max_bytes": 5242880,
          "notes": "..."
        }

    Implementer-call decision (plan §PM resolutions #4): "draft" is
    detected as `source_url` being empty / missing. The richer signal
    (non-200 HEAD on `latest/download/...`) is handled as the failure
    path of the live probe rather than a static draft marker.
    """
    if not _PINNED_HASH.is_file():
        return ""
    try:
        data = json.loads(_PINNED_HASH.read_text())
    except json.JSONDecodeError:
        return ""
    if not isinstance(data, dict):
        return ""
    # Accept both `source_url` (current) and `url` (defensive fallback
    # in case the field name shifts in a future F-INFRA-11 slice).
    raw = data.get("source_url") or data.get("url") or ""
    return raw if isinstance(raw, str) else ""


@pytest.mark.skipif(
    _is_truthy_env("DEMO_OFFLINE"),
    reason="DEMO_OFFLINE=1",
)
def test_welcome_corpus_release_reachable() -> None:
    """HEAD the pinned welcome-corpus URL.

    200 -> pass.
    Empty URL / draft marker -> skip with a clear reason.
    Any other status -> AssertionError pointing at the local fallback.
    """
    url = _load_source_url()
    if not url:
        pytest.skip(
            "welcome-corpus release URL is empty in pinned_hash.json "
            "(treated as draft marker)"
        )
    try:
        with httpx.Client(timeout=5.0) as client:
            r = client.head(url, follow_redirects=True)
    except httpx.HTTPError as exc:
        pytest.skip(
            f"network unreachable for welcome-corpus HEAD ({exc!r}); "
            "re-run with DEMO_OFFLINE=1 to silence this test"
        )
    if r.status_code == 200:
        return
    raise AssertionError(
        f"HEAD {url} returned {r.status_code}. "
        f"The F-INFRA-11 release is likely still draft "
        f" "
        f"Pre-stage a local copy at "
        f"python/errorta_welcome/welcome-corpus.tar.gz "
        f"OR re-run with DEMO_OFFLINE=1."
    )
